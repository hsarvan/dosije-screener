#!/usr/bin/env python3
"""Provjera besplatnih izvora (2. krug): ispravan ESPN upit i FotMob poluvremena."""
import datetime as dt
import gzip
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


def get(url, headers=None, timeout=40):
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Encoding": "gzip"}
    h.update(headers or {})
    t0 = time.time()

    def body(raw, enc):
        if raw[:2] == b"\x1f\x8b" or enc == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        return raw

    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            raw = body(r.read(), r.headers.get("Content-Encoding"))
            sec = round(time.time() - t0, 2)
            try:
                return r.status, json.loads(raw.decode("utf-8")), len(raw), sec
            except ValueError:
                return r.status, raw[:300].decode("utf-8", "replace"), len(raw), sec
    except urllib.error.HTTPError as e:
        raw = body(e.read(), e.headers.get("Content-Encoding"))
        return e.code, raw[:300].decode("utf-8", "replace"), 0, round(time.time() - t0, 2)
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:300], 0, round(time.time() - t0, 2)


def short(js, n=1500):
    return json.dumps(js, ensure_ascii=False)[:n]


def espn_variants(rep):
    d = TODAY
    variants = {
        "plain": f"{ESPN}/aus.1/scoreboard",
        "oneDay": f"{ESPN}/tur.1/scoreboard?dates={d - dt.timedelta(days=1):%Y%m%d}",
        "range9": f"{ESPN}/tur.1/scoreboard?dates={d - dt.timedelta(days=9):%Y%m%d}-{d:%Y%m%d}",
        "range9lim100": f"{ESPN}/tur.1/scoreboard?dates={d - dt.timedelta(days=9):%Y%m%d}-{d:%Y%m%d}&limit=100",
        "range9lim1000": f"{ESPN}/tur.1/scoreboard?dates={d - dt.timedelta(days=9):%Y%m%d}-{d:%Y%m%d}&limit=1000",
        "range120lim500": f"{ESPN}/tur.1/scoreboard?dates={d - dt.timedelta(days=120):%Y%m%d}-{d:%Y%m%d}&limit=500",
        "range400lim900": f"{ESPN}/tur.1/scoreboard?dates={d - dt.timedelta(days=400):%Y%m%d}-{d:%Y%m%d}&limit=900",
        "season": f"{ESPN}/tur.1/scoreboard?season=2025&limit=500",
        "webHost": f"https://site.web.api.espn.com/apis/site/v2/sports/soccer/tur.1/scoreboard?dates={d - dt.timedelta(days=9):%Y%m%d}-{d:%Y%m%d}",
    }
    out = {}
    sample_event = None
    for k, url in variants.items():
        st, js, size, sec = get(url)
        row = {"status": st, "kb": size // 1024, "sec": sec}
        if isinstance(js, dict):
            ev = js.get("events") or []
            row["events"] = len(ev)
            row["dates"] = [ev[0].get("date"), ev[-1].get("date")] if ev else None
            if ev and sample_event is None:
                done = [e for e in ev if e.get("status", {}).get("type", {}).get("completed")]
                if done:
                    sample_event = done[0]
        else:
            row["err"] = js
        out[k] = row
    rep["espnVariants"] = out
    if sample_event:
        comp = sample_event["competitions"][0]
        rep["espnEventKeys"] = sorted(sample_event.keys())
        rep["espnCompKeys"] = sorted(comp.keys())
        rep["espnCompetitor"] = short({k: v for k, v in comp["competitors"][0].items() if k not in ("statistics", "team")}, 1200)
        rep["espnDetails"] = short(comp.get("details"), 2500)
        rep["espnStatus"] = short(comp.get("status"), 400)
        # summary jednog meča
        st, js, size, sec = get(f"{ESPN}/tur.1/summary?event={sample_event['id']}")
        if isinstance(js, dict):
            rep["espnSummaryKeys"] = sorted(js.keys())
            ke = js.get("keyEvents") or []
            rep["espnKeyEvents"] = short([{"t": (e.get("type") or {}).get("text"), "c": (e.get("clock") or {}).get("displayValue"),
                                           "txt": (e.get("text") or "")[:80], "per": (e.get("period") or {}).get("number"),
                                           "team": (e.get("team") or {}).get("id"), "sp": e.get("scoringPlay")} for e in ke], 2500)
            hc = ((js.get("header") or {}).get("competitions") or [{}])[0]
            rep["espnSummaryLinescores"] = short([c.get("linescores") for c in hc.get("competitors", [])], 500)
        else:
            rep["espnSummary"] = {"status": st, "err": js}


def fotmob(rep):
    h = {"Referer": "https://www.fotmob.com/", "Origin": "https://www.fotmob.com"}
    y = TODAY - dt.timedelta(days=1)
    st, js, size, sec = get(f"https://www.fotmob.com/api/data/matches?date={y:%Y%m%d}", h)
    res = {"matchesYesterday": {"status": st, "kb": size // 1024}}
    fin = None
    if isinstance(js, dict):
        lgs = js.get("leagues") or []
        res["matchesYesterday"]["leagues"] = len(lgs)
        res["leagueList"] = [[lg.get("primaryId") or lg.get("id"), lg.get("ccode"), lg.get("name"), len(lg.get("matches") or [])] for lg in lgs][:400]
        res["leagueKeys"] = sorted(lgs[0].keys()) if lgs else None
        for lg in lgs:
            for m in lg.get("matches") or []:
                if (m.get("status") or {}).get("finished") and fin is None:
                    fin = (lg, m)
        if fin:
            res["matchSample"] = short(fin[1], 1500)
    else:
        res["matchesYesterday"]["err"] = js
    if fin:
        lg, m = fin
        for name, url in (("matchDetails", f"https://www.fotmob.com/api/data/matchDetails?matchId={m['id']}"),
                          ("matchDetailsOld", f"https://www.fotmob.com/api/matchDetails?matchId={m['id']}")):
            st, js, size, sec = get(url, h)
            row = {"status": st, "kb": size // 1024, "sec": sec}
            if isinstance(js, dict):
                row["keys"] = sorted(js.keys())
                hdr = js.get("header") or {}
                row["headerStatus"] = short(hdr.get("status"), 800)
                evs = (((js.get("content") or {}).get("matchFacts") or {}).get("events") or {}).get("events") or []
                row["events"] = short([{k: e.get(k) for k in ("type", "time", "overloadTime", "isHome", "homeScore",
                                                             "awayScore", "halfStrShort", "newScore")} for e in evs], 1800)
            else:
                row["err"] = js
            res[name] = row
        lid = lg.get("primaryId") or lg.get("id")
        st, js, size, sec = get(f"https://www.fotmob.com/api/data/leagues?id={lid}", h)
        row = {"status": st, "kb": size // 1024, "sec": sec, "id": lid, "name": lg.get("name")}
        if isinstance(js, dict):
            row["keys"] = sorted(js.keys())
            fx = (js.get("fixtures") or {}).get("allMatches") or js.get("matches", {}).get("allMatches") if isinstance(js.get("fixtures") or js.get("matches"), dict) else None
            row["fixtures"] = len(fx) if fx else None
            row["fixtureSample"] = short(fx[0], 900) if fx else short(js.get("fixtures"), 600)
        else:
            row["err"] = js
        res["league"] = row
        tid = (m.get("home") or {}).get("id")
        st, js, size, sec = get(f"https://www.fotmob.com/api/data/teams?id={tid}", h)
        row = {"status": st, "kb": size // 1024, "sec": sec}
        if isinstance(js, dict):
            row["keys"] = sorted(js.keys())
            row["fixturesKeys"] = sorted((js.get("fixtures") or {}).keys()) if isinstance(js.get("fixtures"), dict) else None
            af = ((js.get("fixtures") or {}).get("allFixtures") or {}) if isinstance(js.get("fixtures"), dict) else {}
            row["allFixtures"] = short(af, 1500)
        else:
            row["err"] = js
        res["team"] = row
    rep["fotmob"] = res


def main():
    os.makedirs(OUT, exist_ok=True)
    rep = {"today": TODAY.isoformat(), "ranAt": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"}
    try:
        espn_variants(rep)
    except Exception as e:  # noqa: BLE001
        rep["espnError"] = repr(e)
    try:
        fotmob(rep)
    except Exception as e:  # noqa: BLE001
        rep["fotmobError"] = repr(e)
    with open(os.path.join(OUT, "summary2.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(json.dumps(rep.get("espnVariants"), ensure_ascii=False)[:2000])


if __name__ == "__main__":
    main()
