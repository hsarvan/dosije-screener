#!/usr/bin/env python3
"""Provjera besplatnih izvora podataka (pokreće se na GitHub Actions).

Upisuje probe/summary.json: koje lige ESPN pokriva, ima li poluvremena,
radi li Sofascore/FotMob/TheSportsDB sa servera i šta vraća API-Football free.
"""
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "probe")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TODAY = dt.datetime.now(ZoneInfo("Europe/Sarajevo")).date()
ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer"

FALLBACK = ("aus.1 chn.1 chn.2 idn.1 idn.2 bol.1 bol.2 usa.1 usa.usl.1 usa.usl.l1 usa.mlsnp tur.1 tur.2 "
            "ecu.1 ecu.2 egy.1 arm.1 eng.2 eng.3 eng.4 eng.5 sco.2 sco.3 sco.4 ger.2 ger.3 ita.2 ita.3 "
            "esp.2 fra.2 fra.3 ned.2 por.2 bel.2 aut.2 sui.2 den.2 swe.2 nor.2 bra.2 bra.3 arg.2 mex.2 "
            "col.1 col.2 chi.1 chi.2 per.1 par.1 uru.1 ven.1 jpn.1 jpn.2 kor.1 kor.2 ksa.1 ind.1 tha.1 "
            "rsa.1 gre.1 gre.2 cyp.1 rou.1 isr.1 irl.1 irl.2 crc.1 gua.1 hon.1 slv.1 nir.1 wal.1").split()


def get(url, headers=None, timeout=40):
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    h.update(headers or {})
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw.decode("utf-8")), len(raw), round(time.time() - t0, 2)
            except ValueError:
                return r.status, raw[:200].decode("utf-8", "replace"), len(raw), round(time.time() - t0, 2)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode("utf-8", "replace"), 0, round(time.time() - t0, 2)
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:200], 0, round(time.time() - t0, 2)


def minute(disp):
    """'23'' -> 23, '45'+2'' -> 45.02 (prvo poluvrijeme), '90'+4'' -> 90.04."""
    m = re.match(r"\s*(\d+)'?\s*(?:\+\s*(\d+))?", disp or "")
    if not m:
        return None
    return int(m.group(1)) + (int(m.group(2)) / 100 if m.group(2) else 0)


def espn_ht(comp):
    """Poluvrijeme iz liste golova (details). Vraća (ht, ft_iz_golova) ili None."""
    det = comp.get("details") or []
    teams = {c["team"]["id"]: c.get("homeAway") for c in comp.get("competitors", [])}
    ht = {"home": 0, "away": 0}
    ft = {"home": 0, "away": 0}
    for d in det:
        if not d.get("scoringPlay") or d.get("shootout"):
            continue
        side = teams.get((d.get("team") or {}).get("id"))
        mn = minute((d.get("clock") or {}).get("displayValue"))
        if side is None or mn is None:
            return None
        ft[side] += 1
        if mn < 46:
            ht[side] += 1
    return ht, ft


