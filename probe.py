#!/usr/bin/env python3
"""Provjera izvora (3. krug): FotMob popis liga, poluvrijeme iz matchDetails, prošla sezona."""
import datetime as dt
import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "probe")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TODAY = dt.datetime.now(ZoneInfo("Europe/Sarajevo")).date()
FM = "https://www.fotmob.com/api/data"
H = {"Referer": "https://www.fotmob.com/", "Origin": "https://www.fotmob.com"}


def get(url, timeout=40):
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Encoding": "gzip", **H}
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            raw = r.read()
            wire = len(raw)
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return r.status, json.loads(raw.decode("utf-8")), wire, round(time.time() - t0, 2)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode("utf-8", "replace"), 0, round(time.time() - t0, 2)
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:200], 0, round(time.time() - t0, 2)


def short(x, n):
    return json.dumps(x, ensure_ascii=False)[:n]


def main():
    os.makedirs(OUT, exist_ok=True)
    rep = {"today": TODAY.isoformat()}

    st, js, wire, sec = get(f"{FM}/allLeagues?locale=en")
    row = {"status": st, "wireKB": wire // 1024, "sec": sec}
    if isinstance(js, dict):
        row["keys"] = sorted(js.keys())
        want = {"AUS", "CHN", "IDN", "BOL", "USA", "TUR", "ARM", "ECU", "EGY"}
        countries = js.get("countries") or []
        row["countrySample"] = short(countries[0] if countries else None, 600)
        row["wanted"] = {c.get("ccode"): [[lg.get("id"), lg.get("name")] for lg in c.get("leagues", [])]
                         for c in countries if c.get("ccode") in want}
        row["nCountries"] = len(countries)
    else:
        row["err"] = js
    rep["allLeagues"] = row

    # meč s golom u prvom poluvremenu: Brighton - Brentford 2:0 (5795506) + jedan iz niže lige
    for mid in (5795506,):
        st, js, wire, sec = get(f"{FM}/matchDetails?matchId={mid}")
        row = {"status": st, "wireKB": wire // 1024, "sec": sec}
        if isinstance(js, dict):
            g = js.get("general") or {}
            row["general"] = short({k: g.get(k) for k in ("matchId", "leagueId", "parentLeagueId", "leagueName", "matchRound",
                                                         "matchTimeUTC", "homeTeam", "awayTeam", "finished", "started")}, 700)
            row["headerTeams"] = short((js.get("header") or {}).get("teams"), 700)
            evs = (((js.get("content") or {}).get("matchFacts") or {}).get("events") or {}).get("events") or []
            row["types"] = sorted({e.get("type") for e in evs})
            row["halfEvents"] = short([e for e in evs if e.get("type") in ("Half", "AddedTime")], 1500)
            row["goals"] = short([{k: e.get(k) for k in ("time", "overloadTime", "isHome", "newScore", "ownGoal", "homeScore", "awayScore")}
                                  for e in evs if e.get("type") == "Goal"], 800)
            row["contentKeys"] = sorted((js.get("content") or {}).keys())
        else:
            row["err"] = js
        rep[f"match{mid}"] = row

    # liga: tekuća i prošla sezona (Süper Lig 71, Bolivija 144)
    for lid in (71, 144):
        st, js, wire, sec = get(f"{FM}/leagues?id={lid}")
        row = {"status": st, "wireKB": wire // 1024, "sec": sec}
        if isinstance(js, dict):
            seasons = js.get("allAvailableSeasons") or []
            row["seasons"] = seasons[:4]
            det = js.get("details") or {}
            row["details"] = short({k: det.get(k) for k in ("id", "name", "selectedSeason", "latestSeason", "country", "type")}, 400)
            fx = (js.get("fixtures") or {}).get("allMatches") or []
            row["fixtures"] = len(fx)
            row["finished"] = sum(1 for m in fx if (m.get("status") or {}).get("finished"))
            if len(seasons) > 1:
                prev = seasons[1]
                st2, js2, wire2, sec2 = get(f"{FM}/leagues?id={lid}&season={urllib.parse.quote(prev)}")
                fx2 = ((js2.get("fixtures") or {}).get("allMatches") or []) if isinstance(js2, dict) else []
                row["prev"] = {"season": prev, "status": st2, "wireKB": wire2 // 1024, "fixtures": len(fx2),
                               "selected": ((js2.get("details") or {}).get("selectedSeason") if isinstance(js2, dict) else None),
                               "first": fx2[0]["status"]["utcTime"] if fx2 else None,
                               "last": fx2[-1]["status"]["utcTime"] if fx2 else None}
            finished = [m for m in fx if (m.get("status") or {}).get("finished")]
            if finished:
                mid = finished[-1]["id"]
                st3, js3, wire3, sec3 = get(f"{FM}/matchDetails?matchId={mid}")
                if isinstance(js3, dict):
                    evs = (((js3.get("content") or {}).get("matchFacts") or {}).get("events") or {}).get("events") or []
                    row["lastMatch"] = {"id": mid, "wireKB": wire3 // 1024, "sec": sec3,
                                        "score": finished[-1]["status"].get("scoreStr"),
                                        "half": short([e for e in evs if e.get("type") == "Half"], 600),
                                        "goals": short([[e.get("time"), e.get("overloadTime"), e.get("isHome"), e.get("newScore")]
                                                        for e in evs if e.get("type") == "Goal"], 400)}
        else:
            row["err"] = js
        rep[f"league{lid}"] = row

    with open(os.path.join(OUT, "summary3.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
