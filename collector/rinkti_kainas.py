"""Surenka pigiausias Ryanair ir Wizz Air kainas iš pasirinktų oro uostų į visas kryptis
(ir, jei neišjungta, antro skrydžio kainas iš persėdimo miestų į šiltas / egzotines kryptis)
ir įrašo jas į JSON failą. Programėlė (docs/) ir pranešimų tikrinimas (tikrinti.mjs) skaito tą failą.

Naudojimas:
    python collector/rinkti_kainas.py                       # VNO KUN RIX WAW WMI, 120 d., su persėdimais
    python collector/rinkti_kainas.py --is VNO KUN --dienos 60
    python collector/rinkti_kainas.py --be-persedimu        # tik tiesioginiai (~9 min vietoj ~22)
    python collector/rinkti_kainas.py --plan build/plan.json  # tikslinė paieška (žr. tikslinis())

Abi avialinijos naudoja neoficialius savo svetainių API adresus — jie gali pasikeisti.
Kainos: vienam suaugusiam, be registruoto bagažo, EUR.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUT = DIR / "build" / "kainos.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
BASE_H = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
          "Accept-Language": "en-GB,en;q=0.9"}

DEFAULT_ORIGINS = ["VNO", "KUN", "RIX", "WAW", "WMI"]
# Didieji persėdimo miestai: iš jų renkamos kainos į VISAS kryptis (iš kitų — tik į šiltas)
DEFAULT_HUBS = ["BGY", "STN", "BUD", "CRL", "BCN", "LTN"]

# Šiltos / egzotinės kryptys, į kurias ieškoma skrydžių per persėdimą.
# Šalys — kaip jas vadina avialinijų API; Ispanija/Portugalija/Turkija — tik konkretūs oro uostai.
WARM_COUNTRIES = {
    "Egypt", "Morocco", "Jordan", "Israel", "Cyprus", "Tunisia", "Lebanon",
    "Georgia", "Armenia", "Azerbaijan",
    "United Arab Emirates", "Saudi Arabia", "Oman", "Kuwait", "Bahrain", "Qatar",
    "Kazakhstan", "Uzbekistan", "Kyrgyzstan", "Tajikistan", "Cape Verde", "Maldives",
}
WARM_AIRPORTS = {
    "TFS", "TFN", "LPA", "ACE", "FUE", "SPC",  # Kanarai
    "FNC", "PXO", "PDL", "TER",                # Madeira, Azorai
    "AYT", "DLM", "BJV", "GZP",                # Pietų Turkija
}

# Žiemos laiko juostos (min. nuo UTC) — tik Wizz atvykimo laikui įvertinti
TZ_COUNTRY = {
    "United Kingdom": 0, "Ireland": 0, "Portugal": 0, "Iceland": 0, "Cape Verde": -60,
    "Lithuania": 120, "Latvia": 120, "Estonia": 120, "Finland": 120, "Greece": 120,
    "Romania": 120, "Bulgaria": 120, "Cyprus": 120, "Israel": 120, "Moldova": 120,
    "Ukraine": 120, "Egypt": 120, "Lebanon": 120,
    "Jordan": 180, "Türkiye": 180, "Turkey": 180, "Saudi Arabia": 180, "Kuwait": 180,
    "Bahrain": 180, "Qatar": 180,
    "Georgia": 240, "Armenia": 240, "Azerbaijan": 240, "United Arab Emirates": 240, "Oman": 240,
    "Kazakhstan": 300, "Uzbekistan": 300, "Tajikistan": 300, "Maldives": 300, "Kyrgyzstan": 360,
}
TZ_AIRPORT = {"TFS": 0, "TFN": 0, "LPA": 0, "ACE": 0, "FUE": 0, "SPC": 0, "PDL": -60, "TER": -60}

FAIL = object()
_lock = threading.Lock()
# fares[(from, to)][date][avialinija] = [kaina, avialinija, išvykimo "HH:MM", atvykimas min. nuo išvykimo
#   dienos vidurnakčio (atvykimo vietos laiku) arba None, 1 jei atvykimas apskaičiuotas, (1 — airBaltic su persėdimu)]
# Saugoma pigiausia kiekvienos avialinijos tos dienos kaina (filtrams pagal avialiniją ir laiką).
fares: dict[tuple[str, str], dict[str, list]] = {}
airports: dict[str, dict] = {}
errors: list[str] = []


def is_warm(code: str, country: str) -> bool:
    return code in WARM_AIRPORTS or country in WARM_COUNTRIES


def put_fare(frm: str, to: str, day: str, price: float, airline: str, hhmm: str,
             arr_min: int | None, est: int, via: int = 0) -> None:
    """via=1 — skrydis su persėdimu vienu bilietu (airBaltic, Norwegian)."""
    extra = [1] if via else []
    with _lock:
        per = fares.setdefault((frm, to), {}).setdefault(day, {})
        cur = per.get(airline)
        if cur is None or price < cur[0]:
            per[airline] = [round(price, 2), airline, hhmm, arr_min, est] + extra


def put_airport(code: str, name: str, country: str, lat=None, lon=None) -> None:
    with _lock:
        a = airports.setdefault(code, {})
        if name and "n" not in a:
            a["n"], a["c"] = name.strip(), (country or "").strip()
            if is_warm(code, country):
                a["w"] = 1
        if lat is not None and "lat" not in a:
            a["lat"], a["lon"] = round(lat, 3), round(lon, 3)


def tz_of(code: str) -> int:
    if code in TZ_AIRPORT:
        return TZ_AIRPORT[code]
    return TZ_COUNTRY.get(airports.get(code, {}).get("c", ""), 60)


def est_arrival(frm: str, to: str, hhmm: str) -> int | None:
    """Wizz neduoda atvykimo laiko: įvertinam pagal atstumą (~800 km/h + 30 min) ir laiko juostas."""
    a, b = airports.get(frm, {}), airports.get(to, {})
    if not hhmm or "lat" not in a or "lat" not in b:
        return None
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dl = math.radians(b["lon"] - a["lon"])
    km = 6371 * math.acos(min(1, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)))
    dur = km / 800 * 60 + 30
    dep = int(hhmm[:2]) * 60 + int(hhmm[3:5])
    return round(dep + dur + tz_of(to) - tz_of(frm))


def get_json(session: requests.Session, method: str, url: str, *, tries: int = 4,
             pause: float = 0.0, log: bool = True, raw: bool = False, **kw):
    """Grąžina JSON, None (maršruto/periodo nėra) arba FAIL (nepavyko po kelių bandymų)."""
    slow, err = 2, ""
    for i in range(tries):
        if pause:
            time.sleep(pause)
        try:
            r = session.request(method, url, timeout=30, **kw)
        except requests.RequestException as e:
            err = str(e)
        else:
            if r.status_code == 200:
                return r.text if raw else r.json()
            if r.status_code in (400, 404):
                return None  # tokio maršruto / periodo nėra
            err = f"HTTP {r.status_code}"
            if r.status_code in (403, 429, 503):
                slow = 6  # avialinija riboja užklausų dažnį
        time.sleep(slow * (i + 1))
    if log:
        with _lock:
            errors.append(f"{url.split('?')[0]}: {err}")
    return FAIL


def run_jobs(fn, jobs, workers: int, label: str) -> list:
    """Paleidžia užduotis lygiagrečiai; grąžina tas, kurių fn grąžino False."""
    done, failed = 0, []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, *j): j for j in jobs}
        for f in as_completed(futs):
            if f.result() is False:
                failed.append(futs[f])
            done += 1
            if done % 100 == 0 or done == len(jobs):
                print(f"  {label}: {done}/{len(jobs)}", flush=True)
    return failed


def months_between(a: dt.date, b: dt.date) -> list[dt.date]:
    out, m = [], a.replace(day=1)
    while m <= b:
        out.append(m)
        m = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return out


# ---------------------------------------------------------------- Ryanair
class Ryanair:
    API = "https://services-api.ryanair.com/farfnd/v4"
    ROUTES = "https://www.ryanair.com/api/views/locate/searchWidget/routes/en/airport/"

    def __init__(self, start: dt.date, end: dt.date):
        self.s = requests.Session()
        self.s.headers.update(BASE_H)
        self.start, self.end = start, end
        self.fetched: set[tuple[str, str]] = set()
        self.extra_days: set[str] = set()  # sekamų skrydžių datos, kurios gali būti už laikotarpio ribų

    def direct_routes(self, origins: list[str]) -> set[tuple[str, str]]:
        routes = set()
        for o in origins:
            d = get_json(self.s, "GET", f"{self.API}/oneWayFares", params={
                "departureAirportIataCode": o, "outboundDepartureDateFrom": str(self.start),
                "outboundDepartureDateTo": str(self.end), "currency": "EUR", "market": "en-gb",
                "limit": 500})
            if d is FAIL:
                continue
            for f in (d or {}).get("fares", []):
                ob = f["outbound"]
                dep, arr = ob["departureAirport"], ob["arrivalAirport"]
                put_airport(dep["iataCode"], dep["city"]["name"] if dep.get("city") else dep["name"],
                            dep["countryName"])
                put_airport(arr["iataCode"], arr["name"], arr["countryName"])
                routes |= {(o, arr["iataCode"]), (arr["iataCode"], o)}
        return routes

    def hub_routes(self, hubs: set[str], skip: set[str], full: set[str]) -> set[tuple[str, str]]:
        """Maršrutai iš persėdimo miestų (ir atgal): iš `full` — į visas kryptis, iš kitų — tik į šiltas."""
        def one(h):
            d = get_json(self.s, "GET", self.ROUTES + h, log=False)
            out = set()
            for x in d if isinstance(d, list) else []:
                a = x["arrivalAirport"]
                code, country = a["code"], a["country"]["name"]
                c = a.get("coordinates") or {}
                put_airport(code, a.get("name", code), country, c.get("latitude"), c.get("longitude"))
                if code not in skip and (h in full or is_warm(code, country)):
                    out |= {(h, code), (code, h)}
            return out
        routes = set()
        with ThreadPoolExecutor(max_workers=6) as ex:
            for r in ex.map(one, sorted(hubs)):
                routes |= r
        return routes

    def fetch(self, routes: set[tuple[str, str]], label: str) -> None:
        routes = routes - self.fetched
        self.fetched |= routes
        jobs = [(a, b, m) for a, b in sorted(routes) for m in months_between(self.start, self.end)]
        print(f"{label}: {len(routes)} maršrutai, {len(jobs)} užklausos")
        failed = run_jobs(self._job, jobs, workers=6, label=label)
        if failed:
            print(f"  {label}: {len(failed)} užklausų nepavyko, po 30 s bandau dar kartą lėčiau")
            time.sleep(30)
            run_jobs(self._job, [(*j, True) for j in failed], workers=2, label=f"{label} (pakartotinai)")

    def _job(self, a: str, b: str, m: dt.date, last_try: bool = False) -> bool:
        d = get_json(self.s, "GET", f"{self.API}/oneWayFares/{a}/{b}/cheapestPerDay",
                     params={"outboundMonthOfDate": str(m), "currency": "EUR"},
                     pause=1.0 if last_try else 0.0, log=last_try)
        if d is FAIL:
            return False
        for f in ((d or {}).get("outbound") or {}).get("fares", []):
            if f.get("unavailable") or f.get("soldOut") or not f.get("price"):
                continue
            day = f["day"]
            if not (str(self.start) <= day <= str(self.end)) and day not in self.extra_days:
                continue
            dep, arr = f.get("departureDate") or "", f.get("arrivalDate") or ""
            arr_min = None
            if arr:
                days = (dt.date.fromisoformat(arr[:10]) - dt.date.fromisoformat(day)).days
                arr_min = days * 1440 + int(arr[11:13]) * 60 + int(arr[14:16])
            put_fare(a, b, day, f["price"]["value"], "FR", dep[11:16], arr_min, 0)
        return True


# ---------------------------------------------------------------- valiutos
def load_rates() -> dict[str, float]:
    """Kiek vienetų valiutos už 1 EUR. Wizz kainas grąžina išvykimo šalies valiuta."""
    try:
        r = requests.get("https://open.er-api.com/v6/latest/EUR", timeout=30).json()
        if r.get("result") == "success":
            return r["rates"]
    except (requests.RequestException, ValueError):
        pass
    try:  # atsarginis šaltinis — ECB (neturi kai kurių valiutų, pvz. GEL, AED)
        xml = requests.get("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml", timeout=30).text
        rates = {c: float(v) for c, v in re.findall(r"currency='(\w+)' rate='([\d.]+)'", xml)}
        rates["EUR"] = 1.0
        return rates
    except requests.RequestException:
        return {"EUR": 1.0}


# ---------------------------------------------------------------- Wizz Air
class Wizz:
    def __init__(self, start: dt.date, end: dt.date):
        self.s = requests.Session()
        self.s.headers.update(BASE_H)
        self.start, self.end = start, end
        self.hdr = {"Origin": "https://wizzair.com", "Referer": "https://wizzair.com/en-gb"}
        self.cities: dict[str, dict] = {}
        self.fetched: set[frozenset] = set()
        self.base = None
        self.rates = load_rates()
        self.unknown_ccy: set[str] = set()
        try:
            html = self.s.get("https://wizzair.com/en-gb", headers={**BASE_H, "Accept": "text/html"},
                              timeout=30).text
            m = re.search(r"be\.wizzair\.com/(\d+\.\d+\.\d+)", html)
        except requests.RequestException:
            m = None
        if not m:
            errors.append("Wizz Air: nepavyko nustatyti API versijos — Wizz kainos praleistos")
            print("Wizz Air: API versija nerasta, praleidžiu")
            return
        self.base = f"https://be.wizzair.com/{m.group(1)}/Api"
        mp = get_json(self.s, "GET", f"{self.base}/asset/map", params={"languageCode": "en-gb"},
                      headers=self.hdr)
        if not mp or mp is FAIL:
            errors.append("Wizz Air: nepavyko gauti maršrutų žemėlapio")
            self.base = None
            return
        self.cities = {c["iata"]: c for c in mp.get("cities", [])}
        for code, c in self.cities.items():
            put_airport(code, c.get("shortName", code), c.get("countryName", ""),
                        c.get("latitude"), c.get("longitude"))

    def routes_from(self, airports_: set[str], warm_only: bool, skip: set[str] = frozenset()):
        out = set()
        for h in airports_:
            for c in self.cities.get(h, {}).get("connections", []):
                code = c["iata"]
                if code in skip:
                    continue
                if not warm_only or is_warm(code, self.cities.get(code, {}).get("countryName", "")):
                    out.add((h, code))
        return out

    def fetch(self, routes: set[tuple[str, str]], label: str) -> None:
        if not self.base:
            return
        pairs = {frozenset(r) for r in routes} - self.fetched
        self.fetched |= pairs
        chunks, a = [], self.start
        while a <= self.end:  # API priima ne ilgesnį nei 42 d. intervalą
            b = min(a + dt.timedelta(days=41), self.end)
            chunks.append((a, b))
            a = b + dt.timedelta(days=1)
        jobs = [(*sorted(p), a, b) for p in sorted(pairs, key=sorted) for a, b in chunks]
        print(f"{label}: {len(pairs)} maršrutai (abi kryptys), {len(jobs)} užklausos")
        failed = run_jobs(self._job, jobs, workers=2, label=label)
        if failed:
            print(f"  {label}: {len(failed)} užklausų nepavyko, po 30 s bandau dar kartą lėčiau")
            time.sleep(30)
            run_jobs(self._job, [(*j, True) for j in failed], workers=1, label=f"{label} (pakartotinai)")

    def _job(self, o: str, d: str, a: dt.date, b: dt.date, last_try: bool = False) -> bool:
        body = {"flightList": [
            {"departureStation": o, "arrivalStation": d, "from": str(a), "to": str(b)},
            {"departureStation": d, "arrivalStation": o, "from": str(a), "to": str(b)}],
            "priceType": "regular", "adultCount": 1, "childCount": 0, "infantCount": 0}
        r = get_json(self.s, "POST", f"{self.base}/search/timetable", json=body, headers=self.hdr,
                     pause=1.5 if last_try else 0.4, log=last_try)
        if r is FAIL:
            return False
        if not r:
            return True
        for f in (r.get("outboundFlights") or []) + (r.get("returnFlights") or []):
            price = f.get("price") or {}
            p, ccy = price.get("amount"), price.get("currencyCode", "EUR")
            if not p or f.get("priceType") != "price":
                continue
            if ccy not in self.rates:
                with _lock:
                    if ccy not in self.unknown_ccy:
                        self.unknown_ccy.add(ccy)
                        errors.append(f"Wizz Air: nežinomas valiutos kursas {ccy} — tos kainos praleistos")
                continue
            p = p / self.rates[ccy]
            # Imama tikra stotis iš atsakymo (Wizz KUN užklausai grąžina VNO skrydžius)
            frm, to = f["departureStation"], f["arrivalStation"]
            times = f.get("departureDates") or []
            hhmm = times[0][11:16] if times else ""
            put_fare(frm, to, f["departureDate"][:10], p, "W6", hhmm, est_arrival(frm, to, hhmm), 1)
        return True


# ---------------------------------------------------------------- airBaltic
class AirBaltic:
    """airBaltic svetainės kainų kalendorius: viena užklausa — kiekvienos dienos pigiausia kaina metams į priekį.
    Skrydžio laiko kalendorius neduoda, todėl airBaltic skrydžiai nejungiami su kitų avialinijų persėdimais."""
    API = "https://www.airbaltic.com/api/"

    def __init__(self, start: dt.date, end: dt.date):
        self.s = requests.Session()
        self.s.headers.update({**BASE_H, "Referer": "https://www.airbaltic.com/en-LT/index"})
        self.start, self.end = start, end
        self.dests: dict[str, dict[str, bool]] = {}  # išvykimas -> {kryptis: ar yra tiesioginis}
        self.fetched: set[tuple[str, str]] = set()
        j = get_json(self.s, "GET", self.API + "orig-dest/en", log=False)
        if not j or j is FAIL:
            errors.append("airBaltic: nepavyko gauti maršrutų sąrašo — airBaltic kainos praleistos")
            print("airBaltic: maršrutų sąrašas nepasiekiamas, praleidžiu")
            return
        for okey in (j.get("origData") or {}).get("btOrigins") or {}:
            info = (j.get("destinData") or {}).get(okey) or {}
            o = okey[:-1] if okey.endswith("A") else okey
            out = {}
            for dkey, v in (info.get("btDest") or {}).items():
                code = v.get("code") or dkey[:-1]
                out[code] = bool(v.get("hasBTDirect"))
                try:
                    put_airport(code, v.get("city") or code, v.get("country") or "",
                                float(v["latitude"]), float(v["longitude"]))
                except (KeyError, TypeError, ValueError):
                    put_airport(code, v.get("city") or code, v.get("country") or "")
            if out:
                self.dests[o] = out

    def routes(self, origins: list[str], targets: list[str] | None = None) -> set[tuple[str, str]]:
        pairs = set()
        for o in origins:
            for t in self.dests.get(o, {}):
                if t not in origins and (targets is None or t in targets):
                    pairs |= {(o, t), (t, o)}
        return pairs

    def fetch(self, pairs: set[tuple[str, str]], label: str = "airBaltic") -> None:
        pairs = pairs - self.fetched
        self.fetched |= pairs
        if not pairs:
            return
        print(f"{label}: {len(pairs) // 2 or len(pairs)} maršrutai, {len(pairs)} užklausos")
        failed = run_jobs(self._job, sorted(pairs), workers=3, label=label)
        if failed:
            time.sleep(20)
            run_jobs(self._job, [(*j, True) for j in failed], workers=1, label=f"{label} (pakartotinai)")

    def _job(self, a: str, b: str, last_try: bool = False) -> bool:
        d = get_json(self.s, "GET", self.API + "fsf/outbound",
                     params={"flightMode": "oneway", "origin": a, "destin": b},
                     pause=1.0 if last_try else 0.2, log=last_try)
        if d is FAIL:
            return False
        for x in (d or {}).get("data") or []:
            day, price = x.get("date"), x.get("price")
            if not day or not price or not (str(self.start) <= day <= str(self.end)):
                continue
            put_fare(a, b, day, float(price), "BT", "", None, 0, via=0 if x.get("isDirect") else 1)
        return True


# ---------------------------------------------------------------- Norwegian
class Norwegian:
    """Norwegian svetainės kainų kalendorius: viena užklausa — kiekvienos mėnesio dienos pigiausia kaina.
    Maršrutų sąrašo nėra, todėl neaptarnaujamas maršrutas atpažįstamas iš 400 atsakymo. Laiko neduoda."""
    API = "https://www.norwegian.com/api/fare-calendar/calendar"
    ORIGINS = {"RIX", "VNO", "WAW"}  # iš KUN ir WMI Norwegian neskrenda
    # Plačiai paieškai: tikėtinos Norwegian kryptys (neaptarnaujamos grąžina 400 iš pirmo karto)
    BROAD = ["OSL", "ARN", "CPH", "HEL", "BGO", "TRD", "SVG", "TOS", "BOO", "AES", "KRS", "GOT", "AAL", "BLL"]

    def __init__(self, start: dt.date, end: dt.date):
        self.s = requests.Session()
        self.s.headers.update({**BASE_H, "Accept-Language": "en-GB", "Referer": "https://www.norwegian.com/"})
        self.start, self.end = start, end
        self.fetched: set[tuple[str, str]] = set()

    def routes(self, origins: list[str], targets: list[str] | None = None) -> set[tuple[str, str]]:
        ts = targets if targets is not None else self.BROAD
        return {pr for o in origins if o in self.ORIGINS for t in ts if t != o and t not in origins
                for pr in ((o, t), (t, o))}

    def fetch(self, pairs: set[tuple[str, str]], label: str = "Norwegian") -> None:
        pairs = pairs - self.fetched
        self.fetched |= pairs
        if not pairs:
            return
        print(f"{label}: {len(pairs)} kryptys (neaptarnaujamos atmetamos po 1 užklausos)")
        failed = run_jobs(self._pair, sorted(pairs), workers=2, label=label)
        if failed:
            time.sleep(20)
            run_jobs(self._pair, [(*j, True) for j in failed], workers=1, label=f"{label} (pakartotinai)")

    def _pair(self, a: str, b: str, last_try: bool = False) -> bool:
        empty = 0
        for m in months_between(self.start, self.end):
            d = get_json(self.s, "GET", self.API, params={
                "adultCount": 1, "originAirportCode": a, "destinationAirportCode": b,
                "outboundDate": str(max(m, self.start)), "tripType": 1, "currencyCode": "EUR", "languageCode": "en-GB"},
                pause=1.0 if last_try else 0.3, log=last_try)
            if d is FAIL:
                return False
            if not d:          # 400 — maršruto nėra
                return True
            out = d.get("outbound") or {}
            priced = 0
            for x in out.get("days") or []:
                day, price = (x.get("date") or "")[:10], x.get("price") or 0
                if not price or x.get("isSoldOut") or not (str(self.start) <= day <= str(self.end)):
                    continue
                priced += 1
                put_fare(a, b, day, float(price), "DY", "", None, 0, via=1 if (x.get("transitCount") or 0) > 0 else 0)
            empty = 0 if priced else empty + 1
            if out.get("nextMonthDisabled") or empty >= 3:  # toliau neparduoda arba maršrutas neaktyvus
                break
        return True


# ---------------------------------------------------------------- Novaturas (užsakomieji skrydžiai)
class Novaturas:
    """Novaturas skrydžiai be viešbučio (flights.novatours.eu, bendra LT/LV/EE sistema).
    Savaitės kalendorius duoda kainą be oro uosto mokesčio kiekvienai dienai; pasirinktos dienos
    bilietų klasės — laikus ir pilną kainą, iš kurių apskaičiuojamas mokestis."""
    B = "https://flights.novatours.eu"
    AL = "NOV"  # programėlėje rodoma kaip „Novaturas“ (vykdo Heston, airBaltic ir kt.)
    OPT = re.compile(r'value="([ab]);([^;]+);([^;]+);([^;]*);([A-Z]{3});([A-Z]{3});([^;]*);[^;]*;[^;]*;([\d.]+);[^;]*;[^;]*;([\d.]+);')
    DAY = re.compile(r'<div class="fl day flight d-([\d-]+)">.*?class="s18 price"[^>]*>([\d.]+)<', re.S)

    def __init__(self, start: dt.date, end: dt.date):
        self.s = requests.Session()
        self.s.headers.update({**BASE_H, "Accept-Language": "lt-LT,en", "Referer": self.B + "/skrydziai/1"})
        self.start, self.end = start, end
        self.dests: dict[str, list[str]] = {}
        self.fetched: set[tuple[str, str]] = set()
        try:
            self.s.get(self.B + "/", timeout=30)
            # /g/ puslapis inicijuoja sesiją ir turi maršrutų sąrašą (var airports = {...})
            # kelionės pirmyn ir atgal nuoroda — tada veikia ir grįžimo krypties kainos
            d1, d2 = start + dt.timedelta(days=7), start + dt.timedelta(days=14)
            html = self.s.get(f"{self.B}/g/VNO/HRG/{d1.strftime('%Y%m%d')}/{d2.strftime('%Y%m%d')}", timeout=30).text
        except requests.RequestException:
            html = ""
        m = re.search(r"airports\s*=\s*\{(.*?)\};", html, re.S)
        for line in (m.group(1).splitlines() if m else []):
            o = re.match(r"\s*'([A-Z]{3})'\s*:\s*\{name:\s*'([^',]+)", line)
            if o:
                put_airport(o.group(1), o.group(2), "")
                self.dests[o.group(1)] = re.findall(r"'([A-Z]{3})'\s*:\s*\{name", line.split("destinations", 1)[-1])
        if not self.dests:
            errors.append("Novaturas: nepavyko gauti maršrutų — Novaturas skrydžiai praleisti")
            print("Novaturas: maršrutų sąrašas nepasiekiamas, praleidžiu")

    def routes(self, origins: list[str], targets: list[str] | None = None) -> set[tuple[str, str]]:
        return {pr for o in origins for t in self.dests.get(o, [])
                if t not in origins and (targets is None or t in targets) for pr in ((o, t), (t, o))}

    def _week(self, a: str, b: str, day: dt.date, kryptis: str = "a"):
        d = get_json(self.s, "GET", self.B + "/ajax.php", params={
            "callback": "cb", "komanda": "kalendorius-savaite", "lang": "1lt", "keleiviai": "1;0;0",
            "kryptis": kryptis, "isvykimasVieta": a, "atvykimasVieta": b, "pazymetaData": str(day),
            "nuoData": str(self.start) if kryptis == "b" else ""}, pause=0.7, log=False, raw=True)
        if not d or d is FAIL or "(" not in d:
            return None
        try:
            return json.loads(d[d.index("(") + 1:d.rindex(")")]).get("html") or ""
        except ValueError:
            return None

    def fetch(self, pairs: set[tuple[str, str]], label: str = "Novaturas") -> None:
        pairs = sorted(pairs - self.fetched)
        self.fetched |= set(pairs)
        if not pairs:
            return
        print(f"{label}: {len(pairs)} kryptys")
        for a, b in pairs:  # nuosekliai — svetainė maža, sesija viena
            self._pair(a, b)

    def _pair(self, a: str, b: str) -> None:
        fares_by_day: dict[str, float] = {}
        times: dict[str, tuple[str, int]] = {}   # diena -> (išvykimas HH:MM, atvykimas min.)
        by_wd: dict[int, tuple[str, int]] = {}   # savaitės diena -> laikai
        tax = None

        def opts(html: str):
            nonlocal tax
            for _, dep, arr, _fn, frm, to, _al, fare, total in self.OPT.findall(html):
                if frm != a or to != b or len(dep) < 16:
                    continue
                day = dep[:10]
                arr_min = (dt.date.fromisoformat(arr[:10]) - dt.date.fromisoformat(day)).days * 1440 \
                    + int(arr[11:13]) * 60 + int(arr[14:16])
                times[day] = (dep[11:16], arr_min)
                by_wd[dt.date.fromisoformat(day).weekday()] = (dep[11:16], arr_min)
                t = round(float(total) - float(fare), 2)
                if t >= 0 and (tax is None or t < tax):
                    tax = t

        d = self.start + dt.timedelta(days=3)
        while d - dt.timedelta(days=3) <= self.end:
            html = self._week(a, b, d)
            if html is None:
                return
            for day, price in self.DAY.findall(html):
                fares_by_day[day] = float(price)
            opts(html)
            d += dt.timedelta(days=7)
        if not fares_by_day:
            return
        # Laikai matosi tik savaitės centrinei dienai: kiekvienai skraidymo savaitės dienai be laiko —
        # po vieną užklausą, centruotą į tos savaitės dienos skrydį (grįžimo kryptį bandom ir su kryptis=b)
        for wd in sorted({dt.date.fromisoformat(x).weekday() for x in fares_by_day} - set(by_wd)):
            day = min(x for x in fares_by_day if dt.date.fromisoformat(x).weekday() == wd)
            for kr in ("a", "b"):
                html = self._week(a, b, dt.date.fromisoformat(day), kr)
                if html:
                    opts(html)
                if wd in by_wd:
                    break
        for day, fare in fares_by_day.items():
            if not (str(self.start) <= day <= str(self.end)):
                continue
            t = times.get(day) or by_wd.get(dt.date.fromisoformat(day).weekday())
            put_fare(a, b, day, fare + (tax or 0), self.AL, t[0] if t else "", t[1] if t else None,
                     0 if day in times else 1)


# ---------------------------------------------------------------- tikslinė paieška
# Kaunas Wizz žemėlapyje turi Vilniaus jungtis (užklausos grąžina VNO skrydžius) — Wizz jam praleidžiame
WIZZ_SKIP = {"KUN"}


def tikslinis(plan: dict, origins: list[str], ry: "Ryanair | None", wz: "Wizz | None",
              ab: "AirBaltic | None" = None, dy: "Norwegian | None" = None,
              nv: "Novaturas | None" = None) -> None:
    """Kainos tik į nurodytas kryptis (plan["targets"]): tiesiogiai ir per VISUS persėdimo miestus,
    į kuriuos skrendama iš išvykimo oro uostų ir iš kurių skrendama į tikslą. Plius sekami skrydžiai."""
    targets = [t.upper() for t in plan.get("targets", [])]
    fr_cache: dict[str, set[str]] = {}

    def fr(code: str) -> set[str]:
        if not ry:
            return set()
        if code not in fr_cache:
            d = get_json(ry.s, "GET", Ryanair.ROUTES + code, log=False)
            out = set()
            for x in d if isinstance(d, list) else []:
                a = x["arrivalAirport"]
                c = a.get("coordinates") or {}
                put_airport(a["code"], a.get("name", a["code"]), a["country"]["name"], c.get("latitude"), c.get("longitude"))
                out.add(a["code"])
            fr_cache[code] = out
        return fr_cache[code]

    def wzr(code: str) -> set[str]:
        if not wz or code in WIZZ_SKIP:
            return set()
        return {c["iata"] for c in wz.cities.get(code, {}).get("connections", [])} - WIZZ_SKIP

    if targets:
        reach = {o: fr(o) | wzr(o) for o in origins}
        pairs: set[tuple[str, str]] = set()
        hubs: set[str] = set()
        for t in targets:
            conn = fr(t) | wzr(t)
            for o in origins:
                if o == t:
                    continue
                if t in reach[o]:
                    pairs |= {(o, t), (t, o)}
                for h in (reach[o] & conn) - {t}:
                    hubs.add(h)
                    pairs |= {(o, h), (h, o), (h, t), (t, h)}
        fr_pairs = {(a, b) for a, b in pairs if b in fr(a)}
        wz_pairs = {(a, b) for a, b in pairs if b in wzr(a)}
        print(f"Kryptys: {len(targets)}, persėdimo miestai: {len(hubs)}")
        if ry:
            ry.fetch(fr_pairs, "Ryanair")
        if wz:
            wz.fetch(wz_pairs, "Wizz")
        if ab:
            ab.fetch(ab.routes(origins, targets))
        if dy:
            dy.fetch(dy.routes(origins, targets))
        if nv:
            nv.fetch(nv.routes(origins, targets))

    # Sekami skrydžiai: tik konkretūs maršrutai ir mėnesiai / dienos
    tracked = plan.get("tracked", [])
    if tracked:
        fr_jobs = sorted({(l["from"], l["to"], dt.date.fromisoformat(l["date"]).replace(day=1))
                          for l in tracked if l.get("al") in ("FR", "R")})
        wz_jobs = sorted({(*sorted((l["from"], l["to"])), dt.date.fromisoformat(l["date"]),
                           dt.date.fromisoformat(l["date"])) for l in tracked if l.get("al") in ("W6", "W")})
        print(f"Sekami skrydžiai: {len(tracked)} (Ryanair užklausų {len(fr_jobs)}, Wizz {len(wz_jobs)})")
        if ry and fr_jobs:
            ry.extra_days |= {l["date"] for l in tracked}
            run_jobs(ry._job, fr_jobs, workers=3, label="Ryanair sekami")
        if wz and wz.base and wz_jobs:
            run_jobs(wz._job, wz_jobs, workers=2, label="Wizz sekami")
        bt_pairs = {(l["from"], l["to"]) for l in tracked if l.get("al") == "BT"}
        if ab and bt_pairs:
            ab.end = max(ab.end, max(dt.date.fromisoformat(l["date"]) for l in tracked))
            ab.fetch(bt_pairs, "airBaltic sekami")
        dy_pairs = {(l["from"], l["to"]) for l in tracked if l.get("al") == "DY"}
        if dy and dy_pairs:
            dy.end = max(dy.end, max(dt.date.fromisoformat(l["date"]) for l in tracked))
            dy.fetch(dy_pairs, "Norwegian sekami")
        nv_pairs = {(l["from"], l["to"]) for l in tracked if l.get("al") == "NOV"}
        if nv and nv_pairs:
            nv.end = max(nv.end, max(dt.date.fromisoformat(l["date"]) for l in tracked))
            nv.fetch(nv_pairs, "Novaturas sekami")



def oro_uostai(ry: "Ryanair | None", wz: "Wizz | None", origins: list[str],
               ab: "AirBaltic | None" = None, nv: "Novaturas | None" = None) -> list[dict]:
    """Visi Ryanair ir Wizz oro uostai krypčių langui programėlėje:
    k — kodas, n — pavadinimas, c — šalis, w — šilta kryptis, a — avialinijos,
    d — iš kurių išvykimo oro uostų skrendama tiesiogiai."""
    out: dict[str, dict] = {}
    def add(code, name, country, al):
        e = out.setdefault(code, {"k": code, "n": name.strip(), "c": (country or "").strip(),
                                  "w": int(is_warm(code, country or "")), "a": [], "d": []})
        if al not in e["a"]:
            e["a"].append(al)
        return e
    if ry:
        d = get_json(ry.s, "GET", "https://www.ryanair.com/api/views/locate/5/airports/en/active", log=False)
        for a in d if isinstance(d, list) else []:
            add(a["code"], a["name"], a["country"]["name"], "FR")
        for o in origins:
            r = get_json(ry.s, "GET", Ryanair.ROUTES + o, log=False)
            for x in r if isinstance(r, list) else []:
                a = x["arrivalAirport"]
                e = add(a["code"], a.get("name", a["code"]), a["country"]["name"], "FR")
                if o not in e["d"]:
                    e["d"].append(o)
    if wz:
        for code, c in wz.cities.items():
            if c.get("connections") and "All Airports" not in (c.get("shortName") or ""):
                add(code, c.get("shortName") or code, c.get("countryName"), "W6")
        for o in origins:
            if o in WIZZ_SKIP:
                continue
            for c in wz.cities.get(o, {}).get("connections", []):
                e = out.get(c["iata"])
                if e is not None and o not in e["d"]:
                    e["d"].append(o)
    if ab:
        for o in origins:
            for code, direct in ab.dests.get(o, {}).items():
                a = airports.get(code, {})
                e = add(code, a.get("n") or code, a.get("c") or "", "BT")
                if direct and o not in e["d"]:
                    e["d"].append(o)
    if nv:
        for o in origins:
            for code in nv.dests.get(o, []):
                a = airports.get(code, {})
                e = out.get(code) or add(code, a.get("n") or code, a.get("c") or "", "NOV")
                if "NOV" not in e["a"]:
                    e["a"].append("NOV")
                if o not in e["d"]:
                    e["d"].append(o)
    for o in origins:
        out.pop(o, None)
    return sorted(out.values(), key=lambda x: (x["c"], x["n"]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--is", dest="origins", nargs="+", default=DEFAULT_ORIGINS)
    ap.add_argument("--dienos", type=int, default=120, help="kiek dienų į priekį ieškoti (plačiai paieškai)")
    ap.add_argument("--be-persedimu", action="store_true", help="nerinkti antro skrydžio kainų")
    ap.add_argument("--hubai", nargs="*", default=DEFAULT_HUBS,
                    help="persėdimo miestai, iš kurių renkamos kainos į visas kryptis (plačiai paieškai)")
    ap.add_argument("--plan", type=Path, help="tikslinė paieška: JSON su targets, start, end, tracked")
    ap.add_argument("--oro-uostai", type=Path, help="kur įrašyti visų oro uostų sąrašą")
    ap.add_argument("--tik-oro-uostai", action="store_true", help="tik atnaujinti oro uostų sąrašą (be kainų)")
    ap.add_argument("--be-ryanair", action="store_true")
    ap.add_argument("--be-wizz", action="store_true")
    ap.add_argument("--be-airbaltic", action="store_true")
    ap.add_argument("--be-norwegian", action="store_true")
    ap.add_argument("--be-novaturas", action="store_true")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="kur įrašyti JSON")
    args = ap.parse_args()

    origins = [o.upper() for o in args.origins]
    today = dt.date.today()
    plan = json.loads(args.plan.read_text(encoding="utf-8")) if args.plan else None
    if plan:
        origins = [o.upper() for o in plan.get("origins") or origins]
        latest = today + dt.timedelta(days=330)
        start = max(today + dt.timedelta(days=1), dt.date.fromisoformat(plan.get("start") or str(today)))
        end = min(latest, dt.date.fromisoformat(plan.get("end") or str(latest)))
        end = max(start, end)
    else:
        start = today + dt.timedelta(days=1)
        end = start + dt.timedelta(days=args.dienos - 1)
    # Išvykimo datos — start..end, bet kainos renkamos ilgiau: grįžimo skrydžiai (iki 30 naktų)
    # ir antras persėdimo skrydis gali būti po paskutinės išvykimo dienos.
    fetch_end = end + dt.timedelta(days=33 if plan else 14)
    t0 = time.time()
    ry = None if args.be_ryanair else Ryanair(start, fetch_end)
    wz = None if args.be_wizz else Wizz(start, fetch_end)
    ab = None if args.be_airbaltic else AirBaltic(start, fetch_end)
    dy = None if args.be_norwegian else Norwegian(start, fetch_end)
    nv = None if args.be_novaturas else Novaturas(start, fetch_end)

    if args.tik_oro_uostai:
        pass
    elif plan:
        tikslinis(plan, origins, ry, wz, ab, dy, nv)
    else:
        # 1. Tiesioginiai skrydžiai iš (ir atgal į) išvykimo oro uostus
        if ry:
            ry.fetch(ry.direct_routes(origins), "Ryanair tiesioginiai")
        if wz:
            wz.fetch(wz.routes_from(set(origins), warm_only=False), "Wizz tiesioginiai")
        if ab:
            ab.fetch(ab.routes(origins), "airBaltic")
        if dy:
            dy.fetch(dy.routes(origins), "Norwegian")
        if nv:
            nv.fetch(nv.routes(origins), "Novaturas")
        # 2. Persėdimai: iš didžiųjų miestų — į visas kryptis, iš kitų — į šiltas
        if not args.be_persedimu:
            hubs = {b for (a, b), v in fares.items()
                    if a in origins and any(al not in ("BT", "DY") for per in v.values() for al in per)} - set(origins)
            full = {h.upper() for h in args.hubai} & hubs
            print(f"Persėdimo miestai: {len(hubs)}, iš jų į visas kryptis: {', '.join(sorted(full)) or '—'}")
            if ry:
                ry.fetch(ry.hub_routes(hubs, skip=set(origins), full=full), "Ryanair persėdimai")
            if wz:
                wz.fetch(wz.routes_from(hubs - full, warm_only=True, skip=set(origins))
                         | wz.routes_from(full, warm_only=False, skip=set(origins)), "Wizz persėdimai")

    if args.oro_uostai:
        args.oro_uostai.parent.mkdir(parents=True, exist_ok=True)
        args.oro_uostai.write_text(json.dumps(oro_uostai(ry, wz, sorted(set(DEFAULT_ORIGINS) | set(origins)), ab, nv), ensure_ascii=False, separators=(",", ":")),
                                   encoding="utf-8")

    if args.tik_oro_uostai:
        print(f"Oro uostų sąrašas atnaujintas → {args.oro_uostai}")
        return
    data = {
        "generated": dt.datetime.now().isoformat(timespec="minutes"),
        "start": str(start), "end": str(end), "origins": origins,
        "transfers": plan is not None or not args.be_persedimu,
        "targets": plan.get("targets", []) if plan else None,
        "airports": {k: v for k, v in airports.items() if "n" in v},
        # fares — pigiausia dienos kaina; more — kitų avialinijų tos dienos kainos
        "fares": {f"{a}-{b}": {day: min(per.values(), key=lambda f: f[0]) for day, per in sorted(v.items())}
                  for (a, b), v in sorted(fares.items()) if v},
        "more": {k: m for k, m in (
            (f"{a}-{b}", {day: sorted(per.values(), key=lambda f: f[0])[1:] for day, per in sorted(v.items()) if len(per) > 1})
            for (a, b), v in sorted(fares.items())) if m},
        "errors": errors[:100],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    n = sum(len(v) for v in data["fares"].values())
    print(f"Surinkta {n} dienos kainų, {len(data['fares'])} maršrutų per {time.time() - t0:.0f} s; "
          f"klaidų: {len(errors)} → {args.out}")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
