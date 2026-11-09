#!/usr/bin/env python3
"""Dosije screener: besplatni FotMob podaci, bez ključa i bez dnevnog limita.

Svako jutro uzme utakmice u narednih 24 h iz liga u config.json (tvoje lige prve,
pa niži rangovi), pročita poluvremena iz zapisnika odigranih utakmica i primijeni
tvoja PV/K pravila (preokreti 1/2 i 2/1). Izlaz:
  candidates/YYYY-MM-DD.json  kandidati s gotovim dokumentima za Dosije
  results/YYYY-MM-DD.json     poluvrijeme i kraj ranijih kandidata (za učenje)
Keš: data/ht.json (poluvremena odigranih utakmica, raste svaki dan) i
     .cache/ (rasporedi liga; čuva ga GitHub Actions cache).
"""
import datetime as dt
import gzip
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

FM = "https://www.fotmob.com/api/data"
WEB = "https://www.fotmob.com"
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(ROOT, ".cache")
TZ = ZoneInfo("Europe/Sarajevo")
UTC = dt.timezone.utc
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
COUNTRY = {
    "AUS": "Australija", "CHN": "Kina", "IDN": "Indonezija", "BOL": "Bolivija", "USA": "SAD",
    "TUR": "Turska", "ARM": "Armenija", "ECU": "Ekvador", "EGY": "Egipat", "ENG": "Engleska",
    "SCO": "Škotska", "GER": "Njemačka", "ITA": "Italija", "ESP": "Španija", "FRA": "Francuska",
    "POR": "Portugal", "NED": "Holandija", "BEL": "Belgija", "GRE": "Grčka", "RUS": "Rusija",
    "POL": "Poljska", "CZE": "Češka", "BUL": "Bugarska", "JPN": "Japan", "KOR": "Južna Koreja",
    "THA": "Tajland", "BRA": "Brazil", "MEX": "Meksiko", "ARG": "Argentina",
    "KSA": "Saudijska Arabija", "IRN": "Iran", "SCO": "Škotska", "AUT": "Austrija", "SUI": "Švicarska",
    "NOR": "Norveška", "SWE": "Švedska", "DEN": "Danska", "ROU": "Rumunija", "COL": "Kolumbija", "CHI": "Čile",
    "URU": "Urugvaj", "SVK": "Slovačka", "HUN": "Mađarska", "CYP": "Kipar", "IND": "Indija", "QAT": "Katar",
}
DROP = set("fc fk nk hnk ofk cf ca cd ac as sc sk afc cr se sv us ssc rc rcd ud sd club 1".split())

CFG = {}
HT = {}          # matchId -> [htH, htA, ftH, ftA] (htH/htA None ako nema zapisnika) | "c" (otkazano)
NOTES = []
STATS = {"calls": 0, "details": 0, "errors": 0}
T0 = time.time()


class Blocked(Exception):
    pass


