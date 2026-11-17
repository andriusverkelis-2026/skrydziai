# Pigūs skrydžiai

Asmeninė programėlė pigiems skrydžiams rasti: kainų diagramos ir datų tinkleliai visoms kryptims iš
pasirinkto oro uosto, skrydžiai su persėdimu į šiltas kryptis, filtrai (savaitės dienos, skrydžio laikas,
kainos riba, avialinijos), išsaugotos paieškos ir pranešimai per [ntfy](https://ntfy.sh).

- `collector/rinkti_kainas.py`: surenka Ryanair ir Wizz Air kainas (neoficialūs svetainių API).
- `collector/tikrinti.mjs`: užšifruoja kainas programėlei ir tikrina išsaugotas paieškas.
- `docs/`: programėlė (GitHub Pages). `engine.js` turi paieškos logiką, kurią naudoja ir naršyklė, ir tikrinimas.
- `.github/workflows/tikrinti.yml`: paleidžia rinkimą 2 kartus per dieną ir iškart po paieškos išsaugojimo.

Kainos, paieškos ir būsena repozitorijoje saugomos tik užšifruotos (AES-GCM, raktas gaunamas iš
slaptažodžio, kuris laikomas `SLAPTAZODIS` secret'e).
