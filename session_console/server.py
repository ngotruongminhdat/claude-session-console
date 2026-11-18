#!/usr/bin/env python3
"""session_console - local web UI to list, watch and drive Claude Code sessions on this machine.

stdlib only (Python 3.10+). No pip install needed.

    python3 session_console/server.py                      # http://127.0.0.1:8765
    python3 session_console/server.py --host 0.0.0.0 --token SECRET   # for Tailscale / LAN

Building blocks (all local, nothing talks to claude.ai):
  * `claude agents --json --all`      -> running sessions (interactive + background)
  * ~/.claude/sessions/<pid>.json     -> pid / socket / status for interactive sessions
  * ~/.claude/projects/*/<sid>.jsonl  -> append-only transcript, tailed for the live view
  * tmux send-keys                    -> type into an interactive session that runs in a tmux pane
  * claude -p --resume <sid>          -> drive a session that is NOT open in a terminal
  * claude -p --session-id <uuid>     -> start a brand-new session
"""
import argparse
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR", os.path.join(HOME, ".claude"))
PROJECTS = os.path.join(CLAUDE_DIR, "projects")
SESS_REG = os.path.join(CLAUDE_DIR, "sessions")
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(PKG_DIR, "static")
CONFIG_DIR = os.environ.get("SESSION_CONSOLE_CONFIG") or os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "session_console")
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache")), "session_console")
# the assistant ("Super Trợ Lý") session runs in its own folder: CLAUDE.md there teaches it `session-console wiki …`
ASSISTANT_DIR = os.environ.get("SESSION_CONSOLE_ASSISTANT_DIR") or os.path.join(CONFIG_DIR, "assistant")
ASSISTANT_TEMPLATE = os.path.join(PKG_DIR, "assistant_template")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
TAIL_BYTES = 3 * 1024 * 1024  # initial view = last 3MB of the transcript
PERMISSION_MODES = ["default", "acceptEdits", "auto", "plan", "dontAsk", "bypassPermissions"]
MODELS = [  # alias -> label shown in the picker; aliases resolve to the latest model of that tier
    ("fable", "Fable 5.2 (claude-fable-5-2)"), ("opus", "Opus 5.5 (claude-opus-5-5)"),
    ("sonnet", "Sonnet 5.5 (claude-sonnet-5-5)"), ("haiku", "Haiku 5.5 (claude-haiku-5-5)"),
    ("claude-fable-5-1", "Fable 5.1 (pinned)"),
]
MODEL_RE = re.compile(r"^[A-Za-z0-9._\[\]-]{1,64}$")

TOKEN = None
PREFS_PATH = os.path.join(CONFIG_DIR, "prefs.json")
PREFS_LOCK = threading.Lock()
GIT_ROOT_CACHE = {}
LAST_GROUPS = {}   # group root -> name, refreshed by list_sessions(); feeds the new-session dir picker
def _default_repo_roots():
    cands = [os.path.expanduser(x) for x in ("~/Desktop/repos", "~/repos", "~/code", "~/projects", "~/src", "~/dev", "~/work")]
    found = [c for c in cands if os.path.isdir(c)]
    return found or [HOME]


REPO_ROOTS = ([os.path.expanduser(x) for x in os.environ["SESSION_CONSOLE_REPOS"].split(":") if x]
              if os.environ.get("SESSION_CONSOLE_REPOS") else _default_repo_roots())
JOBS = {}  # sid -> job dict for `claude -p` processes we spawned
JOBS_LOCK = threading.Lock()


# ----------------------------------------------------------------------------- helpers
def child_env():
    """Env for spawned `claude` processes: drop the nested-session markers so a server started
    from inside a Claude session can still launch new ones."""
    env = dict(os.environ)
    for k in list(env):
        if k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_") or k in ("CLAUDE_PID", "CLAUDE_EFFORT"):
            env.pop(k, None)
    return env


def run(cmd, timeout=20, cwd=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=child_env())


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ----------------------------------------------------------------------------- prefs (shared across devices)
def load_prefs():
    d = read_json(PREFS_PATH, {}) or {}
    return {"hidden": list(d.get("hidden", [])), "hidden_groups": list(d.get("hidden_groups", [])),
            "pinned_groups": list(d.get("pinned_groups", [])), "pinned": list(d.get("pinned", [])),
            "recent_cwds": list(d.get("recent_cwds", [])),
            "names": {k: str(v)[:80] for k, v in (d.get("names") or {}).items() if isinstance(v, str) and v.strip()},
            "labels": {"agent": str(d.get("labels", {}).get("agent") or "assistant")[:24],
                       "user": str(d.get("labels", {}).get("user") or "you")[:24]}}


def save_prefs(p):
    os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
    tmp = PREFS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PREFS_PATH)


def update_prefs(body):
    """body: {"hide"/"unhide": [sid], "hide_group"/"unhide_group": [key],
              "pin_group"/"unpin_group": [key], "set_pinned": [key]  (pinned = only these folders are shown)}"""
    with PREFS_LOCK:
        p = load_prefs()
        hidden, groups, pinned, pins = set(p["hidden"]), set(p["hidden_groups"]), set(p["pinned_groups"]), set(p["pinned"])
        pins |= set(body.get("pin") or [])
        pins -= set(body.get("unpin") or [])
        hidden |= set(body.get("hide") or [])
        hidden -= set(body.get("unhide") or [])
        groups |= set(body.get("hide_group") or [])
        groups -= set(body.get("unhide_group") or [])
        if "set_pinned" in body:
            pinned = set(body.get("set_pinned") or [])
        pinned |= set(body.get("pin_group") or [])
        pinned -= set(body.get("unpin_group") or [])
        labels = dict(p["labels"])
        for k in ("agent", "user"):
            v = (body.get("labels") or {}).get(k)
            if isinstance(v, str) and v.strip():
                labels[k] = v.strip()[:24]
        names = dict(p["names"])
        for sid, v in (body.get("names") or {}).items():   # {"sid": "new name"} ; "" removes
            if isinstance(v, str) and v.strip():
                names[sid] = v.strip()[:80]
            else:
                names.pop(sid, None)
        p = {"hidden": sorted(hidden), "hidden_groups": sorted(groups), "pinned_groups": sorted(pinned), "pinned": sorted(pins),
             "recent_cwds": p["recent_cwds"], "labels": labels, "names": names}
        save_prefs(p)
        return p


def remember_name(sid, name):
    """Persist a user-set session name (from /rename) so it survives the session process."""
    with PREFS_LOCK:
        p = load_prefs()
        if p["names"].get(sid) == name:
            return
        p["names"][sid] = name[:80]
        save_prefs(p)


def remember_cwd(cwd):
    with PREFS_LOCK:
        p = load_prefs()
        p["recent_cwds"] = [cwd] + [c for c in p["recent_cwds"] if c != cwd]
        p["recent_cwds"] = p["recent_cwds"][:8]
        save_prefs(p)


def list_dirs():
    """Choices for the new-session directory picker."""
    with PREFS_LOCK:
        recent = load_prefs()["recent_cwds"]
    repos = []
    for root in REPO_ROOTS:
        try:
            for name in sorted(os.listdir(root)):
                path = os.path.join(root, name)
                if name.startswith(".") or not os.path.isdir(path):
                    continue
                repos.append({"path": path, "name": name, "git": os.path.isdir(os.path.join(path, ".git")),
                              "mtime": os.path.getmtime(path)})
        except OSError:
            continue
    repos.sort(key=lambda r: (not r["git"], -r["mtime"]))
    seen = set(recent) | {r["path"] for r in repos}
    active = [{"path": g, "name": n} for g, n in LAST_GROUPS.items()
              if g not in seen and os.path.isdir(g) and not g.startswith("/tmp/")]
    return {"recent": [c for c in recent if os.path.isdir(c)], "active": active, "repos": repos,
            "home": HOME, "repo_roots": REPO_ROOTS}


def git_root(cwd):
    """Nearest ancestor with a .git entry, else the cwd itself. Cached (cwd rarely changes meaning)."""
    if not cwd:
        return None
    if cwd in GIT_ROOT_CACHE:
        return GIT_ROOT_CACHE[cwd]
    d = cwd
    root = cwd
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            root = d
            break
        nd = os.path.dirname(d)
        if nd == d or len(d) <= len(HOME):
            break
        d = nd
    GIT_ROOT_CACHE[cwd] = root
    return root


SCRATCH_RE = re.compile(r"^(/tmp/claude-\d+/([^/]+)/[0-9a-f-]{36}/scratchpad)(/|$)")


def scratch_label(slug):
    """Claude names scratchpad folders after the project cwd with '/' -> '-' (e.g. -home-me-repos-foo). Strip the
    repo-root / home prefix so the label is just 'foo'."""
    for root in sorted(REPO_ROOTS + [HOME], key=len, reverse=True):
        pre = root.replace("/", "-")
        if slug.startswith(pre + "-"):
            return slug[len(pre) + 1:]
    return slug.strip("-")
DATE_DIR_RE = re.compile(r"^\d{8}")


def short_path(path, parts=2):
    p = path.replace(HOME, "~", 1) if path.startswith(HOME) else path
    segs = [x for x in p.split("/") if x]
    return "/".join(segs[-parts:]) if len(segs) > parts else p


def group_of(item):
    """Attach group / group_name / subdir.
    git repos group by root; scratchpad sessions group per parent project; run folders shaped like
    <config>/<YYYYMMDD...>/repo collapse onto <config> so one strategy config is one group."""
    cwd = item.get("cwd") or "?"
    m = SCRATCH_RE.match(cwd)
    if m:
        root = m.group(1)
        name = "scratchpad · " + scratch_label(m.group(2))
    else:
        root = git_root(cwd) or cwd
        while True:  # climb out of generic 'repo' dirs and date-stamped run dirs
            base = os.path.basename(root.rstrip("/"))
            parent = os.path.dirname(root.rstrip("/"))
            if parent in ("", "/", HOME) or not (base == "repo" or DATE_DIR_RE.match(base)):
                break
            root = parent
        name = os.path.basename(root.rstrip("/")) if os.path.exists(os.path.join(root, ".git")) else short_path(root, 2)
        if root == HOME:
            name = "~"
    item["group"] = root
    item["group_name"] = name
    item["subdir"] = os.path.relpath(cwd, root) if cwd != root and cwd.startswith(root + "/") else ""
    return item


