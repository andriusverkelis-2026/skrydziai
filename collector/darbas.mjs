// Debesies darbo eiga (GitHub Actions). Du žingsniai aplink Python rinkiklį:
//
//   node collector/darbas.mjs planas   — paruošia build/plan.json (ką rinkti) pagal VEIKSMAS:
//        kryptis  — vienkartinė paieška kryptimi (užšifruota užklausa iš programėlės, env UZKLAUSA)
//        tikrinti — išsaugotos kryptys + sekami skrydžiai (kas 6 val.)
//        visi     — plati paieška visomis kryptimis (plano nereikia)
//   node collector/darbas.mjs po       — užšifruoja rezultatą į data/ (šaka „duomenys“), tikrina
//        išsaugotas kryptis ir sekamus skrydžius, siunčia ntfy pranešimus, saugo būseną.
//
// Aplinkos kintamieji: SLAPTAZODIS, VEIKSMAS, UZKLAUSA, APP_URL. ROOT/data — šakos „duomenys“ kopija.
// DĖMESIO: viešos repozitorijos Actions žurnalas matomas visiems — čia spausdinami tik skaičiai.
import { readFile, writeFile, mkdir, readdir, rm } from "node:fs/promises";
import { existsSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { createEngine, defaultSearch, alName, d2n, viaOf } from "../docs/engine.js";
import { encryptJSON, decryptJSON } from "../docs/crypto.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const P = {
  store: join(ROOT, "docs", "paieskos.enc"),
  plan: join(ROOT, "build", "plan.json"),
  req: join(ROOT, "build", "uzklausa.json"),
  data: join(ROOT, "build", "kainos.json"),
  out: join(ROOT, "data"),
};
const PASS = process.env.SLAPTAZODIS;
const VEIKSMAS = process.env.VEIKSMAS || "tikrinti";
const APP_URL = process.env.APP_URL || "";
const DRY = process.argv.includes("--dry-run");
if (!PASS) { console.error("Trūksta SLAPTAZODIS"); process.exit(1); }

const MON = ["saus.", "vas.", "kov.", "bal.", "geg.", "birž.", "liep.", "rugp.", "rugs.", "spal.", "lapkr.", "gruod."];
const fmt = n => { const d = new Date(n * 86400000); return `${MON[d.getUTCMonth()]} ${d.getUTCDate()}`; };
const eur = v => "€" + (v >= 100 ? Math.round(v) : v.toFixed(2).replace(/\.00$/, ""));
const today = () => Math.floor(Date.now() / 86400000);
const readEnc = async f => decryptJSON(await readFile(f, "utf8"), PASS);
// Ką šis paleidimas įrašė / ištrynė šakoje „duomenys“ — išsaugojant perkeliami TIK šie failai,
// kad lygiagrečiai vykstantys paleidimai neperrašytų vienas kito rezultatų.
const WRITTEN = [], DELETED = [];
const writeEnc = async (f, obj) => {
  await mkdir(dirname(f), { recursive: true }); await writeFile(f, await encryptJSON(obj, PASS));
  WRITTEN.push(relative(P.out, f).replaceAll("\\", "/"));
};
async function saveManifest() {
  await mkdir(dirname(P.plan), { recursive: true });
  await writeFile(join(ROOT, "build", "rasyti.txt"), WRITTEN.map(f => f + "\n").join(""));
  await writeFile(join(ROOT, "build", "trinti.txt"), DELETED.map(f => f + "\n").join(""));
}
const loadStore = async () => existsSync(P.store) ? readEnc(P.store) : { settings: {}, directions: [], tracked: [] };

// ---------------------------------------------------------------- 1. planas
async function planas() {
  await mkdir(dirname(P.plan), { recursive: true });
  if (VEIKSMAS === "visi") { console.log("Plati paieška — plano nereikia."); return; }
  if (VEIKSMAS === "kryptis") {
    const req = await decryptJSON(process.env.UZKLAUSA, PASS);
    await writeFile(P.req, JSON.stringify(req));
    await writeFile(P.plan, JSON.stringify({ targets: req.targets, start: req.start, end: req.end, origins: req.origins }));
    console.log(`Paieška kryptimi: ${req.targets.length} oro uostai.`);
    return;
  }
  const store = await loadStore();
  const dirs = store.directions || [];
  const tracked = (store.tracked || []).filter(t => d2n(t.legs[0].date) >= today());
  if (!dirs.length && !tracked.length) { console.log("Nėra ką tikrinti."); return; }
  const starts = dirs.map(d => d.start).filter(Boolean), ends = dirs.map(d => d.end).filter(Boolean);
  await writeFile(P.plan, JSON.stringify({
    targets: [...new Set(dirs.flatMap(d => d.targets))],
    start: starts.sort()[0], end: ends.sort().at(-1),
    tracked: tracked.flatMap(t => t.legs),
  }));
  console.log(`Tikrinama: ${dirs.length} kryptys, ${tracked.length} sekami skrydžiai.`);
}

