// Užšifruoja surinktas kainas programėlei ir patikrina išsaugotas paieškas — radus tinkamą
// skrydį, siunčia pranešimą per ntfy. Paleidžiamas GitHub Actions po kiekvieno kainų rinkimo.
//
//   node collector/tikrinti.mjs --data build/kainos.json      # nauji duomenys -> docs/data.enc + tikrinimas
//   node collector/tikrinti.mjs --data-enc build/data.enc     # tik tikrinimas su jau užšifruotais duomenimis
//   ... --dry-run                                              # nieko nesiųsti, tik parodyti
//
// Aplinkos kintamieji: SLAPTAZODIS (būtinas), APP_URL (nuoroda pranešime).
// DĖMESIO: viešos repozitorijos Actions žurnalas matomas visiems — čia spausdinami tik skaičiai.
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createEngine, d2n, route, viaOf } from "../docs/engine.js";
import { encryptJSON, decryptJSON } from "../docs/crypto.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const P = {
  dataEnc: join(ROOT, "docs", "data.enc"),
  searches: join(ROOT, "docs", "paieskos.enc"),
  state: join(ROOT, "state", "pranesimai.enc"),
};
const args = process.argv.slice(2);
const arg = n => { const i = args.indexOf(n); return i >= 0 ? args[i + 1] : null; };
const DRY = args.includes("--dry-run");
const PASS = process.env.SLAPTAZODIS;
const APP_URL = process.env.APP_URL || "";
if (!PASS) { console.error("Trūksta SLAPTAZODIS"); process.exit(1); }

const MON = ["saus.", "vas.", "kov.", "bal.", "geg.", "birž.", "liep.", "rugp.", "rugs.", "spal.", "lapkr.", "gruod."];
const fmt = n => { const d = new Date(n * 86400000); return `${MON[d.getUTCMonth()]} ${d.getUTCDate()}`; };
const eur = v => "€" + (v >= 100 ? Math.round(v) : v.toFixed(2).replace(/\.00$/, ""));

async function main() {
  // 1. Duomenys
  let DATA;
  if (arg("--data")) {
    DATA = JSON.parse(await readFile(arg("--data"), "utf8"));
    await writeFile(P.dataEnc, await encryptJSON(DATA, PASS));
    console.log(`Užšifruota: ${Object.keys(DATA.fares).length} maršrutų -> docs/data.enc`);
  } else if (arg("--data-enc")) {
    DATA = await decryptJSON(await readFile(arg("--data-enc"), "utf8"), PASS);
  } else {
    console.error("Nurodyk --data arba --data-enc"); process.exit(1);
  }

  // 2. Išsaugotos paieškos
  if (!existsSync(P.searches)) { console.log("Išsaugotų paieškų nėra."); return; }
  const store = await decryptJSON(await readFile(P.searches, "utf8"), PASS);
  const topic = store.settings && store.settings.ntfy;
  const prevState = existsSync(P.state) ? await decryptJSON(await readFile(P.state, "utf8"), PASS) : {};
  const state = {};
  const E = createEngine(DATA);
  let sent = 0, checked = 0;

  for (const s of store.searches || []) {
    if (!s.notify || s.S.maxp == null || s.S.maxp === "") continue;
    if (!DATA.origins.includes(s.S.origin)) continue;
    checked++;
    const prev = prevState[s.id] || {};
    const rows = E.compute(s.S); // maxp jau pritaikytas
    const fresh = rows.filter(r => prev[r.dest] == null || r.best.total < prev[r.dest] - 0.5);
    // Būsena: paskutinė pranešta kaina kiekvienai vis dar tinkančiai krypčiai
    state[s.id] = Object.fromEntries(rows.map(r => {
      const isNew = fresh.includes(r);
      return [r.dest, isNew ? r.best.total : prev[r.dest]];
    }));
    if (!fresh.length) continue;

    const lines = fresh.slice(0, 6).map(r => {
      const b = r.best, via = [viaOf(b.o), b.b && viaOf(b.b)].filter(Boolean);
      const when = s.S.mode === "ow" ? fmt(b.d) : `${fmt(b.d)} → ${fmt(b.r)} (${b.r - b.o.arrDay} n.)`;
      return `${E.apName(r.dest)} ${eur(b.total)} · ${when}${via.length ? ` · per ${[...new Set(via)].join(", ")}` : ""}`;
    });
    if (fresh.length > 6) lines.push(`… ir dar ${fresh.length - 6}`);
    const msg = {
      topic, title: `✈ ${s.name}: nuo ${eur(fresh[0].best.total)}`, message: lines.join("\n"),
      tags: ["airplane"], priority: 4,
      ...(APP_URL ? { click: `${APP_URL}#s=${encodeURIComponent(s.id)}` } : {}),
    };
    if (DRY || !topic) {
      console.log(DRY ? "[dry-run]" : "[ntfy tema nenustatyta]", JSON.stringify(msg, null, 1));
    } else {
      const res = await fetch("https://ntfy.sh/", { method: "POST", body: JSON.stringify(msg),
        headers: { "Content-Type": "application/json" } });
      if (!res.ok) console.error(`ntfy klaida: HTTP ${res.status}`);
      else sent++;
    }
  }
  console.log(`Patikrinta paieškų: ${checked}, išsiųsta pranešimų: ${sent}`);

  // 3. Būsena (rašoma tik jei pasikeitė, kad nebūtų tuščių commit'ų)
  if (!DRY && JSON.stringify(state) !== JSON.stringify(prevState)) {
    await mkdir(dirname(P.state), { recursive: true });
    await writeFile(P.state, await encryptJSON(state, PASS));
    console.log("Būsena atnaujinta.");
  }
}

main().catch(e => { console.error(e.message === "BAD_PASSWORD" ? "Neteisingas slaptažodis" : e); process.exit(1); });
