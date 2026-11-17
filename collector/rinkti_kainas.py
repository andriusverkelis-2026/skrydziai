"""Surenka pigiausias Ryanair ir Wizz Air kainas iš pasirinktų oro uostų į visas kryptis
(ir, jei neišjungta, antro skrydžio kainas iš persėdimo miestų į šiltas / egzotines kryptis)
ir įrašo jas į JSON failą. Programėlė (docs/) ir pranešimų tikrinimas (tikrinti.mjs) skaito tą failą.

Naudojimas:
    python collector/rinkti_kainas.py                       # VNO KUN RIX WAW WMI, 120 d., su persėdimais
    python collector/rinkti_kainas.py --is VNO KUN --dienos 60
    python collector/rinkti_kainas.py --be-persedimu        # tik tiesioginiai (~9 min vietoj ~22)

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
# fares[(from, to)][date] = [kaina, avialinija, išvykimo "HH:MM", atvykimas min. nuo išvykimo dienos
#                            vidurnakčio (atvykimo vietos laiku) arba None, 1 jei atvykimas apskaičiuotas]
fares: dict[tuple[str, str], dict[str, list]] = {}
airports: dict[str, dict] = {}
errors: list[str] = []


def is_warm(code: str, country: str) -> bool:
    return code in WARM_AIRPORTS or country in WARM_COUNTRIES


def put_fare(frm: str, to: str, day: str, price: float, airline: str, hhmm: str,
             arr_min: int | None, est: int) -> None:
    with _lock:
        d = fares.setdefault((frm, to), {})
        cur = d.get(day)
        if cur is None or price < cur[0]:
            d[day] = [round(price, 2), airline, hhmm, arr_min, est]


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
             pause: float = 0.0, log: bool = True, **kw):
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
                return r.json()
            if r.status_code in (400, 404):
                return None  # tokio maršruto / periodo nėra
            err = f"HTTP {r.status_code}"
            if r.status_code in (429, 503):
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

    def warm_routes_from(self, hubs: set[str], skip: set[str]) -> set[tuple[str, str]]:
        """Maršrutai iš persėdimo miestų į šiltas kryptis (ir atgal)."""
        def one(h):
            d = get_json(self.s, "GET", self.ROUTES + h, log=False)
            out = set()
            for x in d if isinstance(d, list) else []:
                a = x["arrivalAirport"]
                code, country = a["code"], a["country"]["name"]
                c = a.get("coordinates") or {}
                put_airport(code, a.get("name", code), country, c.get("latitude"), c.get("longitude"))
                if is_warm(code, country) and code not in skip:
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
        run_jobs(self._job, jobs, workers=6, label=label)

    def _job(self, a: str, b: str, m: dt.date) -> None:
        d = get_json(self.s, "GET", f"{self.API}/oneWayFares/{a}/{b}/cheapestPerDay",
                     params={"outboundMonthOfDate": str(m), "currency": "EUR"})
        if d is FAIL:
            return
        for f in ((d or {}).get("outbound") or {}).get("fares", []):
            if f.get("unavailable") or f.get("soldOut") or not f.get("price"):
                continue
            day = f["day"]
            if not (str(self.start) <= day <= str(self.end)):
                continue
            dep, arr = f.get("departureDate") or "", f.get("arrivalDate") or ""
            arr_min = None
            if arr:
                days = (dt.date.fromisoformat(arr[:10]) - dt.date.fromisoformat(day)).days
                arr_min = days * 1440 + int(arr[11:13]) * 60 + int(arr[14:16])
            put_fare(a, b, day, f["price"]["value"], "FR", dep[11:16], arr_min, 0)


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--is", dest="origins", nargs="+", default=DEFAULT_ORIGINS)
    ap.add_argument("--dienos", type=int, default=120, help="kiek dienų į priekį ieškoti")
    ap.add_argument("--be-persedimu", action="store_true", help="nerinkti antro skrydžio kainų")
    ap.add_argument("--be-ryanair", action="store_true")
    ap.add_argument("--be-wizz", action="store_true")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="kur įrašyti JSON")
    args = ap.parse_args()

    origins = [o.upper() for o in args.origins]
    start = dt.date.today() + dt.timedelta(days=1)
    end = start + dt.timedelta(days=args.dienos - 1)
    t0 = time.time()
    ry = None if args.be_ryanair else Ryanair(start, end)
    wz = None if args.be_wizz else Wizz(start, end)

    # 1. Tiesioginiai skrydžiai iš (ir atgal į) išvykimo oro uostus
    if ry:
        ry.fetch(ry.direct_routes(origins), "Ryanair tiesioginiai")
    if wz:
        wz.fetch(wz.routes_from(set(origins), warm_only=False), "Wizz tiesioginiai")

    # 2. Persėdimai: iš kiekvieno pasiekiamo miesto — į šiltas kryptis ir atgal
    if not args.be_persedimu:
        hubs = {b for (a, b) in fares if a in origins} - set(origins)
        print(f"Persėdimo miestai: {len(hubs)}")
        if ry:
            ry.fetch(ry.warm_routes_from(hubs, skip=set(origins)), "Ryanair persėdimai")
        if wz:
            wz.fetch(wz.routes_from(hubs, warm_only=True, skip=set(origins)), "Wizz persėdimai")

    data = {
        "generated": dt.datetime.now().isoformat(timespec="minutes"),
        "start": str(start), "end": str(end), "origins": origins,
        "transfers": not args.be_persedimu,
        "airports": {k: v for k, v in airports.items() if "n" in v},
        "fares": {f"{a}-{b}": dict(sorted(v.items())) for (a, b), v in sorted(fares.items()) if v},
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
