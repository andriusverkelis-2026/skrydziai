// Bendra skrydžių paieškos logika: naudoja ir programėlė (naršyklėje), ir pranešimų tikrinimas (Node).
// Kaina f = [kaina EUR, avialinijos kodas, išvykimas "HH:MM", atvykimas min. nuo išvykimo dienos
//            vidurnakčio (atvykimo vietos laiku) arba null, 1 jei atvykimas apskaičiuotas]

export const DAY = 86400000;
export const d2n = s => Math.round(Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)) / DAY);
export const n2d = n => new Date(n * DAY).toISOString().slice(0, 10);
export const weekday = n => new Date(n * DAY).getUTCDay(); // 0 = sekmadienis
export const hm = s => s ? (+s.slice(0, 2)) * 60 + (+s.slice(3, 5)) : null;

export const AIRLINES = {
  FR: "Ryanair", W6: "Wizz Air", BT: "airBaltic", AY: "Finnair", TK: "Turkish Airlines",
  LO: "LOT", LH: "Lufthansa", R: "Ryanair", W: "Wizz Air",
};
export const alName = c => AIRLINES[c] || c;

export const DEFAULT_LAY = [3, 24]; // persėdimo laukimas, val. (nustatomas filtruose)

export function bookUrl(al, from, to, n) {
  const d = n2d(n);
  if (al === "FR" || al === "R")
    return `https://www.ryanair.com/gb/en/trip/flights/select?adults=1&teens=0&children=0&infants=0&dateOut=${d}&dateIn=&isConnectedFlight=false&isReturn=false&discount=0&originIata=${from}&destinationIata=${to}`;
  if (al === "W6" || al === "W")
    return `https://wizzair.com/en-gb/booking/select-flight/${from}/${to}/${d}/null/1/0/0/null`;
  if (al === "BT")
    return `https://www.airbaltic.com/en/book-flight?originType=A&originCode=${from}&destinType=A&destinCode=${to}&tripType=oneway&departure=${d}&numAdt=1&numChd=0&numInf=0`;
  return `https://www.google.com/travel/flights?q=${encodeURIComponent(`flights from ${from} to ${to} on ${d} one way`)}`;
}

export function defaultSearch(DATA) {
  return {
    origin: DATA.origins[0], mode: "rt", tr: DATA.transfers ? 1 : 0, nmin: 5, nmax: 10,
    dfrom: "", dto: "", outDays: [], backDays: [], outTime: [0, 24], backTime: [0, 24],
    maxp: null, q: "", warm: false, sort: "p", airlines: [], lay: [...DEFAULT_LAY],
    origins: [...DATA.origins], only: [],
  };
}