def espn_league(slug, start, end):
    st, js, size, sec = get(f"{ESPN}/{slug}/scoreboard?dates={start:%Y%m%d}-{end:%Y%m%d}&limit=1000")
    row = {"slug": slug, "status": st, "kb": size // 1024, "sec": sec}
    if st != 200 or not isinstance(js, dict):
        row["err"] = js if isinstance(js, str) else None
        return row, None
    lg = (js.get("leagues") or [{}])[0]
    ev = js.get("events") or []
    row.update({"name": lg.get("name"), "season": (lg.get("season") or {}).get("displayName"), "events": len(ev)})
    done = [e for e in ev if (e.get("status") or {}).get("type", {}).get("completed")]
    today = [e for e in ev if e.get("date", "")[:10] in (TODAY.isoformat(), (TODAY + dt.timedelta(days=1)).isoformat())]
    ok = bad = nodet = linesc = 0
    sample = None
    for e in done:
        comp = (e.get("competitions") or [{}])[0]
        cs = comp.get("competitors") or []
        if any(c.get("linescores") for c in cs):
            linesc += 1
        if not comp.get("details"):
            tot = sum(int(c.get("score") or 0) for c in cs)
            nodet += 1 if tot > 0 else 0
            if tot > 0:
                continue
        r = espn_ht(comp)
        if r is None:
            bad += 1
            continue
        real = {c.get("homeAway"): int(c.get("score") or 0) for c in cs}
        if r[1] == real:
            ok += 1
        else:
            bad += 1
        if sample is None and sum(real.values()) >= 2:
            sample = {
                "name": e.get("name"), "date": e.get("date"),
                "score": {c.get("homeAway"): c.get("score") for c in cs},
                "linescores": {c.get("homeAway"): c.get("linescores") for c in cs},
                "ht_calc": r[0],
                "details": [{"t": (d.get("type") or {}).get("text"), "c": (d.get("clock") or {}).get("displayValue"),
                             "team": (d.get("team") or {}).get("id"), "og": d.get("ownGoal"), "pk": d.get("penaltyKick"),
                             "sp": d.get("scoringPlay")} for d in comp.get("details", [])][:12],
                "teams": {c.get("homeAway"): [c["team"].get("id"), c["team"].get("displayName")] for c in cs},
                "venue": (comp.get("venue") or {}).get("fullName"),
            }
    row.update({"completed": len(done), "htOK": ok, "htBad": bad, "noDetails": nodet, "linescores": linesc,
                "todayTomorrow": len(today)})
    return row, sample


def espn_slugs():
    st, js, _, _ = get("https://sports.core.api.espn.com/v2/sports/soccer/leagues?limit=1000")
    if st != 200 or not isinstance(js, dict):
        return FALLBACK, f"core list {st}"
    slugs = []
    for it in js.get("items", []):
        m = re.search(r"/leagues/([^/?]+)", it.get("$ref", ""))
        if m:
            slugs.append(m.group(1))
    return slugs, f"core list {len(slugs)}"


def main():
    os.makedirs(OUT, exist_ok=True)
    rep = {"today": TODAY.isoformat(), "ranAt": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"}

    # ESPN: popis liga, pa po jedan poziv za 3 sedmice unazad + 2 dana unaprijed
    slugs, note = espn_slugs()
    rep["espnList"] = note
    rep["espnAllSlugs"] = slugs
    skip = re.compile(r"(wom|\.w\.|w\.|friendly|cup|copa|pokal|coupe|coppa|super|uefa|fifa|conmebol|concacaf|"
                      r"afc|caf|olymp|u17|u19|u20|u21|u23|youth|nwsl|shebelieves|champions|trophy|shield|playoff)")
    cand = [s for s in slugs if re.match(r"^[a-z]{3}\.\d$", s) or s in FALLBACK]
    cand = [s for s in dict.fromkeys(cand + FALLBACK) if not skip.search(s)]
    start, end = TODAY - dt.timedelta(days=21), TODAY + dt.timedelta(days=2)
    rows, samples = [], {}
    for s in cand[:260]:
        r, smp = espn_league(s, start, end)
        if r.get("status") == 200 and r.get("events"):
            rows.append(r)
            if smp and len(samples) < 6:
                samples[s] = smp
        elif r.get("status") != 200:
            rows.append({"slug": s, "status": r.get("status")})
    rep["espn"] = rows
    rep["espnSamples"] = samples

    # Jedna sezona unazad u jednom pozivu (da li raspon od ~1 godine radi)
    r, _ = espn_league("tur.1", TODAY - dt.timedelta(days=420), TODAY)
    rep["espnLongRange"] = r

    # Sofascore
    sh = {"Referer": "https://www.sofascore.com/", "Origin": "https://www.sofascore.com"}
    for host in ("https://api.sofascore.com", "https://www.sofascore.com"):
        st, js, size, sec = get(f"{host}/api/v1/sport/football/scheduled-events/{TODAY.isoformat()}", sh)
        rep.setdefault("sofascore", []).append({
            "host": host, "status": st, "kb": size // 1024, "sec": sec,
            "events": len(js.get("events", [])) if isinstance(js, dict) else None,
            "err": js if isinstance(js, str) else None})

    # FotMob
    st, js, size, sec = get(f"https://www.fotmob.com/api/data/matches?date={TODAY:%Y%m%d}")
    rep["fotmob"] = {"status": st, "kb": size // 1024, "err": js if isinstance(js, str) else None,
                     "leagues": len(js.get("leagues", [])) if isinstance(js, dict) else None}

    # TheSportsDB (javni ključ 123)
    st, js, size, sec = get(f"https://www.thesportsdb.com/api/v1/json/123/eventsday.php?d={TODAY.isoformat()}&s=Soccer")
    ev = (js.get("events") or []) if isinstance(js, dict) else []
    rep["thesportsdb"] = {"status": st, "events": len(ev), "leagues": sorted({e.get("strLeague") for e in ev})[:40]}

    # API-Football free: status + jedan poziv za današnji dan + jedan za tekuću sezonu A-Lige
    key = os.environ.get("API_FOOTBALL_KEY")
    if key:
        af = {}
        for name, path in (("status", "status"), ("today", f"fixtures?date={TODAY.isoformat()}"),
                           ("season", f"fixtures?league=188&season={TODAY.year}")):
            st, js, _, _ = get(f"https://v3.football.api-sports.io/{path}", {"x-apisports-key": key})
            if isinstance(js, dict):
                resp = js.get("response")
                af[name] = {"status": st, "errors": js.get("errors"), "results": js.get("results"),
                            "plan": (resp or {}).get("subscription") if name == "status" and isinstance(resp, dict) else None,
                            "requests": (resp or {}).get("requests") if name == "status" and isinstance(resp, dict) else None}
            else:
                af[name] = {"status": st, "err": js}
        rep["apiFootball"] = af
    else:
        rep["apiFootball"] = "nema ključa u okruženju"

    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: rep[k] for k in ("espnList", "sofascore", "fotmob", "apiFootball")}, ensure_ascii=False)[:3000])


if __name__ == "__main__":
    main()