def list_subdirs(typed):
    """Path completion for the dir picker: subdirectories of dirname(typed) whose name starts with
    basename(typed). Hidden dirs only when the typed prefix starts with a dot."""
    typed = os.path.expanduser(typed or "")
    if not typed.startswith("/"):
        return {"dirs": []}
    if typed.endswith("/"):
        parent, prefix = typed, ""
    else:
        parent, prefix = os.path.dirname(typed) or "/", os.path.basename(typed)
    out = []
    try:
        for name in sorted(os.listdir(parent), key=str.lower):
            if name.startswith(".") and not prefix.startswith("."):
                continue
            if not name.lower().startswith(prefix.lower()):
                continue
            full = os.path.join(parent, name)
            if os.path.isdir(full):
                out.append({"path": full, "git": os.path.isdir(os.path.join(full, ".git"))})
            if len(out) >= 60:
                break
    except OSError:
        pass
    return {"dirs": out}


# ----------------------------------------------------------------------------- slash commands
BUILTIN_COMMANDS = [
    ("help", "Show available commands"), ("clear", "Clear conversation history"),
    ("compact", "Compact context, optional focus text"), ("context", "Show context usage"),
    ("cost", "Show token / cost usage"), ("status", "Session status"), ("config", "Open settings"),
    ("model", "Switch model"), ("effort", "Set effort level"), ("fast", "Toggle fast mode"),
    ("plan", "Enter plan mode"), ("permissions", "Manage tool permissions"), ("memory", "Edit memory files"),
    ("rename", "Rename this session"), ("resume", "Resume another session"), ("export", "Export conversation"),
    ("add-dir", "Add a working directory"), ("init", "Create CLAUDE.md"), ("review", "Review a PR"),
    ("mcp", "MCP servers"), ("hooks", "Hooks"), ("agents", "Agents"), ("artifacts", "List artifacts"),
    ("workflows", "Watch workflows"), ("doctor", "Diagnose installation"), ("bug", "Report a bug"),
    ("login", "Sign in"), ("logout", "Sign out"), ("vim", "Toggle vim mode"),
    ("remote-control", "Remote Control via claude.ai"), ("chrome", "Claude in Chrome"),
]
CMD_CACHE = {}  # root -> (ts, list)


def parse_frontmatter(path):
    """Tiny YAML-ish reader for SKILL.md / command.md frontmatter: name, description, argument-hint."""
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(6000)
    except OSError:
        return out
    if not head.startswith("---"):
        return out
    body = head[3:].split("\n---", 1)[0].splitlines()
    key = None
    for line in body:
        if line[:1] in (" ", "\t") and key:
            out[key] = (out[key] + " " + line.strip()).strip()
            continue
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val in (">", ">-", "|", "|-"):
                val = ""
            out[key] = val.strip("\"'")
    return out


def scan_commands(root):
    """Skills + custom commands visible from `root` (project .claude + user ~/.claude) + builtins."""
    now = time.time()
    hit = CMD_CACHE.get(root)
    if hit and now - hit[0] < 30:
        return hit[1]
    cmds, seen = [], set()

    def add(name, desc, scope, hint=""):
        if name and name not in seen:
            seen.add(name)
            cmds.append({"name": name, "desc": (desc or "")[:160], "scope": scope, "hint": hint})

    def scan_dir(base, scope):
        for p in sorted(glob.glob(os.path.join(base, "skills", "**", "SKILL.md"), recursive=True)):
            fm = parse_frontmatter(p)
            name = fm.get("name") or os.path.basename(os.path.dirname(p))
            if os.sep + "synced" + os.sep in p and ":" not in name:
                name = "anthropic-skills:" + name  # skills synced from claude.ai are namespaced in the CLI
                scope_ = "synced"
            else:
                scope_ = scope
            add(name, fm.get("description"), scope_, fm.get("argument-hint", ""))
        for p in sorted(glob.glob(os.path.join(base, "commands", "**", "*.md"), recursive=True)):
            rel = os.path.relpath(p, os.path.join(base, "commands"))[:-3]
            fm = parse_frontmatter(p)
            add(rel.replace(os.sep, ":"), fm.get("description") or "custom command", scope, fm.get("argument-hint", ""))

    if root and os.path.isdir(root):
        scan_dir(os.path.join(root, ".claude"), "project")
    scan_dir(CLAUDE_DIR, "user")
    for name, desc in BUILTIN_COMMANDS:
        add(name, desc, "builtin")
    CMD_CACHE[root] = (now, cmds)
    return cmds


# ----------------------------------------------------------------------------- @file index, tmux pane, keys
FILES_CACHE = {}  # root -> (ts, [relpaths])
UPLOAD_DIR = os.path.join(CACHE_DIR, "uploads")
UPLOAD_MAX = 15 * 1024 * 1024
UPLOAD_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
              "text/plain": ".txt", "text/markdown": ".md", "text/csv": ".csv", "application/json": ".json",
              "application/pdf": ".pdf"}
TMUX_KEYS = {"Enter", "Escape", "Tab", "BTab", "Up", "Down", "Left", "Right", "Space", "BSpace",
             "PageUp", "PageDown", "Home", "End", "C-c", "C-l", "C-u", "C-d", "C-r"}


def file_index(root):
    now = time.time()
    hit = FILES_CACHE.get(root)
    if hit and now - hit[0] < 60:
        return hit[1]
    files = []
    if os.path.isdir(os.path.join(root, ".git")) and shutil.which("git"):
        try:
            r = subprocess.run(["git", "-C", root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                               capture_output=True, timeout=10)
            files = [f for f in r.stdout.decode("utf-8", "replace").split("\0") if f][:30000]
        except Exception:
            files = []
    if not files:
        skip = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".cache", "dist", "build"}
        for dp, dns, fns in os.walk(root):
            depth = dp[len(root):].count(os.sep)
            dns[:] = [d for d in dns if d not in skip and not d.startswith(".")] if depth < 4 else []
            for fn in fns:
                if not fn.startswith("."):
                    files.append(os.path.relpath(os.path.join(dp, fn), root))
            if len(files) > 8000:
                break
    dirs = sorted({os.path.dirname(f) + "/" for f in files if os.path.dirname(f)})
    out = dirs + sorted(files)
    FILES_CACHE[root] = (now, out)
    return out


def fuzzy_rank(q, items, limit=30):
    """Terminal-like ranking: contiguous substring (basename first) > subsequence. Pure python, fine for ~30k."""
    ql = q.lower()
    scored = []
    for it in items:
        s = it.lower()
        base = s.rsplit("/", 2)[-2] + "/" if s.endswith("/") else s.rsplit("/", 1)[-1]
        if ql in base:
            sc = 300 - base.index(ql) - len(s) / 200
        elif ql in s:
            sc = 200 - s.index(ql) / 10 - len(s) / 200
        else:
            i = 0
            for ch in ql:
                i = s.find(ch, i)
                if i < 0:
                    break
                i += 1
            else:
                sc = 100 - len(s) / 100
                scored.append((sc, it))
                continue
            continue
        scored.append((sc, it))
    scored.sort(key=lambda x: -x[0])
    return [it for _, it in scored[:limit]]


def save_upload(body):
    """body: {name, type, data(base64)} -> absolute path under UPLOAD_DIR."""
    import base64
    mime = (body.get("type") or "").split(";")[0].strip().lower()
    ext = UPLOAD_EXT.get(mime) or os.path.splitext(body.get("name") or "")[1].lower()
    if not ext or (mime and not (mime in UPLOAD_EXT or mime.startswith("image/"))):
        raise ValueError(f"unsupported file type: {mime or ext or '?'}")
    if mime.startswith("image/") and ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        ext = ".png"
    raw = base64.b64decode(body.get("data") or "", validate=False)
    if not raw or len(raw) > UPLOAD_MAX:
        raise ValueError("empty file or larger than 15MB")
    day = time.strftime("%Y%m%d")
    d = os.path.join(UPLOAD_DIR, day)
    os.makedirs(d, exist_ok=True)
    stem = "".join(ch for ch in os.path.splitext(body.get("name") or "paste")[0] if ch.isalnum() or ch in "-_")[:40] or "paste"
    path = os.path.join(d, f"{time.strftime('%H%M%S')}_{stem}_{uuid.uuid4().hex[:6]}{ext}")
    with open(path, "wb") as f:
        f.write(raw)
    return {"path": path, "bytes": len(raw)}


BROWSE_ROOTS = [os.path.abspath(os.path.expanduser(x)) for x in
                os.environ.get("SESSION_CONSOLE_ROOTS", "~:/data:/tmp").split(":") if x]


def check_browse_root(root):
    root = os.path.abspath(os.path.expanduser(root or ""))
    if not os.path.isdir(root):
        raise ValueError("root is not a directory")
    if not any(root == b or root.startswith(b + os.sep) for b in BROWSE_ROOTS):
        raise ValueError(f"browsing outside allowed roots ({', '.join(BROWSE_ROOTS)}); set SESSION_CONSOLE_ROOTS to widen")
    return root


TREE_SKIP = {"node_modules", "__pycache__", ".git", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
STATUS_CACHE = {}  # root -> (ts, {relpath: code})


def git_status_map(root):
    now = time.time()
    hit = STATUS_CACHE.get(root)
    if hit and now - hit[0] < 5:
        return hit[1]
    out = {}
    if os.path.isdir(os.path.join(root, ".git")) and shutil.which("git"):
        try:
            r = subprocess.run(["git", "-C", root, "status", "--porcelain", "-z", "--untracked-files=all"],
                               capture_output=True, timeout=10)
            for ent in r.stdout.decode("utf-8", "replace").split("\0"):
                if len(ent) > 3:
                    code, path = ent[:2].strip() or "M", ent[3:]
                    out[path] = code
                    # bubble a marker up to parent dirs so collapsed folders show a dot
                    d = os.path.dirname(path)
                    while d:
                        out.setdefault(d + "/", "•")
                        d = os.path.dirname(d)
        except Exception:
            pass
    STATUS_CACHE[root] = (now, out)
    return out


GIT_INFO_CACHE = {}


def git_info(root):
    """Live branch / dirty count / ahead-behind for the repo at root (cached 5s)."""
    root = check_browse_root(root)
    now = time.time()
    hit = GIT_INFO_CACHE.get(root)
    if hit and now - hit[0] < 5:
        return hit[1]
    info = {"root": root, "git": False}
    if os.path.isdir(os.path.join(root, ".git")) and shutil.which("git"):
        def g(*args):
            r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=8)
            return r.stdout.strip() if r.returncode == 0 else ""
        branch = g("rev-parse", "--abbrev-ref", "HEAD")
        if branch == "HEAD":
            branch = "detached @ " + g("rev-parse", "--short", "HEAD")
        dirty = len([l for l in g("status", "--porcelain").splitlines() if l.strip()])
        ab = g("rev-list", "--left-right", "--count", "@{upstream}...HEAD")
        behind, ahead = (ab.split() + ["0", "0"])[:2] if ab else ("0", "0")
        info.update({"git": True, "branch": branch, "dirty": dirty, "ahead": int(ahead), "behind": int(behind),
                     "upstream": g("rev-parse", "--abbrev-ref", "@{upstream}"), "last": g("log", "-1", "--format=%h %s")[:80]})
    GIT_INFO_CACHE[root] = (now, info)
    return info