// ---------------------------------------------------------------- 2. po rinkimo
async function po() {
  const store = await loadStore();
  const topic = store.settings && store.settings.ntfy;
  let sent = 0;
  async function send(msg) {
    if (DRY || !topic) { console.log(DRY ? "[dry-run]" : "[ntfy tema nenustatyta]", JSON.stringify(msg, null, 1)); return; }
    const res = await fetch("https://ntfy.sh/", { method: "POST", body: JSON.stringify({ topic, ...msg }),
      headers: { "Content-Type": "application/json" } });
    if (!res.ok) console.error(`ntfy klaida: HTTP ${res.status}`); else sent++;
  }
  const line = (E, r, S) => {
    const b = r.best, via = [viaOf(b.o), b.b && viaOf(b.b)].filter(Boolean);
    const when = S.mode === "ow" ? fmt(b.d) : `${fmt(b.d)} → ${fmt(b.r)} (${b.r - b.o.arrDay} n.)`;
    return `${r.origin}→${E.apName(r.dest)} ${eur(b.total)} · ${when}${via.length ? ` · per ${[...new Set(via)].join(", ")}` : ""}`;
  };

  if (!existsSync(P.data)) { console.log("Naujų duomenų nėra."); return; }
  const DATA = JSON.parse(await readFile(P.data, "utf8"));
  const E = createEngine(DATA);

  if (VEIKSMAS === "visi") {
    await writeEnc(join(P.out, "visi.enc"), DATA);
    await send({ title: "✈ Plati paieška baigta", message: `${Object.keys(DATA.fares).length} maršrutų. Atidaryk programėlę → „Visur“.`,
      tags: ["airplane"], ...(APP_URL ? { click: `${APP_URL}#visur` } : {}) });
  } else if (VEIKSMAS === "kryptis") {
    const req = JSON.parse(await readFile(P.req, "utf8"));
    await writeEnc(join(P.out, "r", `${req.id}.enc`), DATA);
    const S = { ...defaultSearch(DATA), ...req.S, only: req.targets, maxp: null };
    const rows = E.computeMulti(S);
    await send({
      title: `✈ Paieška baigta: ${req.name || req.targets.join(", ")}`,
      message: rows.length ? rows.slice(0, 4).map(r => line(E, r, S)).join("\n") : "Pagal nustatymus skrydžių nerasta.",
      tags: ["mag"], ...(APP_URL ? { click: `${APP_URL}#r=${req.id}` } : {}),
    });
    // Paliekam 20 naujausių vienkartinių rezultatų
    const dir = join(P.out, "r");
    const files = (await readdir(dir)).filter(f => f.endsWith(".enc")).sort((a, b) => parseInt(b, 36) - parseInt(a, 36));
    for (const f of files.slice(20)) { await rm(join(dir, f)); DELETED.push(`r/${f}`); }
  } else {
    await writeEnc(join(P.out, "sekami.enc"), DATA);
    const statePath = join(P.out, "state.enc");
    const prev = existsSync(statePath) ? await readEnc(statePath) : { directions: {}, tracked: {} };
    const state = { directions: {}, tracked: {} };

    // Išsaugotos kryptys: pranešti apie naujus tinkamus variantus arba nukritusią kainą
    for (const d of store.directions || []) {
      const S = { ...defaultSearch(DATA), ...d.S, only: d.targets };
      if (!d.notify || S.maxp == null || S.maxp === "") continue;
      const old = (prev.directions || {})[d.id] || {};
      const rows = E.computeMulti(S);
      const key = r => `${r.origin}-${r.dest}`;
      const fresh = rows.filter(r => old[key(r)] == null || r.best.total < old[key(r)] - 0.5);
      state.directions[d.id] = Object.fromEntries(rows.map(r => [key(r), fresh.includes(r) ? r.best.total : old[key(r)]]));
      if (fresh.length) await send({
        title: `✈ ${d.name}: nuo ${eur(fresh[0].best.total)}`, priority: 4, tags: ["airplane"],
        message: fresh.slice(0, 5).map(r => line(E, r, S)).join("\n") + (fresh.length > 5 ? `\n… ir dar ${fresh.length - 5}` : ""),
        ...(APP_URL ? { click: `${APP_URL}#d=${encodeURIComponent(d.id)}` } : {}),
      });
    }

    // Sekami skrydžiai: pranešti apie kiekvieną kainos pokytį (≥ €1) ir jei skrydžio nebeliko
    const click = APP_URL ? { click: `${APP_URL}#saved` } : {};
    for (const t of store.tracked || []) {
      if (d2n(t.legs[0].date) < today()) continue;
      const last = (prev.tracked || {})[t.id] ?? t.p0;
      const now = E.trackedPrice(t.legs);
      if (!now) {
        state.tracked[t.id] = "gone";
        if (last !== "gone") await send({ title: `✈ Kainos nebėra: ${t.label}`, tags: ["warning"], priority: 4, ...click,
          message: "Šiai dienai skrydžio kainos neberandame — gal išparduota arba reisas atšauktas." });
        continue;
      }
      state.tracked[t.id] = now.p;
      if (last === "gone" || Math.abs(now.p - last) >= 1) {
        const down = last !== "gone" && now.p < last;
        const other = now.fs.some((f, i) => f[1] !== t.legs[i].al)
          ? ` (dabar pigiausia — ${[...new Set(now.fs.map(f => alName(f[1])))].join(", ")})` : "";
        await send({
          title: last === "gone" ? `✈ Kaina vėl yra: ${eur(now.p)}` : `✈ Kaina ${down ? "sumažėjo" : "padidėjo"}: ${eur(last)} → ${eur(now.p)}`,
          message: `${t.label}${other}\nKai pradėjai sekti, kaina buvo ${eur(t.p0)}.`,
          tags: [down ? "chart_with_downwards_trend" : "chart_with_upwards_trend"], priority: down ? 4 : 3, ...click,
        });
      }
    }
    if (!DRY) await writeEnc(statePath, state);
  }
  console.log(`Veiksmas: ${VEIKSMAS}, išsiųsta pranešimų: ${sent}`);
}

const cmd = process.argv[2];
(cmd === "planas" ? planas() : cmd === "po" ? po().finally(saveManifest) : Promise.reject(new Error("Komanda: planas | po")))
  .catch(e => { console.error(e.message === "BAD_PASSWORD" ? "Neteisingas slaptažodis" : e); process.exit(1); });
