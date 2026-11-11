#!/usr/bin/env python3
"""Dosije screener: besplatni FotMob podaci, bez ključa i bez dnevnog limita.

Svako jutro uzme utakmice u narednih 24 h iz liga u config.json (tvoje lige prve, pa niži
rangovi), za obje ekipe pročita zadnjih 15 mečeva s poluvremenom (liga, kup, Evropa) i
primijeni tvoja PV/K pravila. Izlaz:
  candidates/DATUM.json      kandidati za 1/2 i 2/1 s gotovim dokumentima za Dosije
  results/DATUM.json         poluvrijeme i kraj ranijih kandidata
  data/samples/MJESEC.json   sve pregledane utakmice: koja su pravila okinula i kako je završilo
  data/rule_stats.json       koliko koje pravilo pogađa; po tome se važu bodovi pravila
  backtest/*.json            naknadna provjera na odigranim utakmicama (--backtest)
Keš: data/ht.json (poluvremena odigranih utakmica) i .cache/ (rasporedi liga i ekipa).
"""
import concurrent.futures as cf
import datetime as dt
import glob
import gzip
import json
import math
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
    "KSA": "Saudijska Arabija", "IRN": "Iran", "AUT": "Austrija", "SUI": "Švicarska",
    "NOR": "Norveška", "SWE": "Švedska", "DEN": "Danska", "ROU": "Rumunija", "COL": "Kolumbija", "CHI": "Čile",
    "URU": "Urugvaj", "SVK": "Slovačka", "HUN": "Mađarska", "CYP": "Kipar", "IND": "Indija", "QAT": "Katar",
}
DROP = set("fc fk nk hnk ofk cf ca cd ac as sc sk afc cr se sv us ssc rc rcd ud sd club 1".split())
CONT = re.compile(r"champions league|europa league|conference league|uefa|libertadores|sudamericana|recopa|"
                  r"\bafc\b|\bcaf\b|concacaf|leagues cup|club world cup", re.I)
FRIENDLY = re.compile(r"friendl", re.I)

SELS = ("1/2", "2/1")
EVW = {"lostLead": "ispušteno vodstvo s PV", "comeback": "okrenut minus s PV"}
WIN = {"home5": "5 kod kuće", "away5": "5 u gostima", "last10": "zadnjih 10", "last15": "zadnjih 15"}
RULES = {"R1": "Preokreti ekipe + isti preokret u međusobnom", "R2": "Ponovljeni preokret iz zadnjeg međusobnog",
         "R3": "Zbir preokreta obje ekipe (zadnjih 10)", "R4": "Ekipa sklona preokretima (zadnjih 15)",
         "R5": "Gust raspored: kup ili Evropa oko utakmice",
         "R6": "Svjež preokret + preokret kod druge ekipe",
         "R7": "Zadnji međusobni preokret + preokret ekipe u zadnjih 10"}
PRIOR = {1: 1.25, 2: 1.6, 3: 2.0}   # koliko puta češće od prosjeka očekujemo pogodak po jačini (prije podataka)
PRIOR_E = 6.0                        # težina te pretpostavke: kao 6 pogodaka koliko bi dao prosjek lige
BOTH = {"R4", "R6"}                        # pravila koja traže preokret u bilo kojem smjeru (kandidat za 1/2 i 2/1)
# Model golova (izmjereno na 3.913 utakmica, 25.9.–8.11.2026): prosjek golova po ekipi u 1. i 2. poluvremenu,
# prednost domaćina i prosječna šansa preokreta po modelu. Utakmice s malo golova imale su preokret u 3,3%
# slučajeva (ostale 5,6%), pa im se bodovi smanjuju; one s puno golova dobiju mali dodatak.
G1, G2, HFA, P_MEAN = 0.608, 0.777, 1.13, 0.0465
GOALS_LOW, GOALS_HIGH = (0.7, 0.6), (1.3, 1.15)     # (granica omjera prema prosjeku, faktor bodova)

