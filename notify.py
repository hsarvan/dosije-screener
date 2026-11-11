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
import re
import sys
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Europe/Sarajevo")
APP = "https://claude.ai/artifact/RT8qUK2yQhwFQPvVjxCL5M"
POT, MIN_TIP = 15, 20   # potencijalni: skor 15–19, PV/K prijedlog od 20
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
    return html.escape(str(s or ""), quote=False)


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
    pot = [m for m in rest if (m["doc"]["cand"].get("score") or 0) >= POT]
    low = [m for m in rest if (m["doc"]["cand"].get("score") or 0) < POT]
    if pot:
        out.append(f"\n🟡 <b>Potencijalni</b> (skor {POT}–{MIN_TIP - 1})")
        for m in pot:
            d, c = m["doc"], m["doc"]["cand"]
            why = (d.get("signals") or [{}])[0].get("note") or ""
            out.append(f"• <b>{e(d['kickoff'])} {e(d['home'])} – {e(d['away'])}</b>\n  {e(d['league'])}\n"
                       f"  PV/K {e(pick(c))} · skor {c.get('score', '')}" + (f"\n  <i>{e(why)}</i>" if why else ""))
    if low:
        out.append(f"\n👀 <b>Kandidati</b> ({len(low)})")
        for m in low[:10]:
            d, c = m["doc"], m["doc"]["cand"]
            out.append(f"• {e(d['kickoff'])} {e(d['home'])} – {e(d['away'])} · {e(pick(c))} · {c.get('score', '')}")
        if len(low) > 10:
            out.append(f"  … i još {len(low) - 10} u Dosijeu")
    out.append(f'\n<a href="{APP}">Otvori Dosije</a>')
    text = "\n".join(out)
    return text if len(text) <= 4000 else text[:3990] + "…"


def build_upsets(today):
    cand = load(os.path.join(ROOT, "candidates", f"{today.isoformat()}.json"), {}) or {}
    ups = cand.get("upsets") or []
    if not ups:
        return None
    out = [f"💰 <b>Velike kvote · {DAYS[today.weekday()]} {today:%d.%m.}</b>",
           "Slabija ekipa s obrascem koji joj ide u prilog. Kvota je procjena iz forme, provjeri pravu kod kladionice."]
    for u in ups:
        out.append(f"\n• <b>{e(u['kickoff'])} {e(u['home'])} – {e(u['away'])}</b>\n  {e(u['league'])}\n"
                   f"  Tip <b>{e(u['pick'])}</b> ({e(u['dog'])}) · procijenjena kvota ~{u['estOdds']:.1f}".replace(".", ",", 1)
                   + "".join(f"\n  <i>{e(w)}</i>" for w in u.get("why", [])))
    st = cand.get("upsetStats") or {}
    if st.get("n"):
        out.append(f"\nDosad: {st['won']} od {st['n']} pogođeno (prosj. kvota ~{st['avgOdds']}).")
    else:
        out.append("\nNa odigranim utakmicama ovakvi tipovi su prolazili u oko 28% slučajeva (inače 22%). Rizično.")
    text = "\n".join(out)
    return text if len(text) <= 4000 else text[:3990] + "…"


def build_k2(today):
    cand = load(os.path.join(ROOT, "candidates", f"{today.isoformat()}.json"), {}) or {}
    ks = cand.get("k2") or []
    if not ks:
        return None
    out = [f"🏠 <b>3 za 3 · {DAYS[today.weekday()]} {today:%d.%m.}</b>",
           "Domaćin je dobio zadnja dva međusobna na svom terenu. Tip 1."]
    for k in ks:
        odds = f" · procijenjena kvota ~{k['estOdds']:.2f}".replace(".", ",") if k.get("estOdds") else ""
        out.append(f"\n• <b>{e(k['kickoff'])} {e(k['home'])} – {e(k['away'])}</b>{' ⭐' if k.get('both') else ''}\n"
                   f"  {e(k['league'])}\n  Tip <b>1</b>{odds}" + "".join(f"\n  <i>{e(w)}</i>" for w in k.get("why", [])))
    st = cand.get("k2Stats") or {}
    if st.get("n"):
        out.append(f"\nDosad: {st['won']} od {st['n']} pogođeno.")
    else:
        out.append("\nNa odigranim utakmicama domaćin je ovdje pobijedio u 65% slučajeva (po formi očekivano 47%), "
                   "ali na malom uzorku. ⭐ = i gost je dobio zadnji meč kod sebe.")
    text = "\n".join(out)
    return text if len(text) <= 4000 else text[:3990] + "…"


def main():
    raw = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    m = re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", raw)
    token = m.group(0) if m else ""
    if not token:
        print("TELEGRAM_BOT_TOKEN nije postavljen ili ne liči na token (broj:slova).")
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
    up = build_upsets(today)
    if up:
        r2 = tg(token, "sendMessage", chat_id=cid, text=up, parse_mode="HTML", disable_web_page_preview="true")
        print("Velike kvote poslane:", r2.get("ok"))
    k2 = build_k2(today)
    if k2:
        r3 = tg(token, "sendMessage", chat_id=cid, text=k2, parse_mode="HTML", disable_web_page_preview="true")
        print("3 za 3 poslano:", r3.get("ok"))
    if r.get("ok"):
        with open(mark, "w", encoding="utf-8") as f:
            json.dump({"date": today.isoformat()}, f)
    return 0


if __name__ == "__main__":
    if "--preview" in sys.argv:
        print(build(dt.date.fromisoformat(sys.argv[-1]) if sys.argv[-1][:2] == "20" else dt.datetime.now(TZ).date()))
    else:
        sys.exit(main())