def list_tree(root, rel):
    """One directory level under root/rel: dirs first, hidden + build dirs skipped, git status attached."""
    root = check_browse_root(root)
    target = os.path.abspath(os.path.join(root, rel or ""))
    if not (target == root or target.startswith(root + os.sep)):
        raise ValueError("path escapes root")
    if not os.path.isdir(target):
        raise ValueError("not a directory")
    st = git_status_map(root)
    items = []
    try:
        names = sorted(os.listdir(target), key=lambda n: (not os.path.isdir(os.path.join(target, n)), n.lower()))
    except OSError as e:
        raise ValueError(str(e))
    for n in names:
        if n in TREE_SKIP or (n.startswith(".") and n not in (".claude", ".github", ".gitignore", ".env.example")):
            continue
        full = os.path.join(target, n)
        r = os.path.relpath(full, root)
        isdir = os.path.isdir(full)
        try:
            size = 0 if isdir else os.path.getsize(full)
            mtime = os.path.getmtime(full)
        except OSError:
            size, mtime = 0, 0
        items.append({"name": n, "rel": r, "dir": isdir, "size": size, "mtime": mtime,
                      "status": st.get(r + "/" if isdir else r)})
        if len(items) >= 500:
            break
    return {"root": root, "rel": rel or "", "items": items}


TEXT_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".txt", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".sh",
            ".html", ".css", ".csv", ".sql", ".xml", ".env", ".log", ".r", ".jl", ".c", ".h", ".cpp", ".go", ".rs", ".java"}


def read_file_preview(root, rel, max_bytes=200 * 1024):
    root = check_browse_root(root)
    full = os.path.abspath(os.path.join(root, rel))
    if not full.startswith(root + os.sep) or not os.path.isfile(full):
        raise ValueError("not a file under root")
    ext = os.path.splitext(full)[1].lower()
    size = os.path.getsize(full)
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
        return {"kind": "image", "rel": rel, "size": size, "url": f"/api/raw?root={root}&rel={rel}"}
    if ext == ".pdf":
        return {"kind": "pdf", "rel": rel, "size": size}
    with open(full, "rb") as f:
        raw = f.read(max_bytes + 1)
    if b"\0" in raw[:8000] and ext not in TEXT_EXT:
        return {"kind": "binary", "rel": rel, "size": size}
    text = raw[:max_bytes].decode("utf-8", "replace")
    return {"kind": "text", "rel": rel, "size": size, "truncated": size > max_bytes, "text": text, "lines": text.count("\n") + 1}


OPT_RE = re.compile(r"^\s*(❯)?\s*(\d{1,2})\.\s+(\S.*)$")


TAB_RE = re.compile(r"([☐☒✔])\s*(\S(?:[^☐☒✔←→]*?\S)?)(?=\s{2,}|\s*[☐☒✔←→]|\s*$)")
SEP_RE = re.compile(r"^\s*[─━]{4,}\s*$")


def parse_tabs(line):
    """AskUserQuestion draws a tab strip: '←  ☒ Profile  ☐ Timerange  ✔ Submit  →'. ☒/✔ = answered."""
    tabs = [{"label": m.group(2).strip(), "done": m.group(1) == "☒"} for m in TAB_RE.finditer(line)]
    if len(tabs) < 2:
        return None
    cur = next((i for i, t in enumerate(tabs) if not t["done"] and t["label"].lower() != "submit"), len(tabs) - 1)
    tabs[cur]["current"] = True
    return tabs


def parse_prompt(txt):
    """Detect an interactive dialog at the bottom of the pane: a question followed by numbered options
    (permission prompts, AskUserQuestion, plan approval, trust prompt). Returns None when nothing is pending."""
    # strip box-drawing frames (permission dialogs are drawn inside │ … │)
    lines = [re.sub(r"^\s*[│┃]\s?", "", l.rstrip()).rstrip(" │┃") for l in txt.split("\n")]
    idxs = [k for k, l in enumerate(lines) if OPT_RE.match(l)]
    tab_at = next((k for k in range(len(lines) - 1, max(-1, len(lines) - 16), -1) if "☐" in lines[k] or "☒" in lines[k]), None)
    if not idxs:
        # AskUserQuestion on its Submit tab: no options, just the strip + a summary + "Enter to submit"
        if tab_at is None:
            return None
        tabs = parse_tabs(lines[tab_at])
        body = [l.strip() for l in lines[tab_at + 1:] if l.strip() and not SEP_RE.match(l)]
        hint = body[-1] if body and ("enter" in body[-1].lower() or "esc" in body[-1].lower()) else ""
        q = [l for l in body if l != hint]
        return {"question": " ".join(q)[:600], "options": [], "confirm": True, "hint": hint[:120], "tabs": tabs}
    # the option block: walk up from the last numbered line; description lines (indented) and ONE separator
    # (AskUserQuestion puts '4. Chat about this' under a rule) may sit between options
    block, k, seps = [idxs[-1]], idxs[-1] - 1, 0
    while k >= 0:
        l = lines[k]
        if OPT_RE.match(l):
            block.insert(0, k)
        elif SEP_RE.match(l):
            seps += 1
            if seps > 1:
                break
        elif not l.strip() or re.match(r"^\s{3,}\S", l):
            pass
        else:
            break
        k -= 1
    if len(block) < 2 or block[-1] < len(lines) - 30:
        return None
    opts = []
    for j, k in enumerate(block):
        m = OPT_RE.match(lines[k])
        nxt = block[j + 1] if j + 1 < len(block) else len(lines)
        desc = " ".join(l.strip() for l in lines[k + 1:nxt] if re.match(r"^\s{3,}\S", l))
        opts.append({"n": int(m.group(2)), "label": m.group(3).strip()[:160], "selected": bool(m.group(1)), "desc": desc[:400]})
    if opts[0]["n"] != 1 or [o["n"] for o in opts] != list(range(1, len(opts) + 1)):
        return None
    q, k = [], block[0] - 1
    while k >= 0 and len(q) < 10:
        l = lines[k]
        if SEP_RE.match(l) or l.lstrip().startswith(("⎿", "●", "⏺", "❯ ")) or "☐" in l or "☒" in l:
            break
        if not l.strip():
            if q:
                break
            k -= 1
            continue
        q.insert(0, l.strip())
        k -= 1
    tail = "\n".join(lines[block[-1] + 1: block[-1] + 7]).lower()
    confirm = "enter to confirm" in tail or "enter to" in tail
    # a real dialog has a ❯ cursor on one option or a confirm hint; a numbered list in prose has neither
    if not any(o["selected"] for o in opts) and not confirm:
        return None
    hint = next((l.strip() for l in tail.split("\n") if l.strip() and not re.match(r"^([─━╰╯╭╮]+|❯)\s*$", l.strip())), "")
    tabs = parse_tabs(lines[tab_at]) if tab_at is not None and tab_at < block[0] else None
    return {"question": " ".join(q)[:600], "options": opts, "confirm": confirm, "hint": hint[:120], "tabs": tabs}


def pane_capture(target, lines=45):
    r = subprocess.run(["tmux", "capture-pane", "-t", target, "-p", "-J", "-S", f"-{lines}"],
                       capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        raise ValueError(r.stderr.strip() or "capture-pane failed")
    txt = r.stdout.rstrip("\n")
    prompt = parse_prompt(txt)
    # keyword fallback (un-numbered dialogs such as the folder-trust prompt): only look at the screen tail
    # AFTER the last assistant/user block, so phrases quoted inside the conversation never count
    lines = [l.rstrip() for l in txt.split("\n") if l.strip()]
    # the normal input box = a "❯" line right under a separator, within the last 5 lines; dialogs replace it
    has_input_box = any(re.match(r"^\s*❯(\s|$)", lines[i]) and i > 0 and re.match(r"^[─━]{4,}", lines[i - 1])
                        for i in range(max(0, len(lines) - 5), len(lines)))
    kw = False
    if not has_input_box:
        tail = "\n".join(lines[-12:]).lower()
        kw = any(k in tail for k in ("enter to confirm", "esc to cancel", "do you want to", "(y/n)",
                                     "allow once", "yes, and don't ask", "press enter"))
    waiting = prompt is not None or kw
    return {"text": txt, "waiting": waiting, "prompt": prompt,
            "suggestion": pane_suggestion(target) if has_input_box and not waiting else None}


SUG_RE = re.compile(r"❯[\s\xa0]*(?:\x1b\[[0-9;]*m)*\x1b\[2m(.*?)(?:\x1b\[[0-9;]*m|$)")


def pane_suggestion(target):
    """The CLI's prompt suggestion: after a turn it renders a predicted next prompt as dim (SGR 2) ghost text
    right after the ❯ cursor. It is never written to the transcript, so read it off the screen with -e."""
    try:
        r = subprocess.run(["tmux", "capture-pane", "-t", target, "-p", "-e", "-J", "-S", "-6"],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    for line in r.stdout.split("\n"):
        if "❯" not in line:
            continue
        m = SUG_RE.search(line)
        if not m:
            continue
        txt = re.sub(r"\x1b\[[0-9;]*m", "", m.group(1)).strip()
        # the empty-box placeholder ('Try "fix lint errors"') and key hints are dim too; a suggestion is prose
        if not txt or txt.startswith(("Try \"", "Press ", "Type ")) or len(txt) > 400:
            return None
        return txt
    return None


def tmux_keys(target, keys=None, text=None):
    for k in keys or []:
        if k not in TMUX_KEYS:
            raise ValueError(f"key not allowed: {k!r}")
        subprocess.run(["tmux", "send-keys", "-t", target, k], check=True, timeout=5)
        time.sleep(0.05)
    if text:
        if len(text) > 500:
            raise ValueError("text too long for raw keys")
        subprocess.run(["tmux", "send-keys", "-t", target, "-l", "--", text], check=True, timeout=5)


# ----------------------------------------------------------------------------- discovery
def registry_by_pid():
    out = {}
    for p in glob.glob(os.path.join(SESS_REG, "*.json")):
        d = read_json(p)
        if isinstance(d, dict) and d.get("pid"):
            out[d["pid"]] = d
    return out


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def tmux_panes():
    """pane_pid -> tmux target. Empty dict if tmux is not running."""
    if not shutil.which("tmux"):
        return {}
    try:
        r = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_pid} #{session_name}:#{window_index}.#{pane_index}"],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return {}
    if r.returncode != 0:
        return {}
    panes = {}
    for line in r.stdout.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[0].isdigit():
            panes[int(parts[0])] = parts[1]
    return panes


def ppid_of(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            s = f.read()
        return int(s[s.rindex(")") + 2:].split()[1])
    except Exception:
        pass
    try:  # macOS / no procfs
        r = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=3)
        return int(r.stdout.strip() or 0) or None
    except Exception:
        return None


def pane_for_pid(pid, panes):
    seen = 0
    while pid and pid > 1 and seen < 30:
        if pid in panes:
            return panes[pid]
        pid = ppid_of(pid)
        seen += 1
    return None


def transcript_path(sid):
    hits = glob.glob(os.path.join(PROJECTS, "*", f"{sid}.jsonl"))
    return hits[0] if hits else None


META_CACHE = {}


def transcript_meta(path):
    """Cheap metadata: first user prompt (head) + latest ai-title / cost / cwd (tail). Cached per (mtime, size)."""
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {"title": None, "first_prompt": None, "cost_usd": None, "cwd": None, "model": None, "branch": None}
    hit = META_CACHE.get(path)
    if hit and hit[0] == key:
        return dict(hit[1])
    meta = _transcript_meta_uncached(path)
    META_CACHE[path] = (key, dict(meta))
    if len(META_CACHE) > 2000:
        META_CACHE.pop(next(iter(META_CACHE)))
    return meta


def _transcript_meta_uncached(path):
    meta = {"title": None, "first_prompt": None, "cost_usd": None, "cwd": None, "model": None, "branch": None}
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
                    if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):  # skip /command + injected blocks
                        meta["first_prompt"] = c[:200]
                        meta["cwd"] = meta["cwd"] or d.get("cwd")
                        break
            f.seek(max(0, size - 512 * 1024))
            tail = f.read().decode("utf-8", "replace")
        for line in tail.splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            if t == "ai-title":
                meta["title"] = d.get("aiTitle")
            elif t == "cost-state":
                meta["cost_usd"] = d.get("totalCostUSD")
            elif t in ("user", "assistant"):
                meta["cwd"] = d.get("cwd") or meta["cwd"]
                meta["branch"] = d.get("gitBranch") or meta["branch"]
                m = d.get("message", {})
                if t == "assistant" and m.get("model"):
                    meta["model"] = m["model"]
    except Exception:
        pass
    return meta