# ---------- pomoćno ----------
def jload(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def jsave(path, obj, pretty=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if pretty:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        else:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def get(path, **params):
    """Jedan FotMob poziv s pauzom i ponovnim pokušajima. 404/greška -> None."""
    url = f"{FM}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    hdr = {"User-Agent": UA, "Accept": "application/json", "Accept-Encoding": "gzip",
           "Referer": WEB + "/", "Origin": WEB}
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=45) as r:
                raw = r.read()
            STATS["calls"] += 1
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            time.sleep(CFG.get("pause", 0.3))
            return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            STATS["calls"] += 1
            if e.code == 404:
                return None
            if e.code in (401, 403):
                raise Blocked(f"FotMob je odbio pristup ({e.code})")
            time.sleep(8 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            time.sleep(5 * (attempt + 1))
    STATS["errors"] += 1
    return None


def out_of_time():
    return time.time() - T0 > CFG.get("maxMinutes", 40) * 60


def parse_utc(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def tname(t):
    return ((t or {}).get("shortName") or (t or {}).get("name") or "").strip()


def slug(s):
    s = (s or "").lower()
    for a, b in (("đ", "dj"), ("ø", "o"), ("æ", "ae"), ("ß", "ss"), ("ł", "l"), ("ı", "i")):
        s = s.replace(a, b)
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return "-".join(t for t in re.sub(r"[^a-z0-9]+", " ", s).split() if t not in DROP)


def res(a, b):
    return "1" if a > b else ("2" if a < b else "X")


def mid_of(m):
    for k in ("id", "matchId"):
        if m.get(k):
            try:
                return int(m[k])
            except (TypeError, ValueError):
                pass
    for k in ("pageUrl", "matchUrl"):
        mm = re.search(r"#(\d+)", str(m.get(k) or ""))
        if mm:
            return int(mm.group(1))
    return None


def as_int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


# ---------- lige i rasporedi ----------
def cached(name, max_age_h, fetch):
    path = os.path.join(CACHE, name)
    c = jload(path, None)
    if c and time.time() - c.get("at", 0) < max_age_h * 3600:
        return c["v"]
    v = fetch()
    if v is None:
        return c["v"] if c else None
    jsave(path, {"at": time.time(), "v": v})
    return v


def resolve_leagues():
    allmap = {}
    if any(not lg.get("id") for lg in CFG["leagues"]):
        def fetch():
            js = get("allLeagues", locale="en")
            if not js:
                return None
            return {c.get("ccode"): [[x.get("id"), x.get("name")] for x in c.get("leagues", [])]
                    for c in js.get("countries", [])}
        allmap = cached("allLeagues.json", 24 * 30, fetch) or {}
        if allmap and not os.path.exists(os.path.join(DATA, "available_leagues.json")):
            jsave(os.path.join(DATA, "available_leagues.json"),
                  {cc: [f"{x[0]} {x[1]}" for x in v] for cc, v in sorted(allmap.items())}, pretty=True)
    out = []
    for i, lg in enumerate(CFG["leagues"]):
        lid = lg.get("id")
        if not lid:
            opts = allmap.get(lg["cc"], [])
            hit = (next((x for x in opts if x[1].lower() == lg["name"].lower()), None)
                   or next((x for x in opts if x[1].lower().startswith(lg["name"].lower())), None))
            if not hit:
                NOTES.append(f"Liga nije pronađena: {lg['cc']} {lg['name']}")
                continue
            lid = hit[0]
        out.append({"id": int(lid), "cc": lg["cc"], "name": lg["name"], "prio": i})
    return out


def rows_of(fixtures):
    rows = []
    for m in fixtures or []:
        st = m.get("status") or {}
        mid = mid_of(m)
        h, a = as_int((m.get("home") or {}).get("id")), as_int((m.get("away") or {}).get("id"))
        t = st.get("utcTime") or (m.get("time") or {}).get("utcTime")
        if not mid or not h or not a or not t:
            continue
        rows.append({"id": mid, "t": t, "h": h, "hn": tname(m.get("home")), "a": a, "an": tname(m.get("away")),
                     "fin": bool(st.get("finished") or m.get("finished")) and not (st.get("cancelled") or m.get("cancelled"))
                     and not st.get("awarded"),
                     "url": m.get("pageUrl")})
    return rows


def league_pool(lg):
    """Utakmice lige: tekuća i prošla sezona (prošla se kešira dugo)."""
    def fetch_cur():
        js = get("leagues", id=lg["id"])
        if not js:
            return None
        return {"seasons": js.get("allAvailableSeasons") or [],
                "rows": rows_of((js.get("fixtures") or {}).get("allMatches"))}
    cur = cached(f"lg_{lg['id']}.json", CFG.get("leagueRefreshHours", 18), fetch_cur) or {"seasons": [], "rows": []}
    rows = list(cur["rows"])
    if len(cur["seasons"]) > 1:
        prev_name = cur["seasons"][1]

        def fetch_prev():
            js = get("leagues", id=lg["id"], season=prev_name)
            return {"rows": rows_of((js.get("fixtures") or {}).get("allMatches"))} if js else None
        prev = cached(f"lg_{lg['id']}_{slug(prev_name)}.json", 24 * 120, fetch_prev)
        rows += (prev or {}).get("rows", [])
    seen, out = set(), []
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
    return out


def team_rows(team_id):
    def fetch():
        js = get("teams", id=team_id)
        if not js:
            return None
        fx = ((js.get("fixtures") or {}).get("allFixtures") or {}).get("fixtures")
        return rows_of(fx)
    return cached(f"team_{team_id}.json", CFG.get("leagueRefreshHours", 18), fetch) or []


# ---------- poluvremena ----------
def parse_result(js):
    """matchDetails -> [htH, htA, ftH, ftA] | 'c' (otkazano) | None (nije završeno)."""
    if not js:
        return None
    hdr = js.get("header") or {}
    st = hdr.get("status") or {}
    if st.get("cancelled") or st.get("awarded"):
        return "c"
    if not st.get("finished"):
        return None
    evs = (((js.get("content") or {}).get("matchFacts") or {}).get("events") or {}).get("events") or []
    ht = ft = None
    for e in evs:
        if e.get("type") == "Half":
            sc = [as_int(e.get("homeScore")), as_int(e.get("awayScore"))]
            if None in sc:
                continue
            if e.get("halfStrShort") == "HT":
                ht = sc
            elif e.get("halfStrShort") == "FT":
                ft = sc
    teams = hdr.get("teams") or []
    if ft is None and len(teams) == 2:
        ft = [as_int(teams[0].get("score")), as_int(teams[1].get("score"))]
    if ft is None or None in ft:
        return None
    if ht is None and sum(ft) == 0:
        ht = [0, 0]
    if ht is None:
        goals = [e for e in evs if e.get("type") == "Goal" and isinstance(e.get("newScore"), list)]
        if goals:
            first = [g for g in goals if (as_int(g.get("time")) or 99) <= 45]
            ht = list(first[-1]["newScore"]) if first else [0, 0]
    if ht is None:
        return [None, None, ft[0], ft[1]]
    return [ht[0], ht[1], ft[0], ft[1]]


def fetch_ht(ids):
    for mid in ids:
        if str(mid) in HT:
            continue
        if STATS["details"] >= CFG.get("maxDetailCalls", 1500) or out_of_time():
            return False
        STATS["details"] += 1
        r = parse_result(get("matchDetails", matchId=mid))
        if r is not None:
            HT[str(mid)] = r
    return True


def htft_of(m):
    r = HT.get(str(m["id"]))
    if not isinstance(r, list) or r[0] is None:
        return None
    return res(r[0], r[1]) + "/" + res(r[2], r[3])


def dmy(t, year=True):
    return f"{t[8:10]}.{t[5:7]}." + (t[:4] if year else "")


def score_txt(m):
    r = HT.get(str(m["id"]))
    if not isinstance(r, list):
        return ""
    return f"{r[2]}:{r[3]}" + (f" (PV {r[0]}:{r[1]})" if r[0] is not None else "")


def line(m):
    r = HT.get(str(m["id"]))
    d = m["t"][:10]
    if not isinstance(r, list):
        return f"{d} {m['hn']}–{m['an']}"
    ht = f" (PV {r[0]}:{r[1]})" if r[0] is not None else ""
    return f"{d} {m['hn']}–{m['an']} {r[2]}:{r[3]}{ht}"


# ---------- analiza (Harisova pravila) ----------
def team_event(m, team):
    hf = htft_of(m)
    if hf not in ("1/2", "2/1"):
        return None
    leader = m["h"] if hf == "1/2" else m["a"]
    return "lostLead" if leader == team else "comeback"


def as_pattern(m, home_team):
    ev = team_event(m, home_team)
    return None if ev is None else ("1/2" if ev == "lostLead" else "2/1")


def history(team, pool, before):
    ms = [m for m in pool if m["fin"] and (m["h"] == team or m["a"] == team) and m["t"] < before]
    return sorted(ms, key=lambda m: m["t"], reverse=True)


def with_ht(ms, n):
    return [m for m in ms if htft_of(m)][:n]


def sig(sel, s, by, note, url):
    return {"cat": "K7", "dir": sel, "s": s, "conf": "V", "note": note[:90], "src": [url] if url else [], "by": by}


SELS = ("1/2", "2/1")
EVW = {"lostLead": "ispušteno vodstvo s PV", "comeback": "okrenut minus s PV"}
WIN = {"home5": "5 kod kuće", "away5": "5 u gostima", "last10": "zadnjih 10"}


def need_event(role, sel):
    """Šta ekipa treba da uradi za današnji PV/K: domaćin 1/2 = ispusti vodstvo, gost 1/2 = okrene minus."""
    if role == "h":
        return "lostLead" if sel == "1/2" else "comeback"
    return "comeback" if sel == "1/2" else "lostLead"


def analyse(fx, ctx):
    H, A = fx["h"], fx["a"]
    team = {"h": (H, fx["hn"]), "a": (A, fx["an"])}
    win = {}
    for r, (T, _) in team.items():
        ms = ctx["hist"][T]
        win[r] = {"home5": with_ht([m for m in ms if m["h"] == T], 5),
                  "away5": with_ht([m for m in ms if m["a"] == T], 5),
                  "last10": with_ht(ms, 10)}
    h2h = with_ht(ctx["h2h"], 8)
    # zadnja 2 međusobna na svakom od dva terena
    recent = sorted([m for m in h2h if m["h"] == H][:2] + [m for m in h2h if m["h"] == A][:2],
                    key=lambda m: m["t"], reverse=True)

    def h2h_same(m, sel):
        # isti preokret po ulogama ekipa, ili ista oznaka kad je teren bio obrnut (opet domaćin vodi pa gubi)
        return as_pattern(m, H) == sel or (m["h"] == A and htft_of(m) == sel)

    def hits(r, w, sel):
        return [m for m in win[r][w] if team_event(m, team[r][0]) == need_event(r, sel)]

    stats = {sel: {r: {w: len(hits(r, w, sel)) for w in WIN} for r in team} for sel in SELS}
    stats["n"] = {r: {w: len(win[r][w]) for w in WIN} for r in team}
    stats["n"]["h2h"] = len(h2h)
    url = fx.get("url")
    lr = ctx.get("league_rates") or {}
    signals, ev = [], {}
    for sel in SELS:
        c = stats[sel]
        # 1) Preokreti ekipe (domaćin ili gost, kod kuće ili u gostima) + isti preokret u jednom od
        #    zadnjih međusobnih na bilo kojem terenu (proširen primjer Técnico U – Manta)
        cands = sorted(((c[r][w], r, w) for r in team for w in WIN if c[r][w] >= 2),
                       key=lambda x: (-x[0], x[1] != "h", x[2] == "last10"))
        hit = next((m for m in recent if h2h_same(m, sel)), None)
        if cands and hit:
            k, r, w = cands[0]
            tail = f"{k}× {EVW[need_event(r, sel)]} ({WIN[w]}); međusobni {dmy(hit['t'])} {htft_of(hit)}"
            note = f"{team[r][1]} {tail}"
            if len(note) > 90:
                note = f"{'Domaćin' if r == 'h' else 'Gost'} {tail}"
            signals.append(sig(sel, 3, "korisnik", note, url))
        # 2) Ponovljeni preokret: zadnji međusobni završio s istom oznakom (1/2 ili 2/1) + preokret u zadnjem
        #    meču jedne od ekipa (primjer Wadi Degla)
        if h2h and htft_of(h2h[0]) == sel:
            last = [(team[r][1], ctx["hist"][team[r][0]][0]) for r in team
                    if ctx["hist"][team[r][0]] and team_event(ctx["hist"][team[r][0]][0], team[r][0])]
            if last:
                signals.append(sig(sel, 1, "korisnik",
                                   f"Zadnji međusobni {htft_of(h2h[0])} ({dmy(h2h[0]['t'])}); "
                                   f"{last[0][0]} u zadnjem meču {htft_of(last[0][1])}", url))
        # 3) Zbir preokreta obje ekipe u zadnjih 10 (+1 ako liga često ima taj preokret)
        th, ta = c["h"]["last10"], c["a"]["last10"]
        if th + ta >= 3:
            s = 2 if th + ta >= 5 else 1
            rate = lr.get("r12" if sel == "1/2" else "r21")
            extra = ""
            if rate is not None and lr.get("n", 0) >= 40 and rate >= 0.05:
                s, extra = min(3, s + 1), f"; liga {round(rate * 100)}%"
            signals.append(sig(sel, s, "web", f"Preokreti {sel} u zadnjih 10: {fx['hn']} {th}, {fx['an']} {ta}{extra}", url))
        # dokazi za obrazloženje: po ekipi prozor s najviše preokreta (prvo ekipa iz pravila 1)
        lines = []
        order = [cands[0][1]] if cands else []
        order += [r for r in ("h", "a") if r not in order]
        for r in order:
            best_w = max(WIN, key=lambda w: (c[r][w], w != "last10"))
            if c[r][best_w]:
                lines.append((r, best_w, hits(r, best_w, sel)[:2]))
        ev[sel] = {"lines": lines, "h2h": hit}
    pts = {sel: sum(8 * x["s"] for x in signals if x["dir"] == sel) for sel in SELS}
    best = max(pts, key=pts.get)
    score = pts[best]
    return {
        "signals": signals,
        "htftTip": {"sel": best, "score": min(score, 99)} if score >= CFG.get("htftMin", 20) else None,
        "score": score, "best": best if score else None, "stats": stats, "ev": ev,
        "h2h": [line(m) for m in h2h[:6]],
        "homeLast5": [line(m) for m in win["h"]["home5"]],
        "awayLast5": [line(m) for m in win["a"]["away5"]],
    }


def reasoning(fx, a):
    """Kao Harisov opis: čiji preokreti (kod kuće ili u gostima), pa međusobni s istim preokretom."""
    if not a["signals"]:
        return None
    sel = a["best"]
    e = a["ev"][sel]
    name = {"h": fx["hn"], "a": fx["an"]}
    tid = {"h": fx["h"], "a": fx["a"]}

    def item(m, t):
        who = f"protiv {m['an']}" if m["h"] == t else f"{m['hn']}–{m['an']}"
        return f"{who} {score_txt(m)} {dmy(m['t'], False)}"
    out = [f"{name[r]}: {EVW[need_event(r, sel)]} ({WIN[w]}) – " + "; ".join(item(m, tid[r]) for m in ms)
           for r, w, ms in e["lines"]]
    if e["h2h"]:
        m = e["h2h"]
        where = "kod domaćina" if m["h"] == fx["h"] else "kod gosta"
        out.append(f"Međusobni {dmy(m['t'])} {where}: {m['hn']}–{m['an']} {score_txt(m)}")
    elif a["h2h"]:
        out.append("Zadnji međusobni: " + a["h2h"][0])
    text = ". ".join(x.rstrip(".") for x in out)
    return (text if len(text) <= 320 else text[:317].rstrip() + "…") + "."


# ---------- rezultati ranijih kandidata ----------
def settle(now):
    today = now.astimezone(TZ).date()
    for back in range(0, CFG.get("settleDays", 4)):
        d = (today - dt.timedelta(days=back)).isoformat()
        cand = jload(os.path.join(ROOT, "candidates", f"{d}.json"), None)
        if not cand or not cand.get("matches"):
            continue
        path = os.path.join(ROOT, "results", f"{d}.json")
        old = {m["fixtureId"]: m for m in jload(path, {"matches": []}).get("matches", [])}
        changed = False
        for c in cand["matches"]:
            fid = c["fixtureId"]
            if fid in old and old[fid]["status"] in ("FT", "NOHT", "VOID"):
                continue
            ko = parse_utc(c.get("utc"))
            if ko and now < ko + dt.timedelta(hours=2, minutes=30):
                continue
            r = HT.get(str(fid))
            if r is None:
                if STATS["details"] >= CFG.get("maxDetailCalls", 1500) + 200:
                    break
                STATS["details"] += 1
                r = parse_result(get("matchDetails", matchId=fid))
                if r is not None:
                    HT[str(fid)] = r
            if r is None:
                continue
            if r == "c":
                row = {"status": "VOID", "ht": None, "ft": None, "htft": None}
            elif r[0] is None:
                row = {"status": "NOHT", "ht": None, "ft": f"{r[2]}-{r[3]}", "htft": None}
            else:
                row = {"status": "FT", "ht": f"{r[0]}-{r[1]}", "ft": f"{r[2]}-{r[3]}",
                       "htft": res(r[0], r[1]) + "/" + res(r[2], r[3])}
            old[fid] = {"fixtureId": fid, "docId": c.get("docId"), **row}
            changed = True
        if changed:
            jsave(path, {"date": d, "updatedAt": now.isoformat(timespec="seconds"),
                         "matches": sorted(old.values(), key=lambda m: str(m.get("docId")))}, pretty=True)


# ---------- glavni tok ----------
def main():
    global CFG, HT
    CFG = jload(os.path.join(ROOT, "config.json"), {})
    HT = jload(os.path.join(DATA, "ht.json"), {})
    now = dt.datetime.now(UTC)
    today = now.astimezone(TZ).date().isoformat()
    end = now + dt.timedelta(hours=CFG.get("windowHours", 24))
    report = {"date": today, "generatedAt": now.isoformat(timespec="seconds"),
              "window": [now.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")]}
    matches = []
    try:
        settle(now)
        leagues = resolve_leagues()
        by_id = {lg["id"]: lg for lg in leagues}

        # utakmice u prozoru
        days = sorted({now.date(), end.date(), now.astimezone(TZ).date(), end.astimezone(TZ).date()})
        fixtures = {}
        for d in days:
            js = get("matches", date=d.strftime("%Y%m%d"))
            for lg in (js or {}).get("leagues", []):
                lid = as_int(lg.get("primaryId")) or as_int(lg.get("id"))
                if lid not in by_id:
                    continue
                for m in lg.get("matches", []):
                    st = m.get("status") or {}
                    t = parse_utc(st.get("utcTime"))
                    if st.get("started") or st.get("finished") or st.get("cancelled") or not t or not (now <= t < end):
                        continue
                    fid = as_int(m.get("id"))
                    h, a = as_int((m.get("home") or {}).get("id")), as_int((m.get("away") or {}).get("id"))
                    if fid and h and a:
                        fixtures[fid] = {"id": fid, "lid": lid, "utc": t, "t": t.strftime("%Y-%m-%dT%H:%M"),
                                         "h": h, "hn": tname(m.get("home")), "a": a, "an": tname(m.get("away")),
                                         "stage": lg.get("name")}
        fx_all = sorted(fixtures.values(), key=lambda f: (by_id[f["lid"]]["prio"], f["utc"]))
        fx_list = fx_all[: CFG.get("maxFixtures", 80)]
        report.update({"leagues": len(leagues), "fixturesSeen": len(fx_all), "analysed": len(fx_list)})

        # rasporedi liga i istorija ekipa
        pools, ctxs = {}, {}
        for f in fx_list:
            if f["lid"] not in pools:
                pools[f["lid"]] = league_pool(by_id[f["lid"]])
        tiers = [[], [], []]
        for f in fx_list:
            pool = pools[f["lid"]]
            row = next((r for r in pool if r["id"] == f["id"]), None)
            f["url"] = WEB + row["url"] if row and row.get("url") else f"{WEB}/match/{f['id']}"
            hist = {}
            for team in (f["h"], f["a"]):
                hs = history(team, pool, f["t"])
                if len(hs) < 6:
                    extra = [r for r in team_rows(team) if r["id"] not in {x["id"] for x in hs}]
                    hs = sorted(hs + [r for r in extra if r["fin"] and r["t"] < f["t"]
                                      and team in (r["h"], r["a"])], key=lambda m: m["t"], reverse=True)
                hist[team] = hs[:14]
            h2h = [m for m in hist[f["h"]] if f["a"] in (m["h"], m["a"])]
            h2h += [m for m in pool if m["fin"] and {m["h"], m["a"]} == {f["h"], f["a"]} and m["t"] < f["t"]]
            # međusobni iz zapisnika same utakmice (sve sezone i takmičenja)
            if CFG.get("fixtureH2H", True) and not out_of_time():
                det = get("matchDetails", matchId=f["id"])
                STATS["details"] += 1
                hb = ((det or {}).get("content") or {}).get("h2h") or {}
                if CFG.get("debug") and not os.path.exists(os.path.join(ROOT, "probe", "h2h_sample.json")):
                    jsave(os.path.join(ROOT, "probe", "h2h_sample.json"),
                          {"keys": sorted(hb.keys()) if isinstance(hb, dict) else str(type(hb)),
                           "first": (hb.get("matches") or [None])[:2] if isinstance(hb, dict) else None,
                           "infoBox": (((det or {}).get("content") or {}).get("matchFacts") or {}).get("infoBox")},
                          pretty=True)
                for m in rows_of(hb.get("matches") if isinstance(hb, dict) else None):
                    if m["fin"] and {m["h"], m["a"]} == {f["h"], f["a"]} and m["t"] < f["t"]:
                        h2h.append(m)
                ib = ((((det or {}).get("content") or {}).get("matchFacts") or {}).get("infoBox") or {})
                f["venue"] = ((ib.get("Stadium") or {}).get("name")) if isinstance(ib.get("Stadium"), dict) else None
            seen, h2u = set(), []
            for m in sorted(h2h, key=lambda m: m["t"], reverse=True):
                if m["id"] not in seen:
                    seen.add(m["id"])
                    h2u.append(m)
            ctxs[f["id"]] = {"hist": hist, "h2h": h2u[:8]}
            tiers[0] += [m["id"] for m in h2u[:6]]
            tiers[1] += [m["id"] for m in hist[f["h"]] if m["h"] == f["h"]][:6]
            tiers[1] += [m["id"] for m in hist[f["a"]] if m["a"] == f["a"]][:6]
            tiers[2] += [m["id"] for m in hist[f["h"]][:12] + hist[f["a"]][:12]]

        # poluvremena: prvo međusobni, pa domaći/gostujući, pa zadnjih 10
        complete = True
        for ids in tiers:
            complete = fetch_ht(list(dict.fromkeys(ids))) and complete
        if not complete:
            NOTES.append("Dio poluvremena nije povučen (limit vremena); dopunit će se sutra.")

        # stopa preokreta po ligi iz poznatih poluvremena
        rates = {}
        for lid, pool in pools.items():
            known = [htft_of(m) for m in pool if m["fin"]]
            known = [x for x in known if x]
            if known:
                rates[lid] = {"n": len(known), "r12": round(known.count("1/2") / len(known), 3),
                              "r21": round(known.count("2/1") / len(known), 3)}
        ls = jload(os.path.join(DATA, "league_stats.json"), {})
        for lid, r in rates.items():
            ls[str(lid)] = {**r, "name": f"{COUNTRY.get(by_id[lid]['cc'], by_id[lid]['cc'])} · {by_id[lid]['name']}",
                            "at": today}
        jsave(os.path.join(DATA, "league_stats.json"), ls, pretty=True)

        # analiza i dokumenti za Dosije
        matches, screened = [], []
        nominal = 0
        for f in fx_list:
            lg = by_id[f["lid"]]
            ctx = {**ctxs[f["id"]], "league_rates": rates.get(f["lid"])}
            a = analyse(f, ctx)
            if a["stats"]["n"]["h"]["last10"] >= 5 and a["stats"]["n"]["a"]["last10"] >= 5:
                nominal += 1
            screened.append([f["utc"].astimezone(TZ).strftime("%d.%m. %H:%M"), f"{lg['cc']} {lg['name']}",
                             f"{f['hn']} – {f['an']}", a["score"]])
            if a["score"] < CFG.get("listMin", 8):
                continue
            local = f["utc"].astimezone(TZ)
            league = f"{COUNTRY.get(lg['cc'], lg['cc'])} · {lg['name']}"
            doc_id = f"{local.date().isoformat()}_{slug(f['hn'])}_{slug(f['an'])}"
            doc = {
                "date": local.date().isoformat(), "kickoff": local.strftime("%H:%M"), "league": league,
                "home": f["hn"], "away": f["an"], "pairId": "__".join(sorted([slug(f["hn"]), slug(f["an"])])),
                "venue": f.get("venue"), "neutral": False, "signals": a["signals"], "warnings": [],
                "tip": "PASS", "score": None, "htftTip": a["htftTip"], "reasoning": reasoning(f, a),
                "odds": None, "htftOdds": None, "status": "pending", "fbPending": False,
                "sources": [f["url"]], "createdAt": now.isoformat(timespec="seconds"),
                "run": {"screener": True, "source": "fotmob", "fixtureId": f["id"]}, "fixtureId": f["id"],
            }
            matches.append({"fixtureId": f["id"], "docId": doc_id, "utc": f["utc"].isoformat(timespec="minutes"),
                            "league": league, "stage": f.get("stage"), "home": f["hn"], "away": f["an"],
                            "score": a["score"], "htftTip": a["htftTip"], "stats": a["stats"], "h2h": a["h2h"],
                            "homeLast5": a["homeLast5"], "awayLast5": a["awayLast5"], "doc": doc})
        matches.sort(key=lambda m: (-m["score"], m["utc"]))
        matches = matches[: CFG.get("listMax", 40)]
        report["screened"] = screened
        report.update({"withData": nominal, "candidates": len(matches),
                       "tips": sum(1 for m in matches if m["htftTip"])})
    except Blocked as e:
        NOTES.append(str(e))
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        NOTES.append(f"Greška u skeneru: {e!r}"[:200])
    finally:
        jsave(os.path.join(DATA, "ht.json"), HT)
    report.update({"calls": STATS["calls"], "detailCalls": STATS["details"], "errors": STATS["errors"],
                   "minutes": round((time.time() - T0) / 60, 1), "notes": NOTES})
    path = os.path.join(ROOT, "candidates", f"{today}.json")
    new_ids = {m["fixtureId"] for m in matches}
    earlier = [m for m in jload(path, {}).get("matches", []) if m["fixtureId"] not in new_ids]
    jsave(path, {**report, "matches": matches + earlier}, pretty=True)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
