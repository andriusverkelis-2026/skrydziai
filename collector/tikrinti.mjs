// Užšifruoja surinktas kainas programėlei, tikrina išsaugotas paieškas ir sekamus skrydžius —
// radus tinkamą skrydį ar pasikeitus sekamo skrydžio kainai, siunčia pranešimą per ntfy.
// Paleidžiamas GitHub Actions po kiekvieno kainų rinkimo ir po kiekvieno paieškų pakeitimo.
//
//   node collector/tikrinti.mjs --data build/kainos.json      # nauji duomenys -> docs/data.enc + tikrinimas
//   node collector/tikrinti.mjs --data-enc docs/data.enc      # tik tikrinimas su jau užšifruotais duomenimis
//   ... --dry-run                                              # nieko nesiųsti, tik parodyti
//
// Aplinkos kintamieji: SLAPTAZODIS (būtinas), APP_URL (nuoroda pranešime).
// DĖMESIO: viešos repozitorijos Actions žurnalas matomas visiems — čia spausdinami tik skaičiai.
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createEngine, defaultSearch, alName, d2n, viaOf } from "../docs/engine.js";
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

  // 2. Išsaugotos paieškos ir sekami skrydžiai
  if (!existsSync(P.searches)) { console.log("Išsaugotų paieškų nėra."); return; }
  const store = await decryptJSON(await readFile(P.searches, "utf8"), PASS);
  const topic = store.settings && store.settings.ntfy;
  let prev = existsSync(P.state) ? await decryptJSON(await readFile(P.state, "utf8"), PASS) : {};
  if (!prev.searches) prev = { searches: prev, tracked: {} }; // senas būsenos formatas
  const state = { searches: {}, tracked: {} };
  const E = createEngine(DATA);
  let sent = 0, checked = 0, tracked = 0;

  async function send(msg) {
    if (DRY || !topic) { console.log(DRY ? "[dry-run]" : "[ntfy tema nenustatyta]", JSON.stringify(msg, null, 1)); return; }
    const res = await fetch("https://ntfy.sh/", { method: "POST", body: JSON.stringify({ topic, ...msg }),
      headers: { "Content-Type": "application/json" } });
    if (!res.ok) console.error(`ntfy klaida: HTTP ${res.status}`); else sent++;
  }

  // 2a. Paieškos: pranešti apie naujas tinkamas kryptis arba nukritusią kainą
  for (const s0 of store.searches || []) {
    const S = { ...defaultSearch(DATA), ...s0.S };
    if (!s0.notify || S.maxp == null || S.maxp === "") continue;
    if (!DATA.origins.includes(S.origin)) continue;
    checked++;
    const old = prev.searches[s0.id] || {};
    const rows = E.compute(S); // maxp jau pritaikytas
    const fresh = rows.filter(r => old[r.dest] == null || r.best.total < old[r.dest] - 0.5);
    state.searches[s0.id] = Object.fromEntries(rows.map(r => [r.dest, fresh.includes(r) ? r.best.total : old[r.dest]]));
    if (!fresh.length) continue;
    const lines = fresh.slice(0, 6).map(r => {
      const b = r.best, via = [viaOf(b.o), b.b && viaOf(b.b)].filter(Boolean);
      const when = S.mode === "ow" ? fmt(b.d) : `${fmt(b.d)} → ${fmt(b.r)} (${b.r - b.o.arrDay} n.)`;
      return `${E.apName(r.dest)} ${eur(b.total)} · ${when}${via.length ? ` · per ${[...new Set(via)].join(", ")}` : ""}`;
    });
    if (fresh.length > 6) lines.push(`… ir dar ${fresh.length - 6}`);
    await send({
      title: `✈ ${s0.name}: nuo ${eur(fresh[0].best.total)}`, message: lines.join("\n"),
      tags: ["airplane"], priority: 4,
      ...(APP_URL ? { click: `${APP_URL}#s=${encodeURIComponent(s0.id)}` } : {}),
    });
  }

  // 2b. Sekami skrydžiai: pranešti apie kiekvieną kainos pokytį (≥ €1) ir jei skrydžio nebeliko
  const today = Math.floor(Date.now() / 86400000);
  const click = APP_URL ? { click: `${APP_URL}#saved` } : {};
  for (const t of store.tracked || []) {
    if (d2n(t.legs[0].date) < today) continue; // jau įvyko
    tracked++;
    const last = prev.tracked[t.id] ?? t.p0;
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
  console.log(`Patikrinta paieškų: ${checked}, sekamų skrydžių: ${tracked}, išsiųsta pranešimų: ${sent}`);

  // 3. Būsena (rašoma tik jei pasikeitė, kad nebūtų tuščių commit'ų)
  if (!DRY && JSON.stringify(state) !== JSON.stringify(prev)) {
    await mkdir(dirname(P.state), { recursive: true });
    await writeFile(P.state, await encryptJSON(state, PASS));
    console.log("Būsena atnaujinta.");
  }
}

main().catch(e => { console.error(e.message === "BAD_PASSWORD" ? "Neteisingas slaptažodis" : e); process.exit(1); });
