# Dosije screener

Svako jutro oko 05:00 (po Sarajevu) pregleda utakmice u narednih 24 sata u ligama iz `config.json` i izvlači kandidate za preokret poluvrijeme/kraj (1/2 i 2/1). Podaci su besplatni (FotMob), bez ključa i bez dnevnog limita.

Za obje ekipe čita zadnjih 15 mečeva s poluvremenom u svim takmičenjima (liga, kup, Evropa), međusobne mečeve i raspored oko utakmice.

## Pravila
- **R1 Preokreti ekipe + međusobni** (primjer Técnico U – Manta): domaćin ili gost ima bar 2 preokreta koja vode na isti današnji PV/K (za 1/2: domaćin ispušta vodstvo s poluvremena ili gost okreće minus) u zadnjih 5 kod kuće, 5 u gostima, zadnjih 10 ili 15, a isti preokret je bio i u jednom od zadnja 2 međusobna na bilo kojem terenu. Jačina 3.
- **R2 Ponovljeni preokret** (primjer Wadi Degla – Kahraba): zadnji međusobni je završio 1/2 ili 2/1, a jedna ekipa je imala preokret i u svom zadnjem meču. Jačina 1.
- **R3 Zbir preokreta:** obje ekipe zajedno bar 3 ista preokreta u zadnjih 10. Jačina 1, od 5 jačina 2, plus 1 ako liga ima taj preokret u bar 5% utakmica.
- **R4 Ekipa sklona preokretima** (primjer Torreense – União de Leiria): broje se svi preokreti ekipe (1/2 i 2/1) u zadnjih 15, a svježi vrijede više. Jačina 1: 2 u zadnjih 10. Jačina 2: 3 u zadnjih 15, ili 2 u zadnjih 5, ili 2 u zadnjih 10 uz preokret u zadnjem meču. Jačina 3: 2 u zadnja 3, ili 4 u zadnjih 15, ili jačina 2 uz preokret u zadnjem meču. Utakmica je kandidat za oba smjera; prvi je smjer koji odgovara vrsti preokreta te ekipe.
- **R5 Gust raspored:** ekipa igra kup ili Evropu do 4 dana prije ili poslije (umor, rotacija), pa lakše ispusti vodstvo. Broji se samo uz neki drugi signal. Jačina 2 za Evropu prije utakmice, inače 1. Ide i u „Upozorenja i nedostaci”.

Bodovi su jačina × 8 × faktor pravila. Od 8 bodova utakmica je kandidat, od 20 je PV/K prijedlog.

## Kako pravila uče
Za svaku pregledanu utakmicu (i one bez signala) pamti se koja su pravila okinula, a sutradan i ishod. Iz toga se za svako pravilo računa koliko češće pogađa od prosjeka lige. Pravilo koje pogađa bolje od očekivanog dobija veći faktor (do 1,6), a ono koje ne pogađa manji (do 0,4), pa samo više ne daje prijedlog. Stanje je u `data/rule_stats.json` i u Dosijeu pod Skener.

Provjera na već odigranim utakmicama: u `backtest_request.json` upiši `{"from": "2026-10-01", "to": "2026-10-31"}`. Rezultat stiže u `backtest/`.

## Izlaz
- `candidates/DATUM.json`: kandidati s gotovim dokumentima za Dosije. Jutarnji zadatak u 06:43 ih upisuje u aplikaciju.
- `results/DATUM.json`: poluvrijeme i kraj ranijih kandidata.
- `data/samples/MJESEC.json`: sve pregledane utakmice s okinutim pravilima i ishodom.
- `data/rule_stats.json`: pogodak i faktor po pravilu.
- `data/ht.json`: poluvremena odigranih utakmica (keš koji raste svaki dan).
- `data/league_stats.json`: udio preokreta 1/2 i 2/1 po ligi.
- `data/available_leagues.json`: sve FotMob lige po državama (za dodavanje u `config.json`).
- `dosije_import.py`: od kandidata i rezultata pravi plan upisa u Dosije.

## Lige
Redoslijed u `config.json` je prioritet: prvo tvoje lige, pa niži rangovi. Dnevno se analizira do 80 utakmica, a u Dosije ide do 40 najboljih kandidata. Ligu dodaješ sa `{"cc": "ENG", "name": "League Two"}` (kod države i naziv kao na FotMobu) ili s njenim FotMob ID-jem. Svaka izmjena `config.json` odmah pokreće novi pregled.
