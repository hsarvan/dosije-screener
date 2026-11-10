#!/usr/bin/env python3
"""Jutarnja Telegram poruka: PV/K prijedlozi i kandidati za danas, jučerašnji ishodi.

Treba samo GitHub secret TELEGRAM_BOT_TOKEN. Chat ID se nađe sam (iz zadnje poruke koju
si poslao botu) i čuva u kešu; može se zadati i kao secret TELEGRAM_CHAT_ID.
Zakazani pregled šalje jednom dnevno; ručno pokretanje (Run workflow) šalje uvijek.
"""
import datetime as dt
import html
import json
import os
import sys
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Europe/Sarajevo")
APP = "https://claude.ai/artifact/RT8qUK2yQhwFQPvVjxCL5M"
DAYS = ["pon", "uto", "sri", "čet", "pet", "sub", "ned"]


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def tg(token, method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def chat_id(token):
    cid = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if cid:
        return cid
    path = os.path.join(ROOT, ".cache", "telegram_chat.json")
    saved = load(path, {}) or {}
    try:
        upd = tg(token, "getUpdates").get("result", [])
    except Exception as e:  # noqa: BLE001
        print("getUpdates nije uspio:", e)
        upd = []
    for u in reversed(upd):
        msg = u.get("message") or u.get("edited_message") or {}
        if (msg.get("chat") or {}).get("id"):
            saved = {"id": msg["chat"]["id"]}
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(saved, f)
            break
    return saved.get("id")


def e(s):
    return html.escape(str(s or ""))


def pl(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else many


def pick(t):
    return t["sel"] + (f" ili {t['alt']}" if t.get("alt") else "")


def build(today):
    cand = load(os.path.join(ROOT, "candidates", f"{today.isoformat()}.json"), {}) or {}
    ms = sorted(cand.get("matches", []), key=lambda m: (-(m.get("score") or 0), m["doc"].get("kickoff") or ""))
    tips = [m for m in ms if m["doc"].get("htftTip")]
    rest = [m for m in ms if not m["doc"].get("htftTip") and m["doc"].get("cand")]
    out = [f"⚽ <b>Dosije · {DAYS[today.weekday()]} {today:%d.%m.}</b>",
           f"Pregledano {cand.get('analysed', 0)} {pl(cand.get('analysed', 0), 'utakmica', 'utakmice', 'utakmica')} "
           f"iz {cand.get('leagues', 0)} {pl(cand.get('leagues', 0), 'lige', 'lige', 'liga')}."]
    if tips:
        out.append("\n🎯 <b>PV/K prijedlozi</b>")
        for m in tips:
            d = m["doc"]
            why = (d.get("signals") or [{}])[0].get("note") or ""
            out.append(f"• <b>{e(d['kickoff'])} {e(d['home'])} – {e(d['away'])}</b>\n"
                       f"  {e(d['league'])}\n  PV/K <b>{e(pick(d['htftTip']))}</b> · skor {d['htftTip']['score']}"
                       + (f"\n  <i>{e(why)}</i>" if why else ""))
    else:
        out.append("\n🎯 Danas nema PV/K prijedloga.")
    if rest:
        out.append(f"\n👀 <b>Kandidati</b> ({len(rest)})")
        for m in rest[:10]:
            d, c = m["doc"], m["doc"].get("cand") or {}
            out.append(f"• {e(d['kickoff'])} {e(d['home'])} – {e(d['away'])} · {e(pick(c))} · {c.get('score', '')}")
        if len(rest) > 10:
            out.append(f"  … i još {len(rest) - 10} u Dosijeu")
    # jučerašnji ishodi prijedloga i kandidata
    y = today - dt.timedelta(days=1)
    seen, lines = set(), []
    for day in (y, today):
        res = load(os.path.join(ROOT, "results", f"{day.isoformat()}.json"), {}) or {}
        cands = {}
        for d in (y - dt.timedelta(days=1), y, today):
            for m in (load(os.path.join(ROOT, "candidates", f"{d.isoformat()}.json"), {}) or {}).get("matches", []):
                cands[m["fixtureId"]] = m
        for r in res.get("matches", []):
            m = cands.get(r["fixtureId"])
            if not m or r["fixtureId"] in seen or r.get("status") != "FT":
                continue
            seen.add(r["fixtureId"])
            d = m["doc"]
            t = d.get("htftTip") or d.get("cand") or {}
            if not t:
                continue
            hit = r.get("htft") in (t.get("sel"), t.get("alt"))
            mark = "✅" if hit else ("❌" if d.get("htftTip") else "▫️")
            lines.append(f"{mark} {e(d['home'])} – {e(d['away'])} {e(r['ft']).replace('-', ':')} "
                         f"(PV {e(r['ht']).replace('-', ':')}) · {e(r.get('htft'))}"
                         + (" · prijedlog" if d.get("htftTip") else ""))
    if lines:
        out.append("\n📊 <b>Jučer</b>")
        out += lines[:12]
    out.append(f'\n<a href="{APP}">Otvori Dosije</a>')
    text = "\n".join(out)
    return text if len(text) <= 4000 else text[:3990] + "…"


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Nema TELEGRAM_BOT_TOKEN, poruka se ne šalje.")
        return 0
    today = dt.datetime.now(TZ).date()
    mark = os.path.join(ROOT, "data", "telegram_sent.json")
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" or "--force" in sys.argv
    if not manual and (load(mark, {}) or {}).get("date") == today.isoformat():
        print("Poruka za danas je već poslana.")
        return 0
    cid = chat_id(token)
    if not cid:
        print("Chat nije pronađen: pošalji botu bilo koju poruku (npr. /start) pa pokreni ponovo.")
        return 0
    r = tg(token, "sendMessage", chat_id=cid, text=build(today), parse_mode="HTML", disable_web_page_preview="true")
    print("Poslano:", r.get("ok"))
    if r.get("ok"):
        with open(mark, "w", encoding="utf-8") as f:
            json.dump({"date": today.isoformat()}, f)
    return 0


if __name__ == "__main__":
    if "--preview" in sys.argv:
        print(build(dt.date.fromisoformat(sys.argv[-1]) if sys.argv[-1][:2] == "20" else dt.datetime.now(TZ).date()))
    else:
        sys.exit(main())