CFG = {}
HT = {}          # matchId -> [htH, htA, ftH, ftA] (htH/htA None bez zapisnika) | "c" otkazano | "n" nema podataka
MULT = {}        # pravilo -> faktor iz provjere na odigranim utakmicama
NOTES = []
STATS = {"calls": 0, "details": 0, "errors": 0}
T0 = time.time()
ABORT = []


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
    if ABORT:
        raise Blocked(ABORT[0])
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
                ABORT.append(f"FotMob je odbio pristup ({e.code})")
                raise Blocked(ABORT[0])
            time.sleep(8 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            time.sleep(5 * (attempt + 1))
    STATS["errors"] += 1
    return None


def pmap(fn, items):
    """Nekoliko poziva istovremeno (config workers), rezultati istim redom."""
    items = list(items)
    w = max(1, int(CFG.get("workers", 3)))
    if w == 1 or len(items) < 2:
        return [fn(x) for x in items]
    with cf.ThreadPoolExecutor(max_workers=w) as ex:
        return list(ex.map(fn, items))


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


def other(sel):
    return "2/1" if sel == "1/2" else "1/2"


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


def dana(n):
    return "dan" if n == 1 else "dana"


# ---------- lige, ekipe i rasporedi ----------
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


def rows_of(fixtures, lid=None):
    """FotMob utakmice -> kratki redovi. lg/ln = takmičenje (iz rasporeda ekipe ili međusobnih)."""
    rows = []
    for m in fixtures or []:
        st = m.get("status") or {}
        mid = mid_of(m)
        h, a = as_int((m.get("home") or {}).get("id")), as_int((m.get("away") or {}).get("id"))
        t = st.get("utcTime") or (m.get("time") or {}).get("utcTime")
        if not mid or not h or not a or not t:
            continue
        tour = m.get("tournament") or m.get("league") or {}
        can = bool(st.get("cancelled") or m.get("cancelled"))
        rows.append({"id": mid, "t": t, "h": h, "hn": tname(m.get("home")), "a": a, "an": tname(m.get("away")),
                     "fin": bool(st.get("finished") or m.get("finished")) and not can and not st.get("awarded"),
                     "can": can, "url": m.get("pageUrl"),
                     "lg": lid or as_int(tour.get("leagueId") or tour.get("id")), "ln": tour.get("name")})
    return rows


def league_pool(lg):
    """Utakmice lige: tekuća i prošla sezona (prošla se kešira dugo)."""
    def fetch_cur():
        js = get("leagues", id=lg["id"])
        if not js:
            return None
        return {"seasons": js.get("allAvailableSeasons") or [],
                "rows": rows_of((js.get("fixtures") or {}).get("allMatches"), lg["id"])}
    cur = cached(f"lgb_{lg['id']}.json", CFG.get("leagueRefreshHours", 18), fetch_cur) or {"seasons": [], "rows": []}
    rows = list(cur["rows"])
    if len(cur["seasons"]) > 1:
        prev_name = cur["seasons"][1]

        def fetch_prev():
            js = get("leagues", id=lg["id"], season=prev_name)
            return {"rows": rows_of((js.get("fixtures") or {}).get("allMatches"), lg["id"])} if js else None
        prev = cached(f"lgb_{lg['id']}_{slug(prev_name)}.json", 24 * 120, fetch_prev)
        rows += (prev or {}).get("rows", [])
    seen, out = set(), []
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
    return out


def team_rows(team_id):
    """Raspored ekipe u svim takmičenjima ove sezone (liga, kup, Evropa), i odigrano i zakazano."""
    if not CFG.get("teamFixtures", True):
        return []

    def fetch():
        js = get("teams", id=team_id)
        if not js:
            return None
        return rows_of(((js.get("fixtures") or {}).get("allFixtures") or {}).get("fixtures"))
    return cached(f"teamb_{team_id}.json", CFG.get("leagueRefreshHours", 18), fetch) or []


def team_matches(team, pool):
    """Sve utakmice ekipe, najnovije prve: liga iz rasporeda lige + ostala takmičenja, bez prijateljskih."""
    by = {m["id"]: m for m in pool if team in (m["h"], m["a"])}
    for m in team_rows(team):
        if team not in (m["h"], m["a"]) or FRIENDLY.search(m.get("ln") or ""):
            continue
        old = by.get(m["id"])
        by[m["id"]] = {**old, "ln": m.get("ln") or old.get("ln")} if old else m
    return sorted(by.values(), key=lambda m: m["t"], reverse=True)


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
    """Poluvremena odigranih utakmica u keš. Vraća False ako je ostalo nepovučenih (limit poziva ili vremena)."""
    todo = [i for i in dict.fromkeys(ids) if str(i) not in HT]
    room = CFG.get("maxDetailCalls", 3000) - STATS["details"]
    if not todo:
        return True
    if room <= 0 or out_of_time():
        return False
    part = todo[:room]
    STATS["details"] += len(part)

    def one(mid):
        if out_of_time():
            return
        js = get("matchDetails", matchId=mid)
        if js is None:
            return
        r = parse_result(js)
        HT[str(mid)] = r if r is not None else "n"
    pmap(one, part)
    return len(part) == len(todo) and not out_of_time()


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
    return f"{m['t'][:10]} {m['hn']}–{m['an']} {score_txt(m)}".rstrip()


# ---------- analiza (Harisova pravila) ----------
def team_event(m, team):
    hf = htft_of(m)
    if hf not in SELS:
        return None
    leader = m["h"] if hf == "1/2" else m["a"]
    return "lostLead" if leader == team else "comeback"


def as_pattern(m, home_team):
    ev = team_event(m, home_team)
    return None if ev is None else ("1/2" if ev == "lostLead" else "2/1")


def with_ht(ms, n):
    return [m for m in ms if htft_of(m)][:n]


def need_event(role, sel):
    """Šta ekipa treba da uradi za današnji PV/K: domaćin 1/2 = ispusti vodstvo, gost 1/2 = okrene minus."""
    if role == "h":
        return "lostLead" if sel == "1/2" else "comeback"
    return "comeback" if sel == "1/2" else "lostLead"


def sel_for(role, event):
    return next(s for s in SELS if need_event(role, s) == event)


def mult(rule, s):
    return MULT.get(f"{rule}:{s}", MULT.get(rule, 1.0))


def sig(rule, sel, s, by, note, url, both=False):
    out = {"cat": "K7", "dir": sel, "s": s, "conf": "V", "note": note[:90], "src": [url] if url else [],
           "by": by, "rule": rule}
    if both:
        out["both"] = True
    return out


def league_means(pool, before):
    """Prosjek golova po ekipi u 1. i 2. poluvremenu u ligi i prednost domaćina (povučeno ka općem prosjeku)."""
    n = g1 = g2 = hn = hg = 0
    for m in pool:
        r = HT.get(str(m["id"]))
        if not m["fin"] or m["t"] >= before or not isinstance(r, list) or r[0] is None:
            continue
        n += 2
        g1 += r[0] + r[1]
        g2 += r[2] + r[3] - r[0] - r[1]
        hn += 1
        hg += r[2]
    m1, m2 = (g1 + 300 * G1) / (n + 300), (g2 + 300 * G2) / (n + 300)
    hf = ((hg + 150 * HFA * (G1 + G2)) / (hn + 150)) / (m1 + m2)
    return m1, m2, min(1.35, max(1.0, hf))


def pois(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def goal_model(fx, h15, means):
    """Šansa za 1/2 i 2/1 iz golova obje ekipe po poluvremenima (nezavisni Poisson). -> (p12, p21)"""
    m1, m2, hf = means

    def rates(rows, team):
        rs = [m for m in rows if m.get("lg") == fx["lid"]]
        if len(rs) < 6:
            rs = rows
        k, n = 10, len(rs)
        f1 = a1 = f2 = a2 = 0
        for m in rs:
            r = HT[str(m["id"])]
            hf_, ha_, ff_, fa_ = (r[0], r[1], r[2], r[3]) if m["h"] == team else (r[1], r[0], r[3], r[2])
            f1, a1, f2, a2 = f1 + hf_, a1 + ha_, f2 + ff_ - hf_, a2 + fa_ - ha_
        return (f1 + k * m1) / (n + k), (a1 + k * m1) / (n + k), (f2 + k * m2) / (n + k), (a2 + k * m2) / (n + k)
    ha1, hd1, ha2, hd2 = rates(h15["h"], fx["h"])
    aa1, ad1, aa2, ad2 = rates(h15["a"], fx["a"])
    s = hf ** 0.5
    lam = (ha1 * ad1 / m1 * s, aa1 * hd1 / m1 / s, ha2 * ad2 / m2 * s, aa2 * hd2 / m2 / s)
    n = 9
    h1, a1_, h2, a2_ = ([pois(i, x) for i in range(n)] for x in lam)

    def tail(win, lose, need):      # u drugom poluvremenu ekipa da više od zaostatka
        return sum(win[i] * lose[j] for i in range(n) for j in range(n) if i - j > need)
    p12 = p21 = 0.0
    for lead in range(1, 5):
        p12 += sum(h1[i] * a1_[i - lead] for i in range(lead, n)) * tail(a2_, h2, lead)
        p21 += sum(a1_[i] * h1[i - lead] for i in range(lead, n)) * tail(h2, a2_, lead)
    return p12, p21


def congestion(team, allm, f):
    """Gust raspored: meč u drugom takmičenju (kup, Evropa) do 4 dana prije ili poslije, ili treći meč u 8 dana."""
    out = {"rest": None, "boost": 0, "impact": 0, "note": None, "type": None}

    def days(m):
        t = parse_utc(m["t"])
        return None if t is None else (t - f["utc"]).total_seconds() / 86400
    past = [m for m in allm if m["fin"] and m["t"][:16] < f["t"]]
    prev = past[0] if past else None
    nxt = next((m for m in reversed(allm) if m["t"][:16] > f["t"] and not m.get("can") and m["id"] != f["id"]), None)
    dp = days(prev) if prev else None
    dn = days(nxt) if nxt else None
    if dp is not None:
        out["rest"] = round(-dp, 1)
    notes = []
    if prev and dp is not None and -dp <= 4.6 and prev.get("lg") and prev["lg"] != f["lid"]:
        cont = bool(CONT.search(prev.get("ln") or ""))
        n = max(1, round(-dp))
        notes.append(f"{prev.get('ln') or 'drugo takmičenje'} prije {n} {dana(n)}")
        out.update({"boost": 2 if cont else 1, "impact": 2 if cont else 1, "type": "guzva"})
    if nxt and dn is not None and dn <= 4.6 and nxt.get("lg") and nxt["lg"] != f["lid"]:
        cont = bool(CONT.search(nxt.get("ln") or ""))
        n = max(1, round(dn))
        notes.append(f"{nxt.get('ln') or 'drugo takmičenje'} za {n} {dana(n)}")
        out.update({"boost": max(out["boost"], 1), "impact": max(out["impact"], 2 if cont else 1),
                    "type": out["type"] or "rotacija"})
    if not notes and sum(1 for m in past[:4] if (days(m) or -99) > -8.2) >= 2:
        notes.append("treći meč u 8 dana")
        out.update({"impact": 1, "type": "guzva"})
    out["note"] = "; ".join(notes) or None
    return out


# ---------- velike kvote (slabija ekipa pobjeđuje) ----------
# Vjerovatnoće 1/X/2 po razlici snage (korak 0,25), izmjereno na 3.552 odigrane utakmice (25.9.–8.11.2026).
UPSET_P = {-6: [0.257, 0.184, 0.559], -5: [0.285, 0.213, 0.502], -4: [0.329, 0.232, 0.439], -3: [0.363, 0.257, 0.379],
           -2: [0.398, 0.261, 0.341], -1: [0.419, 0.271, 0.311], 0: [0.447, 0.268, 0.286], 1: [0.465, 0.278, 0.257],
           2: [0.489, 0.275, 0.237], 3: [0.502, 0.278, 0.22], 4: [0.535, 0.257, 0.208], 5: [0.559, 0.243, 0.198],
           6: [0.622, 0.197, 0.181]}


def team_rows_scored(team, ms):
    """Utakmice ekipe s rezultatom iz njene perspektive: (domaća?, golovi za, golovi protiv)."""
    out = []
    for m in ms:
        r = HT.get(str(m["id"]))
        if isinstance(r, list) and r[2] is not None:
            home = m["h"] == team
            out.append((home, r[2] if home else r[3], r[3] if home else r[2]))
    return out


def ppg(rows):
    return sum(3 if f > a else (1 if f == a else 0) for _, f, a in rows) / len(rows) if rows else None


def strength(fx, ctx):
    """Forma obje ekipe (zadnjih 15, sva takmičenja) -> (vjerovatnoće 1/X/2, redovi po ekipi, redovi na terenu)."""
    rows = {r: team_rows_scored(t, ctx["hist"][t][:15]) for r, t in (("h", fx["h"]), ("a", fx["a"]))}
    if min(len(rows["h"]), len(rows["a"])) < 8:
        return None
    venue = {"h": [x for x in rows["h"] if x[0]][:8], "a": [x for x in rows["a"] if not x[0]][:8]}

    def rating(r):
        g = sum(f - a for _, f, a in rows[r]) / len(rows[r])
        return ppg(rows[r]) + 0.35 * g + 0.3 * ((ppg(venue[r]) or ppg(rows[r])) - ppg(rows[r]))
    diff = rating("h") - rating("a")
    return UPSET_P[max(-6, min(6, round(diff / 0.25)))], rows, venue


def three_for_three(fx, ctx):
    """K2: domaćin je dobio zadnja 2 međusobna na svom terenu (zadnje 3 godine).
    Provjera: 65% pobjeda domaćina (54 utakmice) naspram 47% očekivanih po formi; jače kad je i gost dobio kod sebe."""
    def ft(m):
        r = HT.get(str(m["id"]))
        return (r[2], r[3]) if isinstance(r, list) and r[2] is not None else None
    lim = (fx["utc"] - dt.timedelta(days=1100)).strftime("%Y-%m-%dT%H:%M")
    h2h = [m for m in ctx["h2h"] if m["t"][:16] >= lim and ft(m)]
    here = [m for m in h2h if m["h"] == fx["h"]][:2]
    if len(here) < 2 or not all(ft(m)[0] > ft(m)[1] for m in here):
        return None
    there = [m for m in h2h if m["h"] == fx["a"]][:1]
    both = bool(there) and ft(there[0])[0] > ft(there[0])[1]
    st = strength(fx, ctx)
    odds = round(0.92 / st[0][0], 2) if st else None
    why = [f"{fx['hn']} dobio zadnja 2 međusobna kod kuće: " +
           "; ".join(f"{ft(m)[0]}:{ft(m)[1]} ({dmy(m['t'])})" for m in here)]
    if both:
        why.append(f"{fx['an']} dobio zadnji kod sebe: {ft(there[0])[0]}:{ft(there[0])[1]} ({dmy(there[0]['t'])}), pravo 3 za 3")
    return {"pick": "1", "estOdds": odds, "both": both, "why": why}


def upset(fx, ctx):
    """Slabija ekipa s procijenjenom kvotom 3,5+ i obrascem koji joj ide u prilog.
    Provjera: obrasci daju ~28% pobjeda slabije ekipe naspram ~22% inače (stabilno u obje polovine perioda)."""
    st = strength(fx, ctx)
    if not st:
        return None
    p, rows, venue = st
    dog = "h" if p[0] < p[2] else "a"
    fav = "a" if dog == "h" else "h"
    odds = round(0.92 / (p[0] if dog == "h" else p[2]), 2)
    if odds < CFG.get("upsetMinOdds", 3.5):
        return None
    fav_v5 = ppg(venue[fav][:5])
    dog_up = ppg(rows[dog][:5]) - ppg(rows[dog])
    fav_down = ppg(rows[fav]) - ppg(rows[fav][:5])
    why = []
    if fav_v5 is not None and fav_v5 <= 1.2:
        why.append(f"{fx['hn'] if fav == 'h' else fx['an']} slab {'kod kuće' if fav == 'h' else 'u gostima'}: "
                   f"{fav_v5:.1f} bod po meču u zadnjih 5".replace(".", ","))
    if dog_up >= 0.4 and fav_down >= 0.4:
        why.append(f"{fx['hn'] if dog == 'h' else fx['an']} u usponu (+{dog_up:.1f}), "
                   f"{fx['hn'] if fav == 'h' else fx['an']} u padu (−{fav_down:.1f} boda po meču)".replace(".", ","))
    return {"pick": "1" if dog == "h" else "2", "estOdds": odds, "flag": bool(why), "why": why,
            "dog": fx["hn"] if dog == "h" else fx["an"]}


def analyse(fx, ctx):
    H, A = fx["h"], fx["a"]
    team = {"h": (H, fx["hn"]), "a": (A, fx["an"])}
    h15 = {r: with_ht(ctx["hist"][team[r][0]], 15) for r in team}
    win = {r: {"home5": [m for m in h15[r] if m["h"] == team[r][0]][:5],
               "away5": [m for m in h15[r] if m["a"] == team[r][0]][:5],
               "last10": h15[r][:10], "last15": h15[r]} for r in team}
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
    signals = []
    ev = {"role": {}, "h2h": {}, "prone": {}, "cong": {}}
    for sel in SELS:
        c = stats[sel]
        # R1) Preokreti ekipe (domaćin ili gost, kod kuće ili u gostima) + isti preokret u jednom od
        #     zadnjih međusobnih na bilo kojem terenu (primjer Técnico U – Manta)
        cands = sorted(((c[r][w], r, w) for r in team for w in WIN if c[r][w] >= 2),
                       key=lambda x: (-x[0], x[1] != "h", list(WIN).index(x[2])))
        hit = next((m for m in recent if h2h_same(m, sel)), None)
        if cands and hit:
            k, r, w = cands[0]
            tail = f"{k}× {EVW[need_event(r, sel)]} ({WIN[w]}); međusobni {dmy(hit['t'])} {htft_of(hit)}"
            note = f"{team[r][1]} {tail}"
            if len(note) > 90:
                note = f"{'Domaćin' if r == 'h' else 'Gost'} {tail}"
            signals.append(sig("R1", sel, 3, "korisnik", note, url))
            ev["h2h"][sel] = hit
        # R2) Ponovljeni preokret: zadnji međusobni završio s istom oznakom (1/2 ili 2/1) + preokret u
        #     zadnjem meču jedne od ekipa (primjer Wadi Degla – Kahraba)
        if h2h and htft_of(h2h[0]) == sel:
            last = [(team[r][1], h15[r][0]) for r in team if h15[r] and team_event(h15[r][0], team[r][0])]
            if last:
                signals.append(sig("R2", sel, 1, "korisnik",
                                   f"Zadnji međusobni {htft_of(h2h[0])} ({dmy(h2h[0]['t'])}); "
                                   f"{last[0][0]} u zadnjem meču {htft_of(last[0][1])}", url))
                ev["h2h"].setdefault(sel, h2h[0])
        # R3) Zbir preokreta obje ekipe u zadnjih 10 (+1 ako liga često ima taj preokret)
        th, ta = c["h"]["last10"], c["a"]["last10"]
        if th + ta >= 3:
            s = 2 if th + ta >= 5 else 1
            rate = lr.get("r12" if sel == "1/2" else "r21")
            extra = ""
            if rate is not None and lr.get("n", 0) >= 40 and rate >= 0.05:
                s, extra = min(3, s + 1), f"; liga {round(rate * 100)}%"
            signals.append(sig("R3", sel, s, "web",
                               f"Preokreti {sel} u zadnjih 10: {fx['hn']} {th}, {fx['an']} {ta}{extra}", url))
        lines = []
        order = [cands[0][1]] if cands else []
        order += [r for r in ("h", "a") if r not in order]
        for r in order:
            best_w = max(WIN, key=lambda w: (c[r][w], -list(WIN).index(w)))
            if c[r][best_w]:
                lines.append((r, best_w, hits(r, best_w, sel)[:2]))
        ev["role"][sel] = lines
    # Smjer po snazi: kad je gost jasno jači, preokret je češće 1/2 (3,8% naspram 2,1%), kad je jači domaćin 2/1.
    stp = strength(fx, ctx)
    strong_dir = None
    if stp:
        p1, _, p2 = stp[0]
        if p2 > p1 - 0.06:
            strong_dir = "1/2"
        elif p1 - p2 >= 0.16:
            strong_dir = "2/1"
    # R4) Ekipa sklona preokretima: bilo koji preokret (1/2 ili 2/1) u zadnjih 15 mečeva, svježi vrijede više
    #     (primjer Torreense – União de Leiria: 3 u zadnjih 15, 2 u zadnja 3, zadnji meč preokret)
    feat = {}
    for r in ("h", "a"):
        T, name = team[r]
        revs = [(i, m, team_event(m, T)) for i, m in enumerate(h15[r]) if team_event(m, T)]
        ev["revs"] = ev.get("revs", {})
        ev["revs"][r] = revs
        n3, n5, n10, n15 = (sum(1 for i, _, _ in revs if i < k) for k in (3, 5, 10, 15))
        last = bool(revs) and revs[0][0] == 0
        c_l = sum(1 for _, _, e in revs if e == "lostLead")
        c_c = len(revs) - c_l
        feat[r] = [n3, n5, n10, n15, int(last), c_l, c_c, len(h15[r])]
        s = 0
        if len(h15[r]) >= 6:
            if n10 >= 2:
                s = 1
            if n15 >= 3 or n5 >= 2 or (last and n10 >= 2):
                s = 2
            if n3 >= 2 or n15 >= 4 or (last and (n15 >= 3 or n5 >= 2)):
                s = 3
        if not s:
            continue
        event = "lostLead" if c_l > c_c else ("comeback" if c_c > c_l else revs[0][2])
        parts = [f"{n15} preokreta u zadnjih {len(h15[r])}"]
        if n3 >= 2:
            parts.append(f"{n3} u zadnja 3")
        elif n5 >= 2:
            parts.append(f"{n5} u zadnjih 5")
        if last:
            parts.append("zadnji meč preokret")
        note = f"{name}: " + ", ".join(parts)
        if len(note) > 90:
            note = f"{'Domaćin' if r == 'h' else 'Gost'}: " + ", ".join(parts)
        signals.append(sig("R4", strong_dir or sel_for(r, event), s, "korisnik", note, url, both=True))
        ev["prone"][r] = [m for _, m, _ in revs[:3]]
    # R6) Svjež preokret: jedna ekipa je imala preokret u zadnjem meču, druga bar jedan u zadnjih 10
    #     (primjeri Yokohama – Kashiwa, Leamington – Bromsgrove). Slab signal: kandidat, ne prijedlog.
    for r, o in (("h", "a"), ("a", "h")):
        if feat.get(r) and feat.get(o) and feat[r][4] and feat[o][2] >= 1 and feat[r][7] >= 6 and feat[o][7] >= 6:
            ev["prone"].setdefault(r, [m for _, m, _ in ev["revs"][r][:2]])
            ev["prone"].setdefault(o, [m for _, m, _ in ev["revs"][o][:1]])
            first = strong_dir or sel_for(r, ev["revs"][r][0][2])
            note = (f"{team[r][1]} preokret u zadnjem meču, {team[o][1]} {feat[o][2]}× u zadnjih 10")
            if len(note) > 90:
                note = f"Zadnji meč preokret ({'domaćin' if r == 'h' else 'gost'}), druga ekipa {feat[o][2]}× u zadnjih 10"
            signals.append(sig("R6", first, 1, "korisnik", note, url, both=True))
            break
    # R7) Zadnji međusobni završio preokretom, a bar jedna ekipa ima preokret u zadnjih 10
    #     (Harisov primjer Yokohama F. Marinos – Kashima Antlers, 11.11.2026). Slab signal: kandidat, ne prijedlog.
    if h2h and htft_of(h2h[0]) in SELS and not any(x["rule"] == "R2" for x in signals):
        cnt = {r: sum(1 for m in h15[r][:10] if team_event(m, team[r][0])) for r in team}
        if cnt["h"] + cnt["a"] >= 1:
            m = h2h[0]
            sel = as_pattern(m, H)
            who = ", ".join(f"{team[r][1]} {cnt[r]}×" for r in ("h", "a") if cnt[r])
            note = f"Zadnji međusobni {htft_of(m)} ({dmy(m['t'])}); u zadnjih 10: {who}"
            signals.append(sig("R7", sel, 1, "korisnik", note, url))
            ev["h2h"].setdefault(sel, m)
    # R5) Gust raspored (kup ili Evropa do 4 dana prije/poslije): umor i rotacija, ekipa lakše ispusti vodstvo.
    #     Broji se samo uz neki drugi signal preokreta.
    cong = ctx.get("cong") or {}
    if signals:
        for r in ("h", "a"):
            cg = cong.get(team[r][0]) or {}
            if cg.get("boost"):
                note = f"{team[r][1]}: {cg['note']} (umor, rotacija)"
                if len(note) > 90:
                    note = f"{'Domaćin' if r == 'h' else 'Gost'}: {cg['note']} (umor, rotacija)"
                signals.append(sig("R5", "1/2" if r == "h" else "2/1", cg["boost"], "korisnik", note, url))
                ev["cong"][r] = cg["note"]
    pts = {sel: 0.0 for sel in SELS}
    for x in signals:
        k = mult(x["rule"], x["s"])
        pts[x["dir"]] += 8 * x["s"] * k
        if x.get("both") and x["s"] >= 2:
            pts[other(x["dir"])] += 8 * (x["s"] - 1) * k
    # golovi: u mečevima s malo golova preokret je rjeđi, s puno golova nešto češći
    feat["g"] = None
    if ctx.get("means") and min(len(h15["h"]), len(h15["a"])) >= 6:
        p12, p21 = goal_model(fx, h15, ctx["means"])
        lift = (p12 + p21) / P_MEAN
        feat["g"] = round(lift, 2)
        gf = GOALS_LOW[1] if lift <= GOALS_LOW[0] else (GOALS_HIGH[1] if lift >= GOALS_HIGH[0] else 1.0)
        if gf != 1.0:
            ev["goals"] = "malo" if gf < 1 else "puno"
            pts = {sel: v * gf for sel, v in pts.items()}
    best = max(SELS, key=lambda s: pts[s])
    score = min(99, round(pts[best]))
    alt = other(best) if round(pts[other(best)]) >= CFG.get("listMin", 8) else None
    cand = {"sel": best, "score": score}
    if alt:
        cand["alt"] = alt
    feat["x"] = [len(h2h), htft_of(h2h[0]) if h2h else None]
    feat["c"] = [(cong.get(team[r][0]) or {}).get("rest") for r in ("h", "a")] + \
                [(cong.get(team[r][0]) or {}).get("boost", 0) for r in ("h", "a")]
    return {
        "signals": signals, "cand": cand if score else None,
        "htftTip": cand if score >= CFG.get("htftMin", 20) else None,
        "score": score, "best": best if score else None, "pts": [round(pts[s], 1) for s in SELS],
        "stats": stats, "ev": ev, "feat": feat, "cong": cong,
        "hist": h15, "h2hRows": h2h,
        "h2h": [line(m) for m in h2h[:6]],
        "homeLast": [line(m) for m in h15["h"][:15]],
        "awayLast": [line(m) for m in h15["a"][:15]],
    }


def reasoning(fx, a):
    """Kao Harisov opis: čiji preokreti i kada, međusobni s istim preokretom, gust raspored."""
    if not a["signals"]:
        return None
    sel = a["best"]
    e = a["ev"]
    name = {"h": fx["hn"], "a": fx["an"]}
    tid = {"h": fx["h"], "a": fx["a"]}

    def item(m, t):
        who = f"protiv {m['an']}" if m["h"] == t else f"{m['hn']}–{m['an']}"
        return f"{who} {score_txt(m)} {dmy(m['t'], False)}"
    m = e["h2h"].get(sel)
    shown = set()

    def items(ms, t):
        shown.update(x["id"] for x in ms)
        return "; ".join(item(x, t) + (" (međusobni)" if m and x["id"] == m["id"] else "") for x in ms)
    out = [f"{name[r]}, preokreti: " + items(e["prone"][r], tid[r]) for r in ("h", "a") if e["prone"].get(r)]
    if not out:
        out = [f"{name[r]}: {EVW[need_event(r, sel)]} ({WIN[w]}) – " + items(ms, tid[r])
               for r, w, ms in e["role"].get(sel, [])]
    if m and m["id"] not in shown:
        where = "kod domaćina" if m["h"] == fx["h"] else "kod gosta"
        out.append(f"Međusobni {dmy(m['t'])} {where}: {m['hn']}–{m['an']} {score_txt(m)}")
    out += [f"{name[r]}: {e['cong'][r]}" for r in ("h", "a") if e["cong"].get(r)]
    if e.get("goals") == "malo":
        out.append("Malo golova u mečevima ovih ekipa, a tada je preokret upola rjeđi")
    elif e.get("goals") == "puno":
        out.append("Puno golova u mečevima ovih ekipa, što ide u prilog preokretu")
    text = ". ".join(x.rstrip(".") for x in out)
    return (text if len(text) <= 380 else text[:377].rstrip() + "…") + "."


# ---------- uzorci i provjera pravila ----------
def month_path(day):
    return os.path.join(DATA, "samples", f"{day[:7]}.json")


def save_samples(rows):
    """rows: {fixtureId: uzorak}. Upis po mjesecima; postojeći ishod se ne briše."""
    by = {}
    for fid, s in rows.items():
        by.setdefault(month_path(s["d"]), {})[str(fid)] = s
    for path, new in by.items():
        cur = jload(path, {})
        for fid, s in new.items():
            if s.get("o") is None and (cur.get(fid) or {}).get("o") is not None:
                s = {**s, "o": cur[fid]["o"]}
            cur[fid] = s
        jsave(path, cur)


def outcome_of(fid):
    """'1/2', 'X/1' ... | 'void' | 'nd' (nema poluvremena) | None (još nema rezultata)."""
    r = HT.get(str(fid))
    if r is None:
        return None
    if r == "c":
        return "void"
    if not isinstance(r, list) or r[0] is None:
        return "nd"
    return res(r[0], r[1]) + "/" + res(r[2], r[3])


def fill_outcomes(now):
    """Ishod za sve ranije pregledane utakmice (ne samo kandidate), da pravila uče i iz promašenih."""
    for path in sorted(glob.glob(os.path.join(DATA, "samples", "*.json")))[-2:]:
        cur = jload(path, {})
        todo = []
        for fid, s in cur.items():
            ko = parse_utc(s.get("t"))
            if s.get("o") is None and ko and ko + dt.timedelta(hours=2, minutes=30) < now < ko + dt.timedelta(days=8):
                todo.append(fid)
        if not todo:
            continue
        for fid in todo:
            if HT.get(fid) == "n":
                del HT[fid]
        fetch_ht(todo)
        for fid in todo:
            cur[fid]["o"] = outcome_of(fid)
        jsave(path, cur)


def calibrate():
    """Iz svih uzoraka s ishodom: koliko često pogađa koje pravilo u odnosu na prosjek lige -> faktori bodova."""
    rows = []
    for path in sorted(glob.glob(os.path.join(DATA, "samples", "*.json"))):
        rows += [s for s in jload(path, {}).values() if re.fullmatch(r"[12X]/[12X]", str(s.get("o") or ""))]
    MULT.clear()
    if not rows:
        return None
    n = len(rows)
    glob_rate = {sel: sum(1 for s in rows if s["o"] == sel) / n for sel in SELS}
    by_lg = {}
    for s in rows:
        c = by_lg.setdefault(s["lg"], {"n": 0, "1/2": 0, "2/1": 0})
        c["n"] += 1
        if s["o"] in SELS:
            c[s["o"]] += 1

    def base(lg, sel):
        c = by_lg[lg]
        return (c[sel] + 30 * glob_rate[sel]) / (c["n"] + 30)
    agg = {}
    for s in rows:
        for rule, d, st in s.get("s") or []:
            # pogodak: tačan smjer; za pravila iz BOTH bilo koji preokret (pa je i prosjek zbir oba smjera)
            b = base(s["lg"], d) + (base(s["lg"], other(d)) if rule in BOTH else 0)
            hit = int(s["o"] in SELS) if rule in BOTH else int(s["o"] == d)
            for key in (rule, f"{rule}:{st}"):
                a = agg.setdefault(key, {"n": 0, "hits": 0, "sel": 0, "any": 0, "exp": 0.0, "pri": 0.0})
                a["n"] += 1
                a["hits"] += hit
                a["sel"] += int(s["o"] == d)
                a["any"] += int(s["o"] in SELS)
                a["exp"] += b
                a["pri"] += b * PRIOR[st]
    rules = {}
    for key, a in sorted(agg.items()):
        if a["exp"] <= 0:
            continue
        b = a["exp"] / a["n"]
        prior = a["pri"] / a["exp"]                         # pretpostavljeni omjer prema prosjeku
        est = (a["hits"] + prior * PRIOR_E) / (a["exp"] + PRIOR_E)   # izmjereni omjer, povučen ka pretpostavci
        k = round(min(1.6, max(0.4, est / prior)), 2)
        rules[key] = {"n": a["n"], "hits": a["hits"], "sel": a["sel"], "any": a["any"],
                      "rate": round(a["hits"] / a["n"], 4), "base": round(b, 4),
                      "lift": round(a["hits"] / a["exp"], 2), "est": round(est, 2), "mult": k}
        MULT[key] = k
    stats = {"at": dt.datetime.now(UTC).isoformat(timespec="seconds"), "n": n,
             "base": {sel: round(glob_rate[sel], 4) for sel in SELS}, "rules": rules}
    jsave(os.path.join(DATA, "rule_stats.json"), stats, pretty=True)
    return stats


def rules_brief(stats):
    """Kratka tabela za Dosije (meta/scanner.rules)."""
    if not stats:
        return None
    out = []
    for rid, name in RULES.items():
        r = stats["rules"].get(rid)
        if r:
            out.append({"id": rid, "name": name, "n": r["n"], "hits": r["hits"], "sel": r["sel"], "any": r["any"],
                        "both": rid in BOTH,
                        "base": r["base"], "lift": r["lift"], "mult": r["mult"]})
    return {"n": stats["n"], "base": stats["base"], "at": stats["at"], "rules": out}


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
        due = []
        for c in cand["matches"]:
            fid = c["fixtureId"]
            ko = parse_utc(c.get("utc"))
            if (fid in old and old[fid]["status"] in ("FT", "NOHT", "VOID")) or \
                    (ko and now < ko + dt.timedelta(hours=2, minutes=30)):
                continue
            due.append(c)
        if not due:
            continue
        for c in due:
            if HT.get(str(c["fixtureId"])) == "n":
                del HT[str(c["fixtureId"])]
        fetch_ht([c["fixtureId"] for c in due])
        changed = False
        for c in due:
            fid = c["fixtureId"]
            r = HT.get(str(fid))
            if r is None or r == "n":
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


# ---------- pregled ----------
def fixtures_for(days, by_id, keep):
    out = {}
    for d in days:
        js = get("matches", date=d.strftime("%Y%m%d"))
        for lg in (js or {}).get("leagues", []):
            lid = as_int(lg.get("primaryId")) or as_int(lg.get("id"))
            if lid not in by_id:
                continue
            for m in lg.get("matches", []):
                st = m.get("status") or {}
                t = parse_utc(st.get("utcTime"))
                if not t or not keep(st, t):
                    continue
                fid = as_int(m.get("id"))
                h, a = as_int((m.get("home") or {}).get("id")), as_int((m.get("away") or {}).get("id"))
                if fid and h and a:
                    out[fid] = {"id": fid, "lid": lid, "utc": t, "t": t.strftime("%Y-%m-%dT%H:%M"),
                                "h": h, "hn": tname(m.get("home")), "a": a, "an": tname(m.get("away")),
                                "stage": lg.get("name")}
    return out


def league_rate(pool, before):
    known = [htft_of(m) for m in pool if m["fin"] and m["t"] < before and str(m["id"]) in HT]
    known = [x for x in known if x]
    if not known:
        return None
    return {"n": len(known), "r12": round(known.count("1/2") / len(known), 3),
            "r21": round(known.count("2/1") / len(known), 3)}


def screen(fx_list, by_id, live):
    """Istorija obje ekipe, međusobni, poluvremena i pravila za svaku utakmicu. -> ([(utakmica, analiza)], potpuno?)"""
    pools = {lid: league_pool(by_id[lid]) for lid in dict.fromkeys(f["lid"] for f in fx_list)}
    pmap(team_rows, dict.fromkeys(t for f in fx_list for t in (f["h"], f["a"])))

    # zapisnik same utakmice: međusobni (sve sezone i takmičenja), stadion; kod provjere unazad i rezultat
    def detail(f):
        if out_of_time() or not CFG.get("fixtureH2H", True):
            return None
        js = get("matchDetails", matchId=f["id"])
        if not js:
            return None
        content = js.get("content") or {}
        hb = content.get("h2h") or {}
        st = ((content.get("matchFacts") or {}).get("infoBox") or {}).get("Stadium")
        return {"h2h": rows_of(hb.get("matches") if isinstance(hb, dict) else None),
                "venue": st.get("name") if isinstance(st, dict) else None,
                "res": None if live else parse_result(js)}
    dets = pmap(detail, fx_list)
    STATS["details"] += len(fx_list)
    ctxs, tiers = {}, [[], [], []]
    for f, det in zip(fx_list, dets):
        pool = pools[f["lid"]]
        row = next((r for r in pool if r["id"] == f["id"]), None)
        f["url"] = WEB + row["url"] if row and row.get("url") else f"{WEB}/match/{f['id']}"
        det = det or {}
        if det.get("res") is not None:
            HT[str(f["id"])] = det["res"]
        allm = {t: team_matches(t, pool) for t in (f["h"], f["a"])}
        hist = {t: [m for m in allm[t] if m["fin"] and m["t"][:16] < f["t"]][:18] for t in allm}
        h2h = [m for m in allm[f["h"]] if m["fin"] and f["a"] in (m["h"], m["a"]) and m["t"][:16] < f["t"]]
        h2h += [m for m in det.get("h2h") or []
                if m["fin"] and {m["h"], m["a"]} == {f["h"], f["a"]} and m["t"][:16] < f["t"]]
        f["venue"] = det.get("venue")
        seen, h2u = set(), []
        for m in sorted(h2h, key=lambda m: m["t"], reverse=True):
            if m["id"] not in seen:
                seen.add(m["id"])
                h2u.append(m)
        ctxs[f["id"]] = {"hist": hist, "h2h": h2u[:8], "cong": {t: congestion(t, allm[t], f) for t in allm}}
        tiers[0] += [m["id"] for m in h2u[:6]]
        tiers[1] += [m["id"] for t in hist for m in hist[t][:6]]
        tiers[2] += [m["id"] for t in hist for m in hist[t][:17]]
    # poluvremena: prvo međusobni, pa zadnjih 6, pa zadnjih 15 svake ekipe
    complete = True
    for ids in tiers:
        complete = fetch_ht(ids) and complete
    out = []
    for f in fx_list:
        ctx = {**ctxs[f["id"]], "league_rates": league_rate(pools[f["lid"]], f["t"]),
               "means": league_means(pools[f["lid"]], f["t"])}
        a = analyse(f, ctx)
        a["upset"] = upset(f, ctx)
        a["k2"] = three_for_three(f, ctx)
        out.append((f, a))
    return out, pools, complete


def sample_of(f, a, o=None, bt=False):
    s = {"d": f["utc"].astimezone(TZ).date().isoformat(), "t": f["utc"].isoformat(timespec="minutes"),
         "lg": f["lid"], "m": f"{f['hn']} – {f['an']}", "s": [[x["rule"], x["dir"], x["s"]] for x in a["signals"]],
         "f": a["feat"], "p": a["pts"], "o": o}
    u = a.get("upset")
    if u:
        s["u"] = [u["pick"], u["estOdds"], int(u["flag"])]
    if a.get("k2"):
        s["k2"] = [1, int(a["k2"]["both"])]
    if bt:
        s["bt"] = 1
    return s


def make_doc(f, a, lg, now):
    local = f["utc"].astimezone(TZ)
    league = f"{COUNTRY.get(lg['cc'], lg['cc'])} · {lg['name']}"
    warnings = []
    for r, key in (("home", f["h"]), ("away", f["a"])):
        cg = a["cong"].get(key) or {}
        if cg.get("note"):
            warnings.append({"team": r, "type": cg["type"] or "guzva", "note": cg["note"],
                             "impact": cg["impact"] or 1, "src": [f["url"]]})
    doc = {
        "date": local.date().isoformat(), "kickoff": local.strftime("%H:%M"), "league": league,
        "home": f["hn"], "away": f["an"], "pairId": "__".join(sorted([slug(f["hn"]), slug(f["an"])])),
        "venue": f.get("venue"), "neutral": False, "signals": a["signals"], "warnings": warnings,
        "tip": "PASS", "score": None, "htftTip": a["htftTip"], "cand": a["cand"], "reasoning": reasoning(f, a),
        "odds": None, "htftOdds": None, "status": "pending", "fbPending": False,
        "sources": [f["url"]], "createdAt": now.isoformat(timespec="seconds"),
        "run": {"screener": True, "source": "fotmob", "fixtureId": f["id"]}, "fixtureId": f["id"],
    }
    return {"fixtureId": f["id"], "docId": f"{local.date().isoformat()}_{slug(f['hn'])}_{slug(f['an'])}",
            "utc": f["utc"].isoformat(timespec="minutes"), "league": league, "stage": f.get("stage"),
            "home": f["hn"], "away": f["an"], "score": a["score"], "cand": a["cand"], "htftTip": a["htftTip"],
            "stats": a["stats"], "h2h": a["h2h"], "homeLast": a["homeLast"], "awayLast": a["awayLast"], "doc": doc}


def run_backtest():
    """Provjera unazad: za odigrane utakmice u periodu izračunaj šta bi pravila rekla prije početka i uporedi s ishodom."""
    req = jload(os.path.join(ROOT, "backtest_request.json"), None) or {}
    d0, d1 = dt.date.fromisoformat(req["from"]), dt.date.fromisoformat(req["to"])
    CFG["maxMinutes"] = req.get("maxMinutes", 150)
    CFG["maxDetailCalls"] = req.get("maxDetailCalls", 40000)
    for k in ("workers", "pause"):
        if k in req:
            CFG[k] = req[k]
    if req.get("noDetail"):
        CFG["fixtureH2H"] = False
    start = dt.datetime.combine(d0, dt.time(0), TZ).astimezone(UTC)
    end = dt.datetime.combine(d1 + dt.timedelta(days=1), dt.time(0), TZ).astimezone(UTC)
    leagues = resolve_leagues()
    if req.get("leagues"):
        leagues = [lg for lg in leagues if lg["id"] in set(req["leagues"])]
    by_id = {lg["id"]: lg for lg in leagues}
    days = [start.date() + dt.timedelta(days=i) for i in range((end.date() - start.date()).days + 1)]
    live = bool(req.get("live"))   # i utakmice koje se još igraju: pravila računaju samo s podacima prije početka
    fixtures = fixtures_for(days, by_id, lambda st, t: (st.get("finished") or (live and st.get("started")))
                            and not st.get("cancelled") and not st.get("awarded") and start <= t < end)
    fx_list = sorted(fixtures.values(), key=lambda f: f["utc"])
    res_, _, complete = screen(fx_list, by_id, live=False)
    name = f"{d0.isoformat()}_{d1.isoformat()}"
    report = {"from": d0.isoformat(), "to": d1.isoformat(), "leagues": len(leagues), "fixtures": len(fx_list),
              "complete": complete, "calls": STATS["calls"], "errors": STATS["errors"],
              "minutes": round((time.time() - T0) / 60, 1)}
    if not complete:
        report["note"] = "Nisu povučena sva poluvremena (limit); pokreni ponovo, nastavlja odakle je stalo."
        jsave(os.path.join(ROOT, "backtest", f"{name}.json"), report, pretty=True)
        print(json.dumps(report, ensure_ascii=False))
        return 3
    rows, table = {}, []
    for f, a in res_:
        o = outcome_of(f["id"])
        rows[f["id"]] = sample_of(f, a, o, bt=True)
        lg = by_id[f["lid"]]
        table.append({"d": rows[f["id"]]["d"], "lg": f"{lg['cc']} {lg['name']}", "m": rows[f["id"]]["m"], "o": o,
                      "score": a["score"], "sel": a["best"], "alt": (a["cand"] or {}).get("alt"),
                      "why": [x["note"] for x in a["signals"]], "txt": reasoning(f, a),
                      "home": a["homeLast"] if req.get("verbose") else None,
                      "away": a["awayLast"] if req.get("verbose") else None})
    if not req.get("noSave"):
        save_samples(rows)
    if req.get("dump"):
        def enc(m, team, f):
            r, home = HT[str(m["id"])], m["h"] == team
            return [(f["utc"] - parse_utc(m["t"])).days, int(home), r[0] if home else r[1], r[1] if home else r[0],
                    r[2] if home else r[3], r[3] if home else r[2], int(m.get("lg") == f["lid"])]
        dump = [{"id": f["id"], "d": rows[f["id"]]["d"], "lg": f["lid"], "h": f["h"], "a": f["a"], "m": rows[f["id"]]["m"],
                 "o": HT.get(str(f["id"])), "s": rows[f["id"]]["s"], "c": a["feat"]["c"],
                 "H": [enc(m, f["h"], f) for m in a["hist"]["h"]], "A": [enc(m, f["a"], f) for m in a["hist"]["a"]],
                 "X": [[(f["utc"] - parse_utc(m["t"])).days, int(m["h"] == f["h"])] + HT[str(m["id"])]
                       for m in a["h2hRows"][:6]]} for f, a in res_]
        os.makedirs(os.path.join(ROOT, "backtest"), exist_ok=True)
        with gzip.open(os.path.join(ROOT, "backtest", f"dump_{name}.json.gz"), "wt", encoding="utf-8") as fh:
            json.dump(dump, fh, ensure_ascii=False, separators=(",", ":"))
    ok = [t for t in table if re.fullmatch(r"[12X]/[12X]", str(t["o"] or ""))]

    def band(lo, hi):
        g = [t for t in ok if lo <= t["score"] < hi]
        return {"n": len(g), "hitSel": sum(1 for t in g if t["o"] == t["sel"]),
                "hitAlt": sum(1 for t in g if t["alt"] and t["o"] == t["alt"]),
                "anyReversal": sum(1 for t in g if t["o"] in SELS)}
    t_min, l_min = CFG.get("htftMin", 20), CFG.get("listMin", 8)
    report.update({
        "withOutcome": len(ok),
        "reversals": {sel: sum(1 for t in ok if t["o"] == sel) for sel in SELS},
        "bands": {"tips": band(t_min, 1000), "candidates": band(l_min, t_min), "rest": band(0, l_min)},
        "byScore": {f"{lo}-{hi - 1}": band(lo, hi) for lo, hi in ((8, 16), (16, 24), (24, 32), (32, 40), (40, 56), (56, 1000))},
        "rules": (calibrate() or {}).get("rules"),
        "flagged": sorted([t for t in table if t["score"] >= l_min], key=lambda t: (-t["score"], t["d"]))[:400],
        "missed": [t for t in ok if t["o"] in SELS and t["score"] < l_min][:400],
        "all": table if req.get("verbose") else None,
    })
    jsave(os.path.join(ROOT, "backtest", f"{name}.json"), report, pretty=True)
    print(json.dumps({k: report[k] for k in report if k not in ("flagged", "missed")}, ensure_ascii=False))
    return 0


def main():
    global CFG, HT
    CFG = jload(os.path.join(ROOT, "config.json"), {})
    HT = jload(os.path.join(DATA, "ht.json"), {})
    if "--backtest" in sys.argv:
        try:
            return run_backtest()
        finally:
            jsave(os.path.join(DATA, "ht.json"), HT)
    now = dt.datetime.now(UTC)
    today = now.astimezone(TZ).date().isoformat()
    end = now + dt.timedelta(hours=CFG.get("windowHours", 24))
    report = {"date": today, "generatedAt": now.isoformat(timespec="seconds"),
              "window": [now.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")]}
    matches = []
    try:
        settle(now)
        fill_outcomes(now)
        report["rules"] = rules_brief(calibrate())
        leagues = resolve_leagues()
        by_id = {lg["id"]: lg for lg in leagues}
        days = sorted({now.date(), end.date(), now.astimezone(TZ).date(), end.astimezone(TZ).date()})
        fixtures = fixtures_for(days, by_id, lambda st, t: not (st.get("started") or st.get("finished")
                                                                 or st.get("cancelled")) and now <= t < end)
        fx_all = sorted(fixtures.values(), key=lambda f: (by_id[f["lid"]]["prio"], f["utc"]))
        fx_list = fx_all[: CFG.get("maxFixtures", 80)]
        report.update({"leagues": len(leagues), "fixturesSeen": len(fx_all), "analysed": len(fx_list)})
        res_, pools, complete = screen(fx_list, by_id, live=True)
        if not complete:
            NOTES.append("Dio poluvremena nije povučen (limit); dopunit će se pri sljedećem pregledu.")

        # udio preokreta po ligi iz poznatih poluvremena
        ls = jload(os.path.join(DATA, "league_stats.json"), {})
        for lid, pool in pools.items():
            r = league_rate(pool, "9999")
            if r:
                ls[str(lid)] = {**r, "name": f"{COUNTRY.get(by_id[lid]['cc'], by_id[lid]['cc'])} · {by_id[lid]['name']}",
                                "at": today}
        jsave(os.path.join(DATA, "league_stats.json"), ls, pretty=True)

        screened, rows, nominal, upsets, k2s = [], {}, 0, [], []
        for f, a in res_:
            lg = by_id[f["lid"]]
            if min(a["stats"]["n"][r]["last15"] for r in ("h", "a")) >= 8:
                nominal += 1
            rows[f["id"]] = sample_of(f, a)
            c = a["cand"] or {}
            screened.append([f["utc"].astimezone(TZ).strftime("%d.%m. %H:%M"), f"{lg['cc']} {lg['name']}",
                             f"{f['hn']} – {f['an']}", a["score"],
                             (c.get("sel") or "") + (" · " + c["alt"] if c.get("alt") else "")])
            if a["score"] >= CFG.get("listMin", 8):
                matches.append(make_doc(f, a, lg, now))
            if a.get("k2"):
                k2s.append({"fixtureId": f["id"], "utc": f["utc"].isoformat(timespec="minutes"),
                            "kickoff": f["utc"].astimezone(TZ).strftime("%H:%M"),
                            "date": f["utc"].astimezone(TZ).date().isoformat(),
                            "league": f"{COUNTRY.get(lg['cc'], lg['cc'])} · {lg['name']}",
                            "home": f["hn"], "away": f["an"], **a["k2"]})
            u = a.get("upset")
            if u and u["flag"]:
                upsets.append({"fixtureId": f["id"], "utc": f["utc"].isoformat(timespec="minutes"),
                               "kickoff": f["utc"].astimezone(TZ).strftime("%H:%M"),
                               "date": f["utc"].astimezone(TZ).date().isoformat(),
                               "league": f"{COUNTRY.get(lg['cc'], lg['cc'])} · {lg['name']}",
                               "home": f["hn"], "away": f["an"], **u})
        upsets.sort(key=lambda u: (-len(u["why"]), -u["estOdds"]))
        st = {"n": 0, "won": 0, "odds": 0.0}
        for path in glob.glob(os.path.join(DATA, "samples", "*.json")):
            for smp in jload(path, {}).values():
                u = smp.get("u")
                if u and u[2] and re.fullmatch(r"[12X]/[12X]", str(smp.get("o") or "")) and not smp.get("bt"):
                    st["n"] += 1
                    st["won"] += int(smp["o"][2] == u[0])
                    st["odds"] += u[1]
        report["upsetStats"] = {"n": st["n"], "won": st["won"],
                                "avgOdds": round(st["odds"] / st["n"], 2) if st["n"] else None}
        k2s.sort(key=lambda k: (not k["both"], k["utc"]))
        report["k2"] = k2s[:12]
        k = {"n": 0, "won": 0}
        for path in glob.glob(os.path.join(DATA, "samples", "*.json")):
            for smp in jload(path, {}).values():
                if smp.get("k2") and re.fullmatch(r"[12X]/[12X]", str(smp.get("o") or "")) and not smp.get("bt"):
                    k["n"] += 1
                    k["won"] += int(smp["o"][2] == "1")
        report["k2Stats"] = k
        report["upsets"] = upsets[: CFG.get("upsetMax", 12)]
        save_samples(rows)
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
    print(json.dumps({k: v for k, v in report.items() if k != "screened"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