export function createEngine(DATA) {
  const fares = {};
  for (const [k, v] of Object.entries(DATA.fares)) {
    const [a, b] = k.split("-");
    const m = new Map(), more = (DATA.more || {})[k] || {};
    // Kiekvienai dienai — visų avialinijų kainos (pirma pigiausia)
    for (const [d, f] of Object.entries(v)) m.set(d2n(d), [f, ...(more[d] || [])]);
    (fares[a] ||= {})[b] = m;
  }
  const pick = (fs, ok) => { let b = null; for (const f of fs || []) if (ok(f) && (!b || f[0] < b[0])) b = f; return b; };
  const START = d2n(DATA.start), END = d2n(DATA.end);
  const ap = c => DATA.airports[c] || {};
  const apName = c => (ap(c).n || c).trim();
  const apCountry = c => (ap(c).c || "").trim();
  const isWarm = c => !!ap(c).w;
  const airlinesPresent = [...new Set([...Object.values(DATA.fares).flatMap(v => Object.values(v).map(f => f[1])),
    ...Object.values(DATA.more || {}).flatMap(v => Object.values(v).flat().map(f => f[1]))])];

  const legArr = l => l.f[3] == null ? null : l.d * 1440 + l.f[3];
  const mkIt = legs => {
    const last = legs[legs.length - 1], a = legArr(last);
    return { p: legs.reduce((s, l) => s + l.f[0], 0), legs, arrDay: a == null ? last.d : Math.floor(a / 1440) };
  };
  const add = (store, T, d, it) => {
    const m = store[T] ||= new Map(); const c = m.get(d);
    if (!c || it.p < c.p) m.set(d, it);
  };
  const inTime = (f, win) => {
    if (!win || (win[0] <= 0 && win[1] >= 24)) return true;
    const t = hm(f[2]); if (t == null) return false;
    return t >= win[0] * 60 && t <= win[1] * 60;
  };

  // Maršrutai iš O: out[T] = Map(išvykimo diena -> pigiausias maršrutas), back[T] = Map(išskridimo iš T diena -> ...)
  const cache = new Map();
  function itins(S) {
    const als = S.airlines && S.airlines.length ? new Set(S.airlines) : null;
    const lay = S.lay || DEFAULT_LAY, minGap = lay[0] * 60, maxGap = lay[1] * 60;
    const ck = JSON.stringify([S.origin, S.tr, S.outTime, S.backTime, S.airlines, lay]);
    if (cache.has(ck)) return cache.get(ck);
    const O = S.origin, out = {}, back = {};
    const alOk = f => (!als || als.has(f[1])) && (S.tr || !f[5]);
    const connect = (store, key, legA, m2, from2, to2) => {
      const a1 = legArr(legA); if (a1 == null) return;
      const last = Math.floor((a1 + maxGap) / 1440);
      for (let d2 = Math.floor((a1 + minGap) / 1440); d2 <= last; d2++) {
        for (const f2 of m2.get(d2) || []) {
          if (!f2[2] || !alOk(f2)) continue;
          const gap = d2 * 1440 + hm(f2[2]) - a1;
          if (gap >= minGap && gap <= maxGap) add(store, key, legA.d, mkIt([legA, { from: from2, to: to2, d: d2, f: f2 }]));
        }
      }
    };
    for (const [T, m] of Object.entries(fares[O] || {}))
      for (const [d, fs] of m) { const f = pick(fs, f => alOk(f) && inTime(f, S.outTime)); if (f) add(out, T, d, mkIt([{ from: O, to: T, d, f }])); }
    for (const T of Object.keys(fares)) {
      const m = fares[T][O]; if (!m || T === O) continue;
      for (const [d, fs] of m) { const f = pick(fs, f => alOk(f) && inTime(f, S.backTime)); if (f) add(back, T, d, mkIt([{ from: T, to: O, d, f }])); }
    }
    if (S.tr) {
      for (const [H, m1] of Object.entries(fares[O] || {}))
        for (const [T, m2] of Object.entries(fares[H] || {})) {
          if (T === O || T === H) continue;
          for (const [d, fs] of m1) for (const f1 of fs) if (alOk(f1) && inTime(f1, S.outTime)) connect(out, T, { from: O, to: H, d, f: f1 }, m2, H, T);
        }
      for (const H of Object.keys(fares)) {
        const m2 = fares[H][O]; if (!m2 || H === O) continue;
        for (const T of Object.keys(fares)) {
          if (T === O || T === H) continue;
          const m1 = fares[T][H]; if (!m1) continue;
          for (const [d, fs] of m1) for (const f1 of fs) if (alOk(f1) && inTime(f1, S.backTime)) connect(back, T, { from: T, to: H, d, f: f1 }, m2, H, O);
        }
      }
    }
    const r = { out, back };
    if (cache.size > 30) cache.clear();
    cache.set(ck, r);
    return r;
  }

  const range = S => {
    let a = S.dfrom ? Math.max(START, d2n(S.dfrom)) : START;
    let b = S.dto ? Math.min(END, d2n(S.dto)) : END;
    return [a, Math.max(a, b)];
  };

  // Kiekvienai išvykimo dienai — pigiausias variantas pagal filtrus
  function analyse(S, I, dest) {
    const out = I.out[dest], back = I.back[dest];
    const [a, b] = range(S);
    const od = S.outDays && S.outDays.length ? new Set(S.outDays) : null;
    const bd = S.backDays && S.backDays.length ? new Set(S.backDays) : null;
    const days = []; let best = null;
    for (let d = a; d <= b; d++) {
      if (od && !od.has(weekday(d))) continue;
      const o = out.get(d); if (!o) continue;
      let e = null;
      if (S.mode === "ow") e = { d, total: o.p, o };
      else if (back) {
        for (let n = S.nmin; n <= S.nmax; n++) {
          const r = o.arrDay + n;
          if (bd && !bd.has(weekday(r))) continue;
          const bb = back.get(r); if (!bb) continue;
          const t = o.p + bb.p;
          if (!e || t < e.total) e = { d, r, total: t, o, b: bb };
        }
      }
      if (e) { days.push(e); if (!best || e.total < best.total) best = e; }
    }
    return { dest, out, back, days, best };
  }

  function compute(S) {
    const I = itins(S);
    const q = (S.q || "").trim().toLowerCase();
    const rows = [];
    for (const dest of Object.keys(I.out)) {
      if (DATA.origins.includes(dest)) continue;
      if (S.only && S.only.length && !S.only.includes(dest)) continue;
      if (S.warm && !isWarm(dest)) continue;
      if (q && !(`${apName(dest)} ${apCountry(dest)} ${dest}`.toLowerCase().includes(q))) continue;
      const a = analyse(S, I, dest);
      if (!a.best) continue;
      if (S.maxp != null && S.maxp !== "" && a.best.total > +S.maxp) continue;
      rows.push(a);
    }
    rows.sort(S.sort === "n" ? (x, y) => apName(x.dest).localeCompare(apName(y.dest), "lt")
      : (x, y) => x.best.total - y.best.total);
    return rows;
  }

  // Iš kelių išvykimo oro uostų: eilutės turi .origin
  function computeMulti(S) {
    const os = (S.origins && S.origins.length ? S.origins : DATA.origins).filter(o => fares[o]);
    const rows = os.flatMap(o => compute({ ...S, origin: o }).map(r => ({ ...r, origin: o })));
    rows.sort(S.sort === "n" ? (x, y) => apName(x.dest).localeCompare(apName(y.dest), "lt") || x.best.total - y.best.total
      : (x, y) => x.best.total - y.best.total);
    return rows;
  }

  // Sekamo skrydžio dabartinė kaina: legs = [{from, to, date, al}] -> {p, legs: [f...]} arba null (nebėra)
  function trackedPrice(legs) {
    // Pirmiausia tos pačios avialinijos kaina; jei jos nebėra — pigiausia tos dienos
    const fs = legs.map(l => { const all = (fares[l.from] || {})[l.to]?.get(d2n(l.date)); return all && (pick(all, f => f[1] === l.al) || pick(all, () => true)); });
    if (fs.some(f => !f)) return null;
    return { p: Math.round(fs.reduce((s, f) => s + f[0], 0) * 100) / 100, fs };
  }

  return { DATA, fares, START, END, range, apName, apCountry, isWarm, airlinesPresent, legArr, itins, analyse, compute, computeMulti, trackedPrice };
}

export const route = it => [it.legs[0].from, ...it.legs.map(l => l.to)].join("→");
export const viaOf = it => it.legs.length > 1 ? it.legs[0].to : null;
