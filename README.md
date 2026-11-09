# Dosije screener

Svako jutro oko 05:00 (po Sarajevu) pregleda utakmice u narednih 24 sata u ligama iz `config.json` i traži preokrete poluvrijeme/kraj (1/2, 2/1) po tvojim pravilima. Podaci su besplatni (FotMob), bez ključa i bez dnevnog limita.

## Pravila
- **Preokreti domaćina kod kuće** (tvoj primjer Técnico U – Manta): domaćin ima bar 2 ista preokreta u zadnjih 5 domaćih ili zadnjih 10 utakmica, a isti preokret je bio i u jednom od zadnja 2 međusobna na njegovom terenu. Jačina 3, dovoljno za PV/K prijedlog.
- **Ponovljeni preokret** (primjer Wadi Degla – Kahraba): zadnji međusobni je završio preokretom, a jedna ekipa je imala preokret i u svom zadnjem meču. Jačina 1.
- **Zbir preokreta:** obje ekipe zajedno bar 3 ista preokreta u zadnjih 10. Jačina 1, od 5 jačina 2, plus 1 ako liga ima taj preokret u bar 5% utakmica.

PV/K prijedlog dobije utakmica čiji signali zajedno daju bar 20 bodova (jačina × 8).

## Izlaz
- `candidates/DATUM.json`: kandidati s gotovim dokumentima za Dosije. Jutarnji Dosije zadatak ih upisuje u aplikaciju.
- `results/DATUM.json`: poluvrijeme i kraj ranijih kandidata, za učenje.
- `data/ht.json`: poluvremena odigranih utakmica (keš koji raste svaki dan).
- `data/league_stats.json`: udio preokreta 1/2 i 2/1 po ligi.
- `data/available_leagues.json`: sve FotMob lige po državama (za dodavanje u `config.json`).
- `dosije_import.py`: od kandidata i rezultata pravi plan upisa u Dosije (koristi ga jutarnji zadatak u 06:43).

## Lige
Redoslijed u `config.json` je prioritet: prvo tvoje lige, pa niži rangovi. Dnevno se analizira do 80 utakmica, a u Dosije ide do 40 najboljih kandidata. Ligu dodaješ sa `{"cc": "ENG", "name": "League Two"}` (kod države i naziv kao na FotMobu) ili s njenim FotMob ID-jem. Svaka izmjena `config.json` odmah pokreće novi pregled.