AGENTS_CACHE = {"ts": 0, "running": [], "err": None}
AGENTS_LOCK = threading.Lock()


def claude_agents():
    """`claude agents --json --all`, coalesced: at most one call every 3s shared by all clients."""
    with AGENTS_LOCK:
        if time.time() - AGENTS_CACHE["ts"] < 3:
            return AGENTS_CACHE["running"], AGENTS_CACHE["err"]
        running, err = [], None
        try:
            r = run([CLAUDE_BIN, "agents", "--json", "--all"], timeout=25)
            if r.returncode == 0 and r.stdout.strip():
                running = json.loads(r.stdout)
            else:
                err = (r.stderr or r.stdout).strip()[:300]
        except Exception as e:  # claude missing / timeout -> fall back to the registry only
            err = str(e)
        AGENTS_CACHE.update({"ts": time.time(), "running": running, "err": err})
        return running, err


def list_sessions():
    """Running sessions (from `claude agents`) + recent on-disk transcripts that are not running."""
    running, err = claude_agents()

    reg = registry_by_pid()
    panes = tmux_panes()
    by_sid = {}
    for s in running:
        sid = s.get("sessionId")
        if not sid:
            continue
        item = {
            "sessionId": sid,
            "short": s.get("id") or sid[:8],
            "name": s.get("name"),
            "kind": s.get("kind"),
            "cwd": s.get("cwd"),
            "startedAt": s.get("startedAt"),
            "status": s.get("status") or (s.get("state") if isinstance(s.get("state"), str) else None),
            "pid": s.get("pid"),
        }
        by_sid[sid] = item
    # registry adds pid/socket/status for interactive sessions and catches ones `claude agents` missed
    for pid, d in reg.items():
        sid = d.get("sessionId")
        if not sid or not pid_alive(pid):
            continue
        item = by_sid.setdefault(sid, {"sessionId": sid, "short": sid[:8], "name": d.get("name"),
                                       "kind": d.get("kind", "interactive"), "cwd": d.get("cwd"),
                                       "startedAt": d.get("startedAt"), "status": d.get("status")})
        if item.get("pid") and item["pid"] != pid:
            # same session id alive in two processes (resumed while the original still runs):
            # both write the same transcript. Keep the newer one, flag the conflict.
            older = item["pid"] if (item.get("startedAt") or 0) <= (d.get("startedAt") or 0) else pid
            item.setdefault("dupes", []).append({"pid": older, "name": item.get("name") if older == item["pid"] else d.get("name")})
            if older == pid:
                continue
            item["name"] = d.get("name")
            item["startedAt"] = d.get("startedAt")
        item["pid"] = pid
        item["status"] = item.get("status") or d.get("status")
        item["socket"] = d.get("messagingSocketPath")
        item["version"] = d.get("version")
        item["tmux_reg"] = d.get("tmux")  # the CLI records its own pane as session:@win.%pane when run inside tmux
        item["name_source"] = d.get("nameSource")  # "derived" = auto cwd+id name, anything else = user-set
        if d.get("nameSource") == "user" and d.get("name"):
            remember_name(sid, d["name"])  # keep /rename names after the process exits

    for sid, item in by_sid.items():
        pid = item.get("pid")
        item["tmux"] = (pane_for_pid(pid, panes) if pid else None) or item.get("tmux_reg")
        item["alive"] = bool(pid and pid_alive(pid)) if item.get("kind") == "interactive" else item.get("status") not in ("done", "exited", "stopped")
        path = transcript_path(sid)
        item["transcript"] = path
        if path:
            item.update({k: v for k, v in transcript_meta(path).items() if v is not None and k != "cwd"})
        with JOBS_LOCK:
            job = JOBS.get(sid)
        item["job"] = job_view(job) if job else None
        item["control"] = control_mode(item)
        group_of(item)

    # recent transcripts on disk that are not running
    recent = []
    files = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for p in files[:150]:
        sid = os.path.splitext(os.path.basename(p))[0]
        if sid in by_sid:
            continue
        m = transcript_meta(p)
        with JOBS_LOCK:
            job = JOBS.get(sid)
        if not m.get("first_prompt") and not m.get("title"):
            continue  # empty / aborted session files
        recent.append(group_of({"sessionId": sid, "short": sid[:8], "transcript": p, "mtime": os.path.getmtime(p),
                                "size": os.path.getsize(p), "kind": "disk", "job": job_view(job) if job else None,
                                "control": "resume", **m}))
        if len(recent) >= 100:
            break
    LAST_GROUPS.clear()
    LAST_GROUPS.update({i["group"]: i["group_name"] for i in list(by_sid.values()) + recent if i.get("group")})
    with PREFS_LOCK:
        prefs = load_prefs()
    # pinned sessions are always listed, even when older than the recent window
    have = set(by_sid) | {r["sessionId"] for r in recent}
    for sid in prefs.get("pinned", []):
        if sid in have:
            continue
        pth = transcript_path(sid)
        if not pth:
            continue
        m = transcript_meta(pth)
        recent.append(group_of({"sessionId": sid, "short": sid[:8], "transcript": pth, "mtime": os.path.getmtime(pth),
                                "size": os.path.getsize(pth), "kind": "disk", "job": None, "control": "resume", **m}))
    # apply remembered / console-set names to everything that is not currently user-named by the CLI
    names = prefs.get("names") or {}
    for it in list(by_sid.values()) + recent:
        nm = names.get(it["sessionId"])
        if nm and it.get("name_source") != "user":
            it["name"] = nm
            it["name_source"] = "user"
    return {"running": sorted(by_sid.values(), key=lambda x: x.get("startedAt") or 0, reverse=True),
            "recent": recent, "prefs": prefs, "error": err, "ts": time.time(), "modes": PERMISSION_MODES,
            "models": MODELS, "overseer_dir": ASSISTANT_DIR, "index": dict(INDEX_STATE)}


def control_mode(item):
    job = item.get("job")
    if job and not job.get("done"):
        return "job"            # a `claude -p` we spawned is still running
    if item.get("kind") == "interactive" and item.get("alive"):
        return "tmux" if item.get("tmux") else "locked"   # open in a terminal: only tmux can type into it
    if item.get("kind") == "background" and item.get("alive"):
        return "background"     # use `claude attach` / logs; no safe way to inject text here
    return "resume"             # not running anywhere: safe to `claude -p --resume`


# ----------------------------------------------------------------------------- transcript parsing
def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _images_of(content, limit=4, max_chars=3_000_000):
    """base64 image blocks (Read of a png, pasted screenshots) -> [{media_type, data}], oversized ones skipped."""
    out = []
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict) and b.get("type") == "image":
            src = b.get("source") or {}
            if src.get("type") == "base64" and src.get("data") and len(src["data"]) <= max_chars:
                out.append({"media_type": src.get("media_type", "image/png"), "data": src["data"]})
            if len(out) >= limit:
                break
    return out


def parse_record(d):
    """One jsonl record -> list of display items."""
    t = d.get("type")
    items = []
    if t in ("user", "assistant"):
        if d.get("isSidechain"):
            return items
        ts = d.get("timestamp")
        msg = d.get("message") or {}
        content = msg.get("content")
        if t == "user":
            if isinstance(content, str):
                items.append({"role": "user", "text": content, "ts": ts, "meta": bool(d.get("isMeta"))})
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        items.append({"role": "user", "text": b.get("text", ""), "ts": ts, "meta": bool(d.get("isMeta"))})
                    elif b.get("type") == "tool_result":
                        txt = _text_of(b.get("content"))
                        it = {"role": "tool_result", "text": txt[:6000], "truncated": len(txt) > 6000,
                              "tool_use_id": b.get("tool_use_id"), "is_error": bool(b.get("is_error")), "ts": ts}
                        imgs = _images_of(b.get("content"))
                        if imgs:
                            it["images"] = imgs
                        items.append(it)
                # pasted images travel as image blocks next to the text: attach them to that user message
                imgs = _images_of(content)
                if imgs:
                    tgt = next((x for x in reversed(items) if x["role"] == "user"), None)
                    if tgt is None:
                        tgt = {"role": "user", "text": "", "ts": ts, "meta": False}
                        items.append(tgt)
                    tgt["images"] = imgs
        else:
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and b.get("text"):
                    items.append({"role": "assistant", "text": b["text"], "ts": ts, "model": msg.get("model")})
                elif bt == "tool_use":
                    inp = b.get("input")
                    try:
                        s = json.dumps(inp, ensure_ascii=False)
                    except Exception:
                        s = str(inp)
                    items.append({"role": "tool_use", "name": b.get("name"), "id": b.get("id"),
                                  "input": s[:2000], "truncated": len(s) > 2000, "ts": ts})
                elif bt == "thinking" and b.get("thinking"):
                    items.append({"role": "thinking", "text": b["thinking"][:3000], "ts": ts})
    elif t == "system":
        if d.get("subtype") == "turn_duration":
            items.append({"role": "system", "text": f"turn finished in {round((d.get('durationMs') or 0)/1000)}s",
                          "ts": d.get("timestamp")})
        elif d.get("content"):
            items.append({"role": "system", "text": _text_of(d.get("content"))[:2000], "ts": d.get("timestamp")})
    elif t == "ai-title":
        items.append({"role": "meta", "title": d.get("aiTitle")})
    elif t == "cost-state":
        items.append({"role": "meta", "cost_usd": d.get("totalCostUSD")})
    elif t == "permission-mode":
        items.append({"role": "meta", "permission_mode": d.get("permissionMode")})
    elif t == "frame-link" and d.get("frameUrl") and d.get("path"):
        items.append({"role": "meta", "frame_link": {"url": d["frameUrl"], "path": d["path"], "title": d.get("title")}})
    return items


