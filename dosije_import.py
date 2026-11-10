#!/usr/bin/env python3
"""Plan uvoza screenera u Dosije (koristi ga jutarnji Dosije zadatak).

python3 dosije_import.py [YYYY-MM-DD]  ->  /tmp/dosije_plan.json + kratak sažetak na ekranu

Plan:
  create   dokumenti za analyses/{docId} iz candidates/DATUM.json (upisati samo one kojih nema)
  settle   polja za zatvaranje ranijih kandidata iz results/*.json (samo dokumenti na čekanju)
  scanner  polja za meta/scanner
  dates    datumi novih dokumenata (za jedan db_query koji pokaže šta već postoji)
"""
import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Europe/Sarajevo")


def load(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def pl(n, one, few, many):
    """Bosanska množina: 1 utakmica, 2 utakmice, 5 utakmica."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def outcome(ft):
    h, a = (int(x) for x in ft.split("-"))
    return "H" if h > a else ("A" if h < a else "D")


def main():
    now = dt.datetime.now(TZ)
    today = sys.argv[1] if len(sys.argv) > 1 else now.date().isoformat()
    days = int(os.environ.get("DOSIJE_DAYS", "5"))
    plan = {"today": today, "create": [], "settle": [], "scanner": None, "dates": []}

    cand = load(os.path.join(ROOT, "candidates", f"{today}.json"))
    if cand:
        for m in cand.get("matches", []):
            plan["create"].append({"path": f"analyses/{m['docId']}", "fixtureId": m["fixtureId"], "data": m["doc"]})
        plan["dates"] = sorted({m["doc"]["date"] for m in cand.get("matches", [])})
        w = cand.get("window") or []
        win = [dt.datetime.fromisoformat(x).astimezone(TZ).date().isoformat() for x in w[:2]]
        tips = cand.get("tips", 0)
        n_fx, n_lg, n_c = cand.get("analysed", 0), cand.get("leagues", 0), cand.get("candidates", 0)
        notes = [f"Screener {dt.datetime.fromisoformat(cand['generatedAt']).astimezone(TZ):%d.%m. %H:%M}: "
                 f"pregledano {n_fx} {pl(n_fx, 'utakmica', 'utakmice', 'utakmica')} iz {n_lg} "
                 f"{pl(n_lg, 'lige', 'lige', 'liga')}, {n_c} {pl(n_c, 'kandidat', 'kandidata', 'kandidata')}, "
                 + (f"{tips} PV/K {pl(tips, 'prijedlog', 'prijedloga', 'prijedloga')}." if tips
                    else "bez PV/K prijedloga.")]
        notes += [n for n in cand.get("notes", []) if not n.startswith("Liga nije pronađena")]
        plan["scanner"] = {"lastRunAt": cand.get("generatedAt"), "fixturesSeen": cand.get("analysed", 0),
                           "candidates": cand.get("candidates", 0), "tips": tips, "leagues": cand.get("leagues", 0),
                           "window": win, "source": "screener", "notes": " ".join(notes)[:600]}
        if cand.get("rules"):
            plan["scanner"]["rules"] = cand["rules"]

    seen = set()
    stamp = now.isoformat(timespec="seconds")
    for back in range(0, days + 1):
        d = (dt.date.fromisoformat(today) - dt.timedelta(days=back)).isoformat()
        res = load(os.path.join(ROOT, "results", f"{d}.json"))
        cnd = load(os.path.join(ROOT, "candidates", f"{d}.json")) or {}
        tips = {m["fixtureId"]: (m.get("htftTip") or {}).get("sel") for m in cnd.get("matches", [])}
        for r in (res or {}).get("matches", []):
            fid, doc_id = r["fixtureId"], r.get("docId")
            if not doc_id or fid in seen:
                continue
            seen.add(fid)
            if r["status"] == "VOID":
                fields = {"status": "void", "settledAt": stamp}
            elif r["status"] in ("FT", "NOHT") and r.get("ft"):
                sel = tips.get(fid)
                fields = {"result": r["ft"], "htResult": r.get("ht"), "htft": r.get("htft"), "outcome": outcome(r["ft"]),
                          "tipWon": None, "htftWon": (r["htft"] == sel) if sel and r.get("htft") else None,
                          "status": "settled", "settledAt": stamp}
            else:
                continue
            plan["settle"].append({"path": f"analyses/{doc_id}", "fixtureId": fid, "fields": fields})

    with open("/tmp/dosije_plan.json", "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False)
    print(json.dumps({
        "today": today, "create": len(plan["create"]), "settle": len(plan["settle"]), "dates": plan["dates"],
        "tips": [f"{c['data']['home']} – {c['data']['away']} {c['data']['date']} {c['data']['kickoff']} "
                 f"PV/K {c['data']['htftTip']['sel']}"
                 f"{' ili ' + c['data']['htftTip']['alt'] if c['data']['htftTip'].get('alt') else ''} "
                 f"({c['data']['htftTip']['score']})"
                 for c in plan["create"] if c["data"].get("htftTip")],
        "candidatesWithoutTip": sum(1 for c in plan["create"] if not c["data"].get("htftTip")),
        "scanner": bool(plan["scanner"]), "plan": "/tmp/dosije_plan.json"}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
