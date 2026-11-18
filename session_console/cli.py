"""`session-console` command line: run the server, print the token, query the wiki, install as a background service."""
import argparse
import os
import platform
import shutil
import subprocess
import sys

from . import __version__
from . import server as srv

SERVICE_NAME = "session-console"


def _exe():
    """Path of this command for the service file (console script if installed, else `python -m session_console`)."""
    p = shutil.which("session-console")
    if p:
        return [p]
    return [sys.executable, "-m", "session_console"]


def _path_env():
    """PATH for the service: the user's PATH plus the usual places `claude` lives (services get a tiny PATH)."""
    extra = [os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.npm-global/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    cur = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(dict.fromkeys([*cur, *extra]))


def cmd_run(a):
    srv.serve(a)


def cmd_token(a):
    print(srv.load_or_create_token())


def cmd_wiki(a):
    from . import wiki
    wiki.main(a.rest)


def cmd_where(a):
    print("config:    ", srv.CONFIG_DIR)
    print("token:     ", srv.TOKEN_PATH)
    print("prefs:     ", srv.PREFS_PATH)
    print("index db:  ", srv.INDEX_DB)
    print("uploads:   ", srv.UPLOAD_DIR)
    print("assistant: ", srv.ASSISTANT_DIR)
    print("claude:    ", shutil.which(srv.CLAUDE_BIN) or f"{srv.CLAUDE_BIN!r} NOT on PATH")
    print("tmux:      ", shutil.which("tmux") or "NOT installed")


def _systemd_unit(a):
    exe = " ".join(_exe())
    return f"""[Unit]
Description=Claude session console
After=network.target

[Service]
Environment=PATH={_path_env()}
Environment=CLAUDE_BIN={srv.CLAUDE_BIN}
ExecStart={exe} run --host {a.host} --port {a.port}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""


def _launchd_plist(a, log):
    args = "".join(f"\n        <string>{x}</string>" for x in [*_exe(), "run", "--host", a.host, "--port", str(a.port)])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>com.claude.{SERVICE_NAME}</string>
    <key>ProgramArguments</key><array>{args}
    </array>
    <key>EnvironmentVariables</key><dict>
        <key>PATH</key><string>{_path_env()}</string>
        <key>CLAUDE_BIN</key><string>{srv.CLAUDE_BIN}</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{log}</string>
    <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""


def cmd_install_service(a):
    srv.load_or_create_token()  # so the service never has to invent one
    if platform.system() == "Darwin":
        d = os.path.expanduser("~/Library/LaunchAgents")
        os.makedirs(d, exist_ok=True)
        log = os.path.join(srv.CACHE_DIR, "server.log")
        os.makedirs(srv.CACHE_DIR, exist_ok=True)
        path = os.path.join(d, f"com.claude.{SERVICE_NAME}.plist")
        with open(path, "w") as f:
            f.write(_launchd_plist(a, log))
        subprocess.run(["launchctl", "unload", path], capture_output=True)
        r = subprocess.run(["launchctl", "load", "-w", path], capture_output=True, text=True)
        print(f"wrote {path}\n" + (r.stderr or "loaded; log at " + log))
    elif shutil.which("systemctl"):
        d = os.path.expanduser("~/.config/systemd/user")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{SERVICE_NAME}.service")
        with open(path, "w") as f:
            f.write(_systemd_unit(a))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        r = subprocess.run(["systemctl", "--user", "enable", "--now", SERVICE_NAME], capture_output=True, text=True)
        print(f"wrote {path}\n" + (r.stderr.strip() or "enabled and started"))
        print(f"logs: journalctl --user -u {SERVICE_NAME} -f")
        if shutil.which("loginctl"):
            print("to keep it running when you are not logged in: loginctl enable-linger $USER")
    else:
        sys.exit("no systemd or launchd here; run `session-console run` under your own supervisor")
    print(f"\nopen: http://<this-machine>:{a.port}/?token={srv.load_or_create_token()}")


def cmd_uninstall_service(a):
    if platform.system() == "Darwin":
        path = os.path.expanduser(f"~/Library/LaunchAgents/com.claude.{SERVICE_NAME}.plist")
        subprocess.run(["launchctl", "unload", path], capture_output=True)
        if os.path.exists(path):
            os.remove(path)
        print("removed", path)
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", SERVICE_NAME], capture_output=True)
        path = os.path.expanduser(f"~/.config/systemd/user/{SERVICE_NAME}.service")
        if os.path.exists(path):
            os.remove(path)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        print("removed", path)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="session-console", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    sp = ap.add_subparsers(dest="cmd")
    p = srv.add_serve_args(sp.add_parser("run", help="start the web console (default command)"))
    p.set_defaults(f=cmd_run)
    sp.add_parser("token", help="print the access token (created on first use)").set_defaults(f=cmd_token)
    sp.add_parser("where", help="show config / cache / assistant paths and whether claude + tmux are found").set_defaults(f=cmd_where)
    p = sp.add_parser("wiki", help="query the transcript index: search / list / card / show / now", add_help=False)
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(f=cmd_wiki)
    p = srv.add_serve_args(sp.add_parser("install-service", help="install as a user service (systemd on Linux, launchd on macOS)"))
    p.set_defaults(f=cmd_install_service)
    sp.add_parser("uninstall-service", help="remove the user service").set_defaults(f=cmd_uninstall_service)
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "wiki":  # hand everything after `wiki` to its own parser (so `wiki --help` works)
        from . import wiki
        return wiki.main(argv[1:])
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version"):
        argv = ["run", *argv]  # `session-console --host 0.0.0.0` == `session-console run --host 0.0.0.0`
    a = ap.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