def read_transcript(path, offset):
    """Read complete lines from byte `offset` (-1 = tail). Returns (items, new_offset, size)."""
    size = os.path.getsize(path)
    items = []
    with open(path, "rb") as f:
        if offset is None or offset < 0:
            offset = max(0, size - TAIL_BYTES)
            f.seek(offset)
            if offset > 0:
                f.readline()  # drop the partial first line
                offset = f.tell()
        else:
            f.seek(min(offset, size))
        buf = f.read()
    end = buf.rfind(b"\n")
    if end < 0:
        return items, offset, size
    for line in buf[:end].split(b"\n"):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        items.extend(parse_record(d))
    return items, offset + end + 1, size


# ----------------------------------------------------------------------------- full-text index (sqlite FTS5)
# Every transcript's user/assistant/tool text goes into ~/.cache/session_console/index.db, indexed incrementally by byte
# offset, so a search over all sessions is one FTS query instead of a 1GB grep. unicode61 strips diacritics, so
# "minh" also finds "mình". The same table feeds the per-session wiki cards (/api/wiki) and the overseer session.
INDEX_DB = os.path.join(CACHE_DIR, "index.db")
INDEX_LOCK = threading.Lock()
INDEX_STATE = {"ts": 0, "busy": False, "files": 0, "msgs": 0, "err": None}
_INDEX_LOCAL = threading.local()


