#!/usr/bin/env python3
"""Query the session-console wiki index from the command line (what the assistant session uses).

  session-console wiki list [--days N] [--group NAME] [--all]      sessions, newest first, with their one-line summary
  session-console wiki search "words or \"a phrase\"" [--limit N]  full-text search over every transcript (diacritics optional)
  session-console wiki card <sid-prefix>                           the wiki card of one session: summary, bullets, keywords, files
  session-console wiki show <sid-prefix> [--ts ISO | --seq N] [--n 12] [--role user,assistant]   messages around one point
  session-console wiki now [--running]                             what every live session is doing right now (last turns)

Every hit is printed with a link the console understands: session://<sid> (whole session) or session://<sid>?ts=<ts>
(one message). Copy those links verbatim into your answer so the user can click them.
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import sys

from . import server as _srv

HOME = os.path.expanduser("~")
DB = _srv.INDEX_DB
PROJECTS = _srv.PROJECTS


def db():
    if not os.path.exists(DB):
        sys.exit(f"no index at {DB}: start `session-console run` first (it builds and refreshes the index)")
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    return c


def meta_of(path):
    """title / first prompt / cwd / branch from the transcript head+tail (same idea as the server)."""
    m = {"title": None, "first_prompt": None, "cwd": None, "branch": None}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(256 * 1024).decode("utf-8", "replace")
            for line in head.splitlines():
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") == "user" and not d.get("isMeta") and not d.get("isSidechain"):
                    c = d.get("message", {}).get("content")
                    if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                        m["first_prompt"] = c[:160].replace("\n", " ")
                        m["cwd"] = d.get("cwd")
                        break
            f.seek(max(0, size - 256 * 1024))
            for line in f.read().decode("utf-8", "replace").splitlines():
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") == "ai-title":
                    m["title"] = d.get("aiTitle")
                elif d.get("type") in ("user", "assistant"):
                    m["cwd"] = d.get("cwd") or m["cwd"]
                    m["branch"] = d.get("gitBranch") or m["branch"]
    except Exception:
        pass
    return m


def resolve(c, prefix):
    rows = c.execute("SELECT sid, path FROM files WHERE sid LIKE ?", (prefix + "%",)).fetchall()
    if not rows:
        sys.exit(f"no session starting with {prefix!r}")
    if len(rows) > 1:
        sys.exit("ambiguous prefix: " + ", ".join(r[0] for r in rows))
    return rows[0]


def group_name(cwd):
    if not cwd:
        return "?"
    cwd = cwd.rstrip("/")
    m = re.match(r"^/tmp/claude-\d+/(-[^/]+)/", cwd)
    if m:
        return "scratchpad · " + _srv.scratch_label(m.group(1))
    d = cwd
    while d and d != "/" and not os.path.exists(os.path.join(d, ".git")):
        d = os.path.dirname(d)
    return os.path.basename(d) if d and d != "/" else cwd.replace(HOME, "~")


def short_ts(ts):
    return (ts or "")[:16].replace("T", " ")


def cmd_list(a):
    c = db()
    cutoff = (dt.datetime.utcnow() - dt.timedelta(days=a.days)).isoformat() if a.days else None
    rows = c.execute("""SELECT f.sid, f.path, f.n, f.mtime, k.card,
                        (SELECT min(ts) FROM msgs m WHERE m.sid=f.sid), (SELECT max(ts) FROM msgs m WHERE m.sid=f.sid)
                        FROM files f LEFT JOIN cards k ON k.sid=f.sid WHERE f.n>=1 ORDER BY f.mtime DESC""").fetchall()
    n = 0
    for sid, path, cnt, mtime, card, first, last in rows:
        if cutoff and (last or "") < cutoff:
            continue
        if not os.path.exists(path):
            continue
        m = meta_of(path)
        g = group_name(m["cwd"])
        if a.group and a.group.lower() not in g.lower():
            continue
        card = json.loads(card) if card else {}
        title = m["title"] or m["first_prompt"] or "(no title)"
        print(f"session://{sid}")
        print(f"  {title}")
        print(f"  {g} · {short_ts(first)} → {short_ts(last)} · {cnt} đoạn · {card.get('status', '?')}")
        if card.get("summary"):
            print(f"  ↳ {card['summary']}")
        if card.get("keywords"):
            print("  # " + ", ".join(card["keywords"]))
        n += 1
        if not a.all and n >= 60:
            print("… (dùng --all để xem hết)")
            break
    print(f"\n{n} session")


def fts_expr(q):
    terms = []
    for m in re.finditer(r'"([^"]+)"|(\S+)', q):
        if m.group(1):
            terms.append('"' + m.group(1).replace('"', "") + '"')
        else:
            w = re.sub(r"[^\w\-\./:@]", " ", m.group(2)).strip()
            if w:
                terms.append('"' + w + '"' + ("*" if len(w) >= 3 else ""))
    return " ".join(terms)


def cmd_search(a):
    c = db()
    expr = fts_expr(" ".join(a.query))
    rows = c.execute("""SELECT m.sid, m.seq, m.ts, m.role, m.name, snippet(fts, 0, '[', ']', '…', 20), bm25(fts)
                        FROM fts JOIN msgs m ON m.id=fts.rowid WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT 400""", (expr,)).fetchall()
    by = {}
    for sid, seq, ts, role, name, snip, score in rows:
        s = by.setdefault(sid, {"hits": 0, "conv": 0, "best": score, "snips": []})
        s["hits"] += 1
        s["conv"] += role in ("user", "assistant")
        if (role in ("user", "assistant") or len(s["snips"]) < 2) and len(s["snips"]) < 3:
            s["snips"].append((seq, ts, role, name, snip))
    order = sorted(by.items(), key=lambda kv: (-min(kv[1]["conv"], 3), kv[1]["best"]))[: a.limit]
    print(f"{len(rows)} đoạn khớp trong {len(by)} session · FTS: {expr}\n")
    for sid, s in order:
        path = c.execute("SELECT path FROM files WHERE sid=?", (sid,)).fetchone()[0]
        m = meta_of(path)
        card = c.execute("SELECT card FROM cards WHERE sid=?", (sid,)).fetchone()
        card = json.loads(card[0]) if card and card[0] else {}
        print(f"session://{sid}   {m['title'] or m['first_prompt'] or ''}")
        print(f"  {group_name(m['cwd'])} · {s['hits']} khớp" + (f" · {card['summary']}" if card.get("summary") else ""))
        for seq, ts, role, name, snip in s["snips"]:
            who = role if role != "tool_use" else f"tool {name}"
            print(f"  - [{who} · {short_ts(ts)}] {snip.replace(chr(10), ' ')[:260]}")
            print(f"    session://{sid}?ts={ts}")
        print()


def cmd_card(a):
    c = db()
    sid, path = resolve(c, a.sid)
    m = meta_of(path)
    row = c.execute("SELECT card, ts, n_at, err FROM cards WHERE sid=?", (sid,)).fetchone()
    card = json.loads(row[0]) if row and row[0] else None
    rows = c.execute("SELECT ts, role, name, text FROM msgs WHERE sid=? ORDER BY seq", (sid,)).fetchall()
    users = [r for r in rows if r[1] == "user"]
    files = {}
    for ts, role, name, text in rows:
        if role == "tool_use" and name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
            p = text.split("\n")[0].strip()
            if p.startswith("/"):
                files[p] = files.get(p, 0) + 1
    print(f"session://{sid}")
    print(f"tiêu đề: {m['title']}\nthư mục: {m['cwd']} · nhánh: {m['branch']}")
    print(f"thời gian: {short_ts(rows[0][0]) if rows else '?'} → {short_ts(rows[-1][0]) if rows else '?'} · {len(users)} lượt người dùng · {len(rows)} đoạn")
    if card:
        print(f"\ntóm tắt ({card.get('status')}): {card.get('summary')}")
        for b in card.get("bullets", []):
            print(f"  • {b}")
        print("từ khoá: " + ", ".join(card.get("keywords", [])))
    else:
        print("\n(chưa có tóm tắt" + (f"; lỗi: {row[3]}" if row and row[3] else "") + ")")
    if files:
        print("\nfile đụng nhiều nhất:")
        for p, n in sorted(files.items(), key=lambda kv: -kv[1])[:12]:
            print(f"  {p}  ×{n}")
    print("\nyêu cầu đầu của người dùng:")
    for ts, role, name, text in users[:6]:
        print(f"  [{short_ts(ts)}] {text[:200].replace(chr(10), ' ')}    session://{sid}?ts={ts}")
    if len(users) > 6:
        print("  …")
        for ts, role, name, text in users[-3:]:
            print(f"  [{short_ts(ts)}] {text[:200].replace(chr(10), ' ')}    session://{sid}?ts={ts}")


def cmd_show(a):
    c = db()
    sid, path = resolve(c, a.sid)
    roles = a.role.split(",") if a.role else None
    if a.ts:
        row = c.execute("SELECT seq FROM msgs WHERE sid=? AND ts>=? ORDER BY seq LIMIT 1", (sid, a.ts)).fetchone()
        center = row[0] if row else 0
    elif a.seq is not None:
        center = a.seq
    else:
        center = c.execute("SELECT max(seq) FROM msgs WHERE sid=?", (sid,)).fetchone()[0] or 0
    lo, hi = max(0, center - a.n // 2), center + a.n // 2
    q = "SELECT seq, ts, role, name, text FROM msgs WHERE sid=? AND seq BETWEEN ? AND ?"
    params = [sid, lo, hi]
    if roles:
        q += " AND role IN (%s)" % ",".join("?" * len(roles))
        params += roles
    for seq, ts, role, name, text in c.execute(q + " ORDER BY seq", params):
        who = role if role != "tool_use" else f"tool {name}"
        body = text if role in ("user", "assistant") else text[:500]
        print(f"── #{seq} [{who} · {short_ts(ts)}]  session://{sid}?ts={ts}")
        print(body[: a.chars])
        print()


def cmd_now(a):
    c = db()
    live = set()
    for p in glob.glob(os.path.join(HOME, ".claude", "sessions", "*.json")):
        try:
            d = json.load(open(p))
            pid = d.get("pid")
            if pid and _srv.pid_alive(pid) and d.get("sessionId"):
                live.add(d["sessionId"])
        except Exception:
            pass
    rows = c.execute("SELECT sid, path, mtime FROM files ORDER BY mtime DESC LIMIT 40").fetchall()
    for sid, path, mtime in rows:
        if a.running and sid not in live:
            continue
        m = meta_of(path)
        last = c.execute("SELECT ts, role, text FROM msgs WHERE sid=? AND role IN ('user','assistant') ORDER BY seq DESC LIMIT 2", (sid,)).fetchall()
        print(f"session://{sid}  {'● running' if sid in live else '○'}  {m['title'] or m['first_prompt'] or ''}  [{group_name(m['cwd'])}]")
        for ts, role, text in reversed(last):
            print(f"  [{role} · {short_ts(ts)}] {text[:220].replace(chr(10), ' ')}")
        print()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="session-console wiki", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("list"); p.add_argument("--days", type=int, default=0); p.add_argument("--group"); p.add_argument("--all", action="store_true"); p.set_defaults(f=cmd_list)
    p = sp.add_parser("search"); p.add_argument("query", nargs="+"); p.add_argument("--limit", type=int, default=12); p.set_defaults(f=cmd_search)
    p = sp.add_parser("card"); p.add_argument("sid"); p.set_defaults(f=cmd_card)
    p = sp.add_parser("show"); p.add_argument("sid"); p.add_argument("--ts"); p.add_argument("--seq", type=int); p.add_argument("--n", type=int, default=12)
    p.add_argument("--role"); p.add_argument("--chars", type=int, default=1500); p.set_defaults(f=cmd_show)
    p = sp.add_parser("now"); p.add_argument("--running", action="store_true"); p.set_defaults(f=cmd_now)
    a = ap.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