def index_db():
    """One sqlite connection per thread (sqlite objects are not shareable); schema created on first use."""
    c = getattr(_INDEX_LOCAL, "conn", None)
    if c is None:
        os.makedirs(os.path.dirname(INDEX_DB), exist_ok=True)
        c = sqlite3.connect(INDEX_DB, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS files(sid TEXT PRIMARY KEY, path TEXT, offset INTEGER, size INTEGER, mtime REAL, n INTEGER);
        CREATE TABLE IF NOT EXISTS msgs(id INTEGER PRIMARY KEY, sid TEXT, seq INTEGER, ts TEXT, role TEXT, name TEXT, text TEXT);
        CREATE INDEX IF NOT EXISTS msgs_sid ON msgs(sid, seq);
        CREATE TABLE IF NOT EXISTS cards(sid TEXT PRIMARY KEY, n_at INTEGER, card TEXT, ts REAL, model TEXT, err TEXT);
        CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, content='msgs', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
        CREATE TRIGGER IF NOT EXISTS msgs_ai AFTER INSERT ON msgs BEGIN INSERT INTO fts(rowid, text) VALUES (new.id, new.text); END;
        CREATE TRIGGER IF NOT EXISTS msgs_ad AFTER DELETE ON msgs BEGIN INSERT INTO fts(fts, rowid, text) VALUES ('delete', old.id, old.text); END;
        """)
        _INDEX_LOCAL.conn = c
    return c


def _index_text_of(d):
    """jsonl record -> (role, name, text) rows worth indexing. Tool calls keep their target (path / command head)."""
    out = []
    t = d.get("type")
    if t not in ("user", "assistant") or d.get("isSidechain"):
        return out
    content = (d.get("message") or {}).get("content")
    if t == "user":
        if d.get("isMeta"):
            return out
        if isinstance(content, str):
            if content.strip() and not content.startswith("<"):  # skip injected <system-reminder>/<local-command> blobs
                out.append(("user", None, content[:20000]))
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip() and not b["text"].startswith("<"):
                    out.append(("user", None, b["text"][:20000]))
                elif b.get("type") == "tool_result":
                    txt = _text_of(b.get("content")).strip()
                    if txt:
                        out.append(("tool_result", None, txt[:4000]))
    else:
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                out.append(("assistant", None, b["text"][:20000]))
            elif b.get("type") == "tool_use":
                inp = b.get("input") or {}
                bits = [str(inp.get(k)) for k in ("file_path", "path", "notebook_path", "pattern", "query", "url", "description") if inp.get(k)]
                if inp.get("command"):
                    bits.append(str(inp["command"])[:300])
                if inp.get("prompt"):
                    bits.append(str(inp["prompt"])[:600])
                if bits:
                    out.append(("tool_use", b.get("name"), "\n".join(bits)[:2000]))
    return out


def index_file(c, path, sid):
    """Index the part of one transcript written since last time. Returns rows added."""
    try:
        st = os.stat(path)
    except OSError:
        return 0
    row = c.execute("SELECT offset, size, n FROM files WHERE sid=?", (sid,)).fetchone()
    offset, n = (row[0], row[2]) if row else (0, 0)
    if row and st.st_size < row[1]:  # truncated / rewritten -> start over
        c.execute("DELETE FROM msgs WHERE sid=?", (sid,))
        offset, n = 0, 0
    if row and st.st_size == row[1] and offset >= st.st_size:
        c.execute("UPDATE files SET mtime=? WHERE sid=?", (st.st_mtime, sid))
        return 0
    added = 0
    with open(path, "rb") as f:
        f.seek(offset)
        buf = f.read()
    end = buf.rfind(b"\n")
    if end >= 0:
        rows = []
        for line in buf[:end].split(b"\n"):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = d.get("timestamp")
            for role, name, text in _index_text_of(d):
                rows.append((sid, n, ts, role, name, text))
                n += 1
        if rows:
            c.executemany("INSERT INTO msgs(sid, seq, ts, role, name, text) VALUES (?,?,?,?,?,?)", rows)
            added = len(rows)
        offset += end + 1
    c.execute("INSERT OR REPLACE INTO files(sid, path, offset, size, mtime, n) VALUES (?,?,?,?,?,?)",
              (sid, path, offset, st.st_size, st.st_mtime, n))
    return added


def refresh_index(force=False, min_interval=15):
    """Bring the index up to date with every transcript on disk. Cheap when nothing changed (one stat per file)."""
    if not force and time.time() - INDEX_STATE["ts"] < min_interval:
        return INDEX_STATE
    if not INDEX_LOCK.acquire(blocking=force):
        return INDEX_STATE
    try:
        INDEX_STATE["busy"] = True
        c = index_db()
        known = {r[0]: (r[1], r[2]) for r in c.execute("SELECT sid, size, mtime FROM files")}
        files = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
        added = 0
        for p in files:
            sid = os.path.splitext(os.path.basename(p))[0]
            try:
                st = os.stat(p)
            except OSError:
                continue
            k = known.get(sid)
            if k and k[0] == st.st_size and abs(k[1] - st.st_mtime) < 1e-6:
                continue
            added += index_file(c, p, sid)
            c.commit()
        gone = set(known) - {os.path.splitext(os.path.basename(p))[0] for p in files}
        for sid in gone:
            c.execute("DELETE FROM msgs WHERE sid=?", (sid,))
            c.execute("DELETE FROM files WHERE sid=?", (sid,))
        c.commit()
        INDEX_STATE.update({"ts": time.time(), "err": None, "files": len(files),
                            "msgs": c.execute("SELECT count(*) FROM msgs").fetchone()[0]})
    except Exception as e:
        INDEX_STATE["err"] = str(e)[:300]
    finally:
        INDEX_STATE["busy"] = False
        INDEX_LOCK.release()
    return INDEX_STATE


def index_loop():
    time.sleep(2)
    while True:
        try:
            refresh_index(force=True)
            n = summarize_pending(limit=4) if os.environ.get("SESSION_CONSOLE_CARDS", "1") != "0" else 0
        except Exception as e:
            INDEX_STATE["err"] = str(e)[:300]
            n = 0
        time.sleep(5 if n >= 4 else 30)  # backfill quickly while cards are missing, then idle


def fts_query(q):
    """User text -> FTS5 MATCH expression: quoted phrases stay phrases, other words become prefix terms, AND-ed."""
    terms = []
    for m in re.finditer(r'"([^"]+)"|(\S+)', q):
        if m.group(1):
            terms.append('"' + m.group(1).replace('"', '') + '"')
        else:
            w = re.sub(r'[^\w\-\./:@]', ' ', m.group(2)).strip()
            if not w:
                continue
            terms.append('"' + w.replace('"', '') + '"' + ('*' if len(w) >= 3 else ''))
    return " ".join(terms)


def search_index(q, limit=40, per_session=3, roles=None):
    refresh_index()
    c = index_db()
    expr = fts_query(q)
    if not expr:
        return {"query": q, "sessions": [], "total": 0}
    rolesql, params = "", [expr]
    if roles:
        rolesql = " AND m.role IN (%s)" % ",".join("?" * len(roles))
        params += list(roles)
    try:
        rows = c.execute(f"""
            SELECT m.sid, m.seq, m.ts, m.role, m.name, snippet(fts, 0, '\x02', '\x03', '…', 18) AS snip, bm25(fts) AS score
            FROM fts JOIN msgs m ON m.id = fts.rowid
            WHERE fts MATCH ?{rolesql}
            ORDER BY score LIMIT 600""", params).fetchall()
    except sqlite3.OperationalError as e:
        return {"query": q, "sessions": [], "total": 0, "error": str(e)}
    by = {}
    for sid, seq, ts, role, name, snip, score in rows:
        s = by.setdefault(sid, {"sid": sid, "hits": 0, "conv": 0, "best": score, "last_ts": None, "snippets": [], "_tool": []})
        s["hits"] += 1
        s["best"] = min(s["best"], score)
        if ts and (not s["last_ts"] or ts > s["last_ts"]):
            s["last_ts"] = ts
        sn = {"seq": seq, "ts": ts, "role": role, "name": name, "text": snip[:400]}
        if role in ("user", "assistant"):
            s["conv"] += 1
            if len(s["snippets"]) < per_session:
                s["snippets"].append(sn)
        elif len(s["_tool"]) < per_session:
            s["_tool"].append(sn)
    for s in by.values():  # conversation snippets first, tool hits fill the rest
        s["snippets"] = (s["snippets"] + s.pop("_tool"))[:per_session]
    sessions = sorted(by.values(), key=lambda x: (-min(x["conv"], 3), x["best"]))[:limit]
    for s in sessions:
        path = transcript_path(s["sid"])
        if path:
            m = transcript_meta(path)
            s.update({"title": m.get("title"), "first_prompt": m.get("first_prompt"), "cwd": m.get("cwd"),
                      "mtime": os.path.getmtime(path)})
            group_of(s)
            row = c.execute("SELECT card FROM cards WHERE sid=?", (s["sid"],)).fetchone()
            if row and row[0]:
                s["summary"] = (json.loads(row[0]) or {}).get("summary")
    return {"query": q, "expr": expr, "sessions": sessions, "total": len(rows), "index": dict(INDEX_STATE)}


# ----------------------------------------------------------------------------- wiki cards
# One card per session: facts straight from the index (times, turns, files touched) + a short Vietnamese summary written
# once by `claude -p --model haiku` and cached in the cards table. Refreshed when the session grew by >= CARD_REGROW rows.
CARD_MODEL = os.environ.get("SESSION_CONSOLE_CARD_MODEL", "haiku")
CARD_REGROW = 20
CARD_LOCK = threading.Lock()
FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"}
PATH_RE = re.compile(r"^(/[^\s:*?\"<>|]+|~/[^\s:*?\"<>|]+)$")


def session_facts(c, sid):
    rows = c.execute("SELECT seq, ts, role, name, text FROM msgs WHERE sid=? ORDER BY seq", (sid,)).fetchall()
    files, users, assistants, tools = {}, [], [], 0
    first_ts = last_ts = None
    for seq, ts, role, name, text in rows:
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        if role == "user":
            users.append((seq, ts, text))
        elif role == "assistant":
            assistants.append((seq, ts, text))
        elif role == "tool_use":
            tools += 1
            if name in FILE_TOOLS:
                for ln in text.split("\n")[:2]:
                    ln = ln.strip()
                    if PATH_RE.match(ln):
                        files[ln] = files.get(ln, 0) + 1
    top = sorted(files.items(), key=lambda kv: -kv[1])[:15]
    return {"n": len(rows), "user_turns": len(users), "assistant_msgs": len(assistants), "tool_calls": tools,
            "first_ts": first_ts, "last_ts": last_ts, "files": [{"path": p, "n": n} for p, n in top],
            "_users": users, "_assistants": assistants}


def card_prompt(sid, meta, facts):
    head = [f"- {t[:500]}" for _, _, t in facts["_users"][:8]]
    tail_u = [f"- {t[:300]}" for _, _, t in facts["_users"][-3:]] if len(facts["_users"]) > 8 else []
    tail_a = [f"- {t[:900]}" for _, _, t in facts["_assistants"][-3:]]
    files = ", ".join(f["path"] for f in facts["files"][:10])
    return f"""Bạn ghi thẻ wiki cho một phiên làm việc với Claude Code. Dựa vào trích đoạn dưới đây, trả về DUY NHẤT một JSON object (không markdown, không giải thích) dạng:
{{"summary": "1 câu tiếng Việt nói phiên này làm gì", "bullets": ["3-7 gạch đầu dòng tiếng Việt: việc đã làm, kết luận, quyết định, lỗi đã sửa"], "keywords": ["5-10 từ khoá tìm kiếm, giữ nguyên tên file/hàm/ticker/tiếng Anh"], "status": "done|ongoing|abandoned|unknown"}}
Viết ngắn, cụ thể (tên file, hàm, con số), không bịa điều không có trong trích đoạn.

Thư mục: {meta.get('cwd')} · nhánh: {meta.get('branch')} · tiêu đề tự sinh: {meta.get('title')}
Số lượt người dùng: {facts['user_turns']} · tool calls: {facts['tool_calls']} · từ {facts['first_ts']} tới {facts['last_ts']}
File đụng nhiều nhất: {files or '(không)'}

Các yêu cầu đầu của người dùng:
{chr(10).join(head) or '(trống)'}
{('Yêu cầu gần cuối:' + chr(10) + chr(10).join(tail_u)) if tail_u else ''}
Trả lời gần cuối của agent:
{chr(10).join(tail_a) or '(trống)'}
"""


def write_card(sid, timeout=120):
    """Summarize one session with claude -p (no transcript written) and store the card. Returns the card dict."""
    c = index_db()
    path = transcript_path(sid)
    meta = transcript_meta(path) if path else {}
    facts = session_facts(c, sid)
    prompt = card_prompt(sid, meta, facts)
    card, err = None, None
    try:
        r = subprocess.run([CLAUDE_BIN, "-p", "--model", CARD_MODEL, "--no-session-persistence", "--output-format", "text",
                            "--disallowedTools", "*"], input=prompt, capture_output=True, text=True, timeout=timeout,
                           cwd=HOME, env=child_env())
        out = (r.stdout or "").strip()
        m = re.search(r"\{.*\}", out, re.S)
        if r.returncode != 0 or not m:
            err = (r.stderr or out or "no output").strip()[:300]
        else:
            card = json.loads(m.group(0))
            card = {"summary": str(card.get("summary", ""))[:600], "bullets": [str(b)[:400] for b in card.get("bullets", [])][:8],
                    "keywords": [str(k)[:60] for k in card.get("keywords", [])][:12], "status": card.get("status", "unknown")}
    except Exception as e:
        err = str(e)[:300]
    c.execute("INSERT OR REPLACE INTO cards(sid, n_at, card, ts, model, err) VALUES (?,?,?,?,?,?)",
              (sid, facts["n"], json.dumps(card, ensure_ascii=False) if card else None, time.time(), CARD_MODEL, err))
    c.commit()
    return card, err


def cards_pending(c, limit=3):
    """Sessions whose card is missing or stale, newest transcript first."""
    rows = c.execute("""SELECT f.sid, f.n, f.mtime, k.n_at, k.ts, k.err FROM files f LEFT JOIN cards k ON k.sid=f.sid
                        WHERE f.n >= 3 ORDER BY f.mtime DESC""").fetchall()
    out = []
    now = time.time()
    for sid, n, mtime, n_at, ts, err in rows:
        if n_at is None:
            out.append(sid)
        elif err and now - (ts or 0) < 3600:
            continue  # failed recently: retry later
        elif n - n_at >= CARD_REGROW and now - mtime > 120:  # grew, and quiet for 2 minutes
            out.append(sid)
        if len(out) >= limit:
            break
    return out


def summarize_pending(limit=3):
    if not CARD_LOCK.acquire(blocking=False):
        return 0
    try:
        c = index_db()
        done = 0
        for sid in cards_pending(c, limit):
            write_card(sid)
            done += 1
        return done
    finally:
        CARD_LOCK.release()


def wiki_card(sid, now=False):
    c = index_db()
    path = transcript_path(sid)
    if not path:
        return None
    meta = transcript_meta(path)
    facts = session_facts(c, sid)
    row = c.execute("SELECT n_at, card, ts, model, err FROM cards WHERE sid=?", (sid,)).fetchone()
    card = json.loads(row[1]) if row and row[1] else None
    stale = not row or (facts["n"] - (row[0] or 0) >= CARD_REGROW)
    if now and stale and facts["n"] >= 3:
        with CARD_LOCK:
            card, err = write_card(sid)
            row = (facts["n"], None, time.time(), CARD_MODEL, err)
            stale = False
    item = group_of({"sessionId": sid, "cwd": meta.get("cwd")})
    return {"sid": sid, "title": meta.get("title"), "first_prompt": meta.get("first_prompt"), "cwd": meta.get("cwd"),
            "branch": meta.get("branch"), "model": meta.get("model"), "cost_usd": meta.get("cost_usd"),
            "group": item.get("group"), "group_name": item.get("group_name"), "mtime": os.path.getmtime(path),
            "facts": {k: v for k, v in facts.items() if not k.startswith("_")},
            "card": card, "card_ts": row[2] if row else None, "card_err": row[4] if row else None, "stale": stale}


def wiki_list():
    """Compact card for every indexed session (sidebar wiki view)."""
    c = index_db()
    rows = c.execute("""SELECT f.sid, f.path, f.n, f.mtime, k.card, k.n_at, k.err,
                        (SELECT min(ts) FROM msgs m WHERE m.sid=f.sid), (SELECT max(ts) FROM msgs m WHERE m.sid=f.sid)
                        FROM files f LEFT JOIN cards k ON k.sid=f.sid WHERE f.n >= 1 ORDER BY f.mtime DESC""").fetchall()
    out = []
    for sid, path, n, mtime, card, n_at, err, first_ts, last_ts in rows:
        if not os.path.exists(path):
            continue
        meta = transcript_meta(path)
        if not meta.get("first_prompt") and not meta.get("title"):
            continue
        item = group_of({"sessionId": sid, "cwd": meta.get("cwd")})
        out.append({"sid": sid, "title": meta.get("title"), "first_prompt": meta.get("first_prompt"), "cwd": meta.get("cwd"),
                    "group": item["group"], "group_name": item["group_name"], "mtime": mtime, "n": n,
                    "first_ts": first_ts, "last_ts": last_ts, "card": json.loads(card) if card else None,
                    "stale": n_at is None or n - n_at >= CARD_REGROW, "card_err": err})
    pend = c.execute("SELECT count(*) FROM files f LEFT JOIN cards k ON k.sid=f.sid WHERE f.n>=3 AND k.sid IS NULL").fetchone()[0]
    return {"sessions": out, "pending": pend, "index": dict(INDEX_STATE)}


# ----------------------------------------------------------------------------- control
def tmux_send(target, text):
    subprocess.run(["tmux", "send-keys", "-t", target, "-l", "--", text], check=True, timeout=5)
    time.sleep(0.15)
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True, timeout=5)


def job_view(job):
    if not job:
        return None
    return {"pid": job["proc"].pid, "done": job["done"], "rc": job.get("rc"), "started": job["started"],
            "fresh": job["fresh"], "last": job["lines"][-1] if job["lines"] else None,
            "stderr": job["stderr"][-1500:], "result": job.get("result")}


def spawn_claude(sid, text, cwd, mode, fresh, model=None, name=None):
    if mode not in PERMISSION_MODES:
        raise ValueError(f"bad permission mode {mode!r}")
    if model and not MODEL_RE.match(model):
        raise ValueError(f"bad model {model!r}")
    if not cwd or not os.path.isdir(cwd):
        raise ValueError(f"cwd does not exist: {cwd!r}")
    cmd = [CLAUDE_BIN, "-p", "--verbose", "--output-format", "stream-json", "--permission-mode", mode]
    if model:
        cmd += ["--model", model]
    if name:
        cmd += ["--name", name[:80]]
    cmd += ["--session-id", sid] if fresh else ["--resume", sid]
    proc = subprocess.Popen(cmd, cwd=cwd, env=child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    job = {"proc": proc, "lines": [], "stderr": "", "done": False, "rc": None, "started": time.time(),
           "fresh": fresh, "result": None, "cwd": cwd}
    with JOBS_LOCK:
        JOBS[sid] = job

    def feed():
        try:
            proc.stdin.write(text)
            proc.stdin.close()
        except Exception:
            pass

    def pump_out():
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                d = json.loads(line)
                summary = d.get("type")
                if d.get("type") == "result":
                    job["result"] = {k: d.get(k) for k in ("subtype", "is_error", "total_cost_usd", "duration_ms", "num_turns")}
                    summary = "result:" + str(d.get("subtype"))
            except Exception:
                summary = line[:200]
            job["lines"].append(summary)
            if len(job["lines"]) > 500:
                del job["lines"][:-500]

    def pump_err():
        for line in proc.stderr:
            job["stderr"] = (job["stderr"] + line)[-8000:]

    def wait():
        proc.wait()
        job["rc"] = proc.returncode
        job["done"] = True

    for fn in (feed, pump_out, pump_err, wait):
        threading.Thread(target=fn, daemon=True).start()
    return job


RESUME_MODE = os.environ.get("SESSION_CONSOLE_RESUME", "tmux")  # "tmux" (interactive, full tools) or "headless" (claude -p)
STRIP_ENV = ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
             "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SSE_PORT", "CLAUDE_PID", "CLAUDE_EFFORT"]


def tmux_available():
    return bool(shutil.which("tmux"))


def tmux_target_of(sid, pid=None):
    """tmux pane for a registered session: the CLI writes its pane into the registry; fall back to ppid walk."""
    for p in glob.glob(os.path.join(SESS_REG, "*.json")):
        d = read_json(p)
        if isinstance(d, dict) and d.get("sessionId") == sid and d.get("pid") and pid_alive(d["pid"]):
            return (pane_for_pid(d["pid"], tmux_panes()) or d.get("tmux")), d["pid"]
    return None, None


def start_in_tmux(sid, cwd, resume=True, name=None, model=None, mode=None):
    """Open an INTERACTIVE claude in a detached tmux session (full tools: artifacts, permission prompts…)
    and wait until its input box is ready. Returns the tmux target to type into."""
    if not tmux_available():
        raise ValueError("tmux is not installed; set SESSION_CONSOLE_RESUME=headless to use claude -p instead")
    if not cwd or not os.path.isdir(cwd):
        raise ValueError(f"cwd does not exist: {cwd!r}")
    tname = "cc-" + sid[:8]
    parts = [CLAUDE_BIN]
    parts += ["--resume", sid] if resume else ["--session-id", sid]
    if name:
        parts += ["--name", name[:80]]
    if model and MODEL_RE.match(model):
        parts += ["--model", model]
    if mode in PERMISSION_MODES and mode != "default":
        parts += ["--permission-mode", mode]
    import shlex
    # CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=true also switches off the CLI's "showing fewer suggestions" back-off
    # (after 20 ignored suggestions it only generates one every 10 turns), so the ghost text is there every turn
    inner = ("env " + " ".join("-u " + v for v in STRIP_ENV) + " CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=true "
             + " ".join(shlex.quote(x) for x in parts))
    # a session with that name may linger from an earlier run of the same sid
    subprocess.run(["tmux", "kill-session", "-t", tname], capture_output=True, timeout=5)
    r = subprocess.run(["tmux", "new-session", "-d", "-s", tname, "-x", "200", "-y", "50", "-c", cwd, inner],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise ValueError("tmux new-session failed: " + (r.stderr or r.stdout).strip()[:200])
    target = tname + ":0.0"
    deadline = time.time() + 30
    trusted = False
    while time.time() < deadline:
        time.sleep(0.6)
        try:
            cap = subprocess.run(["tmux", "capture-pane", "-t", target, "-p"], capture_output=True, text=True, timeout=5).stdout
        except Exception:
            cap = ""
        low = cap.lower()
        if "trust this folder" in low and not trusted:
            # first run in this folder: the user chose it in the console, so answer the trust prompt for them
            subprocess.run(["tmux", "send-keys", "-t", target, "Down", "Enter"], timeout=5)
            trusted = True
            continue
        lines = [l.rstrip() for l in cap.split("\n") if l.strip()]
        # empty input box = "❯" alone, or (CLI >= 2.1.326) "❯ Try \"…\"" with the placeholder hint on the same line
        ready = any(re.match(r"^\s*❯\s*(?:$|Try \"|Press |Type )", lines[i]) and i > 0 and re.match(r"^[─━]{4,}", lines[i - 1])
                    for i in range(max(0, len(lines) - 5), len(lines)))
        if ready:
            tg, _ = tmux_target_of(sid)
            return tg or target
    raise ValueError(f"claude did not become ready in tmux session {tname} within 30s; open it with: tmux attach -t {tname}")


def stop_job(sid):
    with JOBS_LOCK:
        job = JOBS.get(sid)
    if job and not job["done"]:
        job["proc"].terminate()
        return True
    return False


# ----------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "session_console/0.1"

    def log_message(self, fmt, *args):
        if os.environ.get("SESSION_CONSOLE_DEBUG"):
            super().log_message(fmt, *args)

    # -- plumbing
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authed(self, q):
        if not TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        tok = auth[7:] if auth.startswith("Bearer ") else q.get("token", [None])[0]
        return tok == TOKEN

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 24 * 1024 * 1024:
            raise ValueError("request body too large (max 24MB)")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    # -- routes
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path.startswith("/static/"):
            name = os.path.basename(u.path)
            fp = os.path.join(STATIC, name)
            if name != "index.html" and os.path.isfile(fp):
                ctype = {"svg": "image/svg+xml", "png": "image/png", "webmanifest": "application/manifest+json",
                         "json": "application/json", "css": "text/css", "js": "text/javascript"}.get(name.rsplit(".", 1)[-1], "application/octet-stream")
                with open(fp, "rb") as f:
                    data = f.read()
                self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=86400"); self.end_headers(); self.wfile.write(data)
                return
            return self._send(404, {"error": "not found"})
        if not self._authed(q):
            return self._send(401, {"error": "token required"})
        if u.path == "/api/sessions":
            return self._send(200, list_sessions())
        if u.path == "/api/transcript":
            sid = q.get("sid", [""])[0]
            path = transcript_path(sid)
            if not path:
                return self._send(404, {"error": "no transcript for this session id"})
            offset = int(q.get("offset", ["-1"])[0])
            items, new_off, size = read_transcript(path, offset)
            return self._send(200, {"items": items, "offset": new_off, "size": size, "path": path})
        if u.path == "/api/stream":
            return self._stream(q)
        if u.path == "/api/jobs":
            with JOBS_LOCK:
                return self._send(200, {sid: job_view(j) for sid, j in JOBS.items()})
        if u.path == "/api/prefs":
            with PREFS_LOCK:
                return self._send(200, load_prefs())
        if u.path == "/api/dirs":
            return self._send(200, list_dirs())
        if u.path == "/api/commands":
            cwd = q.get("cwd", [""])[0]
            return self._send(200, {"commands": scan_commands(git_root(cwd) or cwd or HOME)})
        if u.path == "/api/ls":
            return self._send(200, list_subdirs(q.get("path", [""])[0]))
        if u.path == "/api/links":
            # artifact url -> local source file, from the transcript's frame-link records (artifacts published from files)
            sid = q.get("sid", [""])[0]
            path = transcript_path(sid)
            out = {}
            if path:
                try:
                    size = os.path.getsize(path)
                    with open(path, "rb") as f:
                        f.seek(max(0, size - 4 * 1024 * 1024))
                        for line in f.read().decode("utf-8", "replace").splitlines():
                            if '"frame-link"' not in line:
                                continue
                            try:
                                d = json.loads(line)
                            except Exception:
                                continue
                            if d.get("type") == "frame-link" and d.get("frameUrl") and d.get("path"):
                                out[d["frameUrl"]] = {"path": d["path"], "title": d.get("title")}
                except OSError:
                    pass
            return self._send(200, {"links": out})
        if u.path == "/api/git":
            try:
                return self._send(200, git_info(q.get("root", [HOME])[0]))
            except Exception as e:
                return self._send(400, {"error": str(e)})
        if u.path == "/api/tree":
            try:
                return self._send(200, list_tree(q.get("root", [HOME])[0], q.get("rel", [""])[0]))
            except Exception as e:
                return self._send(400, {"error": str(e)})
        if u.path == "/api/file":
            try:
                return self._send(200, read_file_preview(q.get("root", [HOME])[0], q.get("rel", [""])[0]))
            except Exception as e:
                return self._send(400, {"error": str(e)})
        if u.path == "/api/raw":
            try:
                root = check_browse_root(q.get("root", [HOME])[0])
            except Exception as e:
                return self._send(400, {"error": str(e)})
            rel = q.get("rel", [""])[0]
            full = os.path.abspath(os.path.join(root, rel))
            if not full.startswith(root + os.sep) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            ext = full.rsplit(".", 1)[-1].lower()
            ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
                     "svg": "image/svg+xml", "html": "text/html; charset=utf-8", "htm": "text/html; charset=utf-8",
                     "css": "text/css", "js": "text/javascript", "json": "application/json", "txt": "text/plain; charset=utf-8",
                     "md": "text/plain; charset=utf-8", "csv": "text/plain; charset=utf-8", "pdf": "application/pdf",
                     "ipynb": "application/json", "xml": "text/xml", "typ": "text/plain; charset=utf-8",
                     "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                     "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                     "zip": "application/zip"}.get(ext, "application/octet-stream")
            disp = "inline" if ctype.split(";")[0] in ("application/pdf",) or ctype.startswith(("text/", "image/")) or ext in ("json", "ipynb") else "attachment"
            with open(full, "rb") as f:
                data = f.read()
            self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'{disp}; filename="{os.path.basename(full)}"')
            self.end_headers(); self.wfile.write(data); return
        if u.path == "/api/files":
            cwd = q.get("cwd", [""])[0]
            root = git_root(cwd) or cwd or HOME
            qq = q.get("q", [""])[0]
            idx = file_index(root)
            return self._send(200, {"root": root, "total": len(idx), "files": fuzzy_rank(qq, idx) if qq else idx[:30]})
        if u.path == "/api/search":
            qq = q.get("q", [""])[0].strip()
            roles = [r for r in q.get("roles", [""])[0].split(",") if r]
            if not qq:
                return self._send(200, {"query": "", "sessions": [], "total": 0, "index": dict(INDEX_STATE)})
            return self._send(200, search_index(qq, limit=int(q.get("limit", ["40"])[0]), roles=roles or None))
        if u.path == "/api/wiki":
            sid = q.get("sid", [""])[0]
            if sid:
                card = wiki_card(sid, now=q.get("now", [""])[0] == "1")
                return self._send(200, card) if card else self._send(404, {"error": "unknown session"})
            return self._send(200, wiki_list())
        if u.path == "/api/index":
            return self._send(200, refresh_index(force=q.get("force", [""])[0] == "1"))
        if u.path == "/api/pane":
            sid = q.get("sid", [""])[0]
            item = self._running_item(sid)
            if not item or not item.get("tmux"):
                return self._send(404, {"error": "not a tmux session"})
            try:
                return self._send(200, pane_capture(item["tmux"], int(q.get("lines", ["45"])[0])))
            except Exception as e:
                return self._send(500, {"error": str(e)})
        return self._send(404, {"error": "not found"})

    def _stream(self, q):
        """Server-sent events: new transcript items as the file grows, plus job status."""
        sid = q.get("sid", [""])[0]
        offset = int(q.get("offset", ["-1"])[0])
        path = transcript_path(sid)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(ev, payload):
            self.wfile.write(f"event: {ev}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        last_job = None
        try:
            while True:
                if path is None:
                    path = transcript_path(sid)  # a fresh session's file appears after the first turn
                if path and os.path.exists(path):
                    if os.path.getsize(path) > offset:
                        items, offset, _ = read_transcript(path, offset)
                        if items:
                            emit("items", {"items": items, "offset": offset})
                with JOBS_LOCK:
                    jv = job_view(JOBS.get(sid))
                if jv != last_job:
                    emit("job", jv)
                    last_job = jv
                tick = locals().get("tick", 0) + 1
                if tick % 5 == 0:
                    emit("ping", {"t": time.time()})
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authed(q):
            return self._send(401, {"error": "token required"})
        try:
            body = self._body()
        except Exception as e:
            return self._send(413, {"error": str(e)})
        try:
            if u.path == "/api/send":
                return self._send(200, self._do_send(body))
            if u.path == "/api/new":
                return self._send(200, self._do_new(body))
            if u.path == "/api/stop":
                sid = body.get("sid", "")
                if stop_job(sid):
                    return self._send(200, {"ok": True, "via": "job"})
                r = run([CLAUDE_BIN, "stop", sid], timeout=20)
                return self._send(200 if r.returncode == 0 else 500,
                                  {"ok": r.returncode == 0, "via": "claude stop", "out": (r.stdout + r.stderr)[-500:]})
            if u.path == "/api/prefs":
                return self._send(200, update_prefs(body))
            if u.path == "/api/upload":
                return self._send(200, save_upload(body))
            if u.path == "/api/keys":
                item = self._running_item(body.get("sid", ""))
                if not item or not item.get("tmux"):
                    raise ValueError("keys can only be sent to a session running inside tmux")
                tmux_keys(item["tmux"], body.get("keys"), body.get("text"))
                return self._send(200, {"ok": True, "target": item["tmux"]})
            if u.path == "/api/bglogs":
                r = run([CLAUDE_BIN, "logs", body.get("sid", "")], timeout=20)
                return self._send(200, {"out": (r.stdout + r.stderr)[-20000:]})
        except Exception as e:
            return self._send(500, {"error": str(e)})
        return self._send(404, {"error": "not found"})

    def _running_item(self, sid):
        """Cheap lookup from the session registry only (no `claude agents` call): pid, kind, tmux pane."""
        panes = None
        for d in glob.glob(os.path.join(SESS_REG, "*.json")):
            r = read_json(d)
            if isinstance(r, dict) and r.get("sessionId") == sid and r.get("pid") and pid_alive(r["pid"]):
                if panes is None:
                    panes = tmux_panes()
                tm = pane_for_pid(r["pid"], panes) or r.get("tmux")
                return {"pid": r["pid"], "tmux": tm, "name": r.get("name"), "kind": r.get("kind", "interactive"),
                        "short": sid[:8], "cwd": r.get("cwd")}
        return None

    def _control_for(self, sid):
        with JOBS_LOCK:
            job = JOBS.get(sid)
        if job and not job["done"]:
            return "job", None
        item = self._running_item(sid)
        if not item:
            return "resume", None
        if item["kind"] == "background":
            return "background", item
        return ("tmux" if item.get("tmux") else "locked"), item

    def _do_send(self, body):
        sid, text = body.get("sid", ""), body.get("text", "")
        mode = body.get("mode") or "default"
        if not sid or not text.strip():
            raise ValueError("sid and text are required")
        t0 = time.time()
        control, item = self._control_for(sid)
        print(f"[send] {time.strftime('%H:%M:%S')}.{int(t0*1000)%1000:03d} sid={sid[:8]} control={control} "
              f"lookup={time.time()-t0:.3f}s text={text[:40]!r}", file=sys.stderr, flush=True)
        model = body.get("model") or None
        if model and not MODEL_RE.match(model):
            raise ValueError(f"bad model {model!r}")
        if control == "tmux":
            if model:  # switch model in the live session first, like typing /model <name> in the terminal
                tmux_send(item["tmux"], "/model " + model)
                time.sleep(1.2)
            tmux_send(item["tmux"], text)
            return {"ok": True, "via": "tmux", "target": item["tmux"], "model": model}
        if control == "locked":
            raise ValueError("session is open in a terminal that is not inside tmux; "
                             "type there, or restart it inside tmux so this console can send keys")
        if control == "background":
            raise ValueError("background session is still running; use `claude attach " + item["short"] + "` in a terminal")
        if control == "job":
            raise ValueError("a `claude -p` run for this session is still in progress; wait for it to finish")
        path = transcript_path(sid)
        if not path:
            raise ValueError("unknown session id")
        if RESUME_MODE == "tmux" and tmux_available():
            cwd = body.get("cwd") or transcript_meta(path).get("cwd") or (item or {}).get("cwd")
            target = start_in_tmux(sid, cwd, resume=True, model=model, mode=body.get("mode") or None)
            tmux_send(target, text)
            return {"ok": True, "via": "tmux-start", "target": target, "cwd": cwd, "model": model}
        if text.lstrip().startswith("!"):
            raise ValueError("'!' bash mode only works in a live terminal/tmux session; this one would be resumed headlessly")
        cwd = body.get("cwd") or transcript_meta(path).get("cwd") or (item or {}).get("cwd")
        job = spawn_claude(sid, text, cwd, mode, fresh=False, model=model)
        return {"ok": True, "via": "resume", "pid": job["proc"].pid, "cwd": cwd, "model": model}

    def _do_new(self, body):
        text, cwd = body.get("text", ""), body.get("cwd") or HOME
        mode = body.get("mode") or "default"
        if not text.strip():
            raise ValueError("text is required")
        cwd = os.path.abspath(os.path.expanduser(cwd))
        sid = str(uuid.uuid4())
        model = body.get("model") or None
        name = (body.get("name") or "").strip() or None
        if RESUME_MODE == "tmux" and tmux_available():
            target = start_in_tmux(sid, cwd, resume=False, name=name, model=model, mode=mode)
            remember_cwd(cwd)
            if name:
                remember_name(sid, name)
            tmux_send(target, text)
            return {"ok": True, "sessionId": sid, "via": "tmux-start", "target": target, "cwd": cwd}
        job = spawn_claude(sid, text, cwd, mode, fresh=True, model=model, name=name)
        remember_cwd(cwd)
        if name:
            remember_name(sid, name)  # shown in the sidebar immediately, even before the CLI registers it
        return {"ok": True, "sessionId": sid, "pid": job["proc"].pid, "cwd": cwd}


TOKEN_PATH = os.path.join(CONFIG_DIR, "token")


def load_or_create_token():
    """One token per machine, generated on first run and kept in the config dir (mode 0600)."""
    try:
        with open(TOKEN_PATH) as f:
            t = f.read().strip()
        if t:
            return t
    except OSError:
        pass
    import secrets
    t = secrets.token_hex(16)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        f.write(t + "\n")
    os.chmod(TOKEN_PATH, 0o600)
    return t


def ensure_assistant_dir():
    """Create the assistant workspace on first run: CLAUDE.md (how to use `session-console wiki`) and a settings file
    that pre-allows that command. Files the user already edited are left alone."""
    os.makedirs(os.path.join(ASSISTANT_DIR, ".claude"), exist_ok=True)
    pairs = [(os.path.join(ASSISTANT_TEMPLATE, "CLAUDE.md"), os.path.join(ASSISTANT_DIR, "CLAUDE.md")),
             (os.path.join(ASSISTANT_TEMPLATE, "settings.json"), os.path.join(ASSISTANT_DIR, ".claude", "settings.json"))]
    for src, dst in pairs:
        if not os.path.exists(dst) and os.path.exists(src):
            shutil.copyfile(src, dst)


def add_serve_args(ap):
    ap.add_argument("--host", default=os.environ.get("SESSION_CONSOLE_HOST", "127.0.0.1"),
                    help="0.0.0.0 to reach it from other devices (LAN / Tailscale)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("SESSION_CONSOLE_PORT", "8765")))
    ap.add_argument("--token", default=os.environ.get("SESSION_CONSOLE_TOKEN"),
                    help="access token; default: the one in %s (created on first run)" % TOKEN_PATH)
    ap.add_argument("--no-token", action="store_true", help="no token at all (loopback only)")
    return ap


def serve(a):
    global TOKEN
    loopback = a.host in ("127.0.0.1", "localhost", "::1")
    if a.no_token:
        if not loopback:
            sys.exit("--no-token is only allowed on a loopback host")
        TOKEN = None
    else:
        TOKEN = a.token or load_or_create_token()
    ensure_assistant_dir()
    if not shutil.which(CLAUDE_BIN):
        print(f"warning: {CLAUDE_BIN!r} not on PATH; listing falls back to ~/.claude/sessions and sending is disabled",
              file=sys.stderr)
    threading.Thread(target=index_loop, daemon=True, name="index").start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    shown = a.host if a.host not in ("0.0.0.0", "::") else (local_ip() or "127.0.0.1")
    print(f"session-console on http://{shown}:{a.port}/" + (f"?token={TOKEN}" if TOKEN else ""), flush=True)
    if TOKEN:
        print(f"token file: {TOKEN_PATH}  (session-console token prints it again)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def local_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def main(argv=None):
    a = add_serve_args(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)).parse_args(argv)
    serve(a)


if __name__ == "__main__":
    main()
