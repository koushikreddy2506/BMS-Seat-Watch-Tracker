#!/usr/bin/env python3
"""
start_site.py — run Seat Watch and get a public link your friends can open.

    python start_site.py            # website + public link
    python start_site.py --watch    # also run your personal watcher

By default the link is a Cloudflare quick tunnel: no account, works from any
network, but it changes every run (it's pushed to your ntfy status topic).
For a link that never changes, add a "site" section to watch_config.json:
a Cloudflare named tunnel on your own domain, or a free ngrok static domain
(see site_settings()). Ctrl+C stops everything.
"""

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
WIN_PATHS = (r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
             r"C:\Program Files\cloudflared\cloudflared.exe")


def find_cloudflared(override=None):
    if override:
        return override
    found = shutil.which("cloudflared")
    if found:
        return found
    for p in WIN_PATHS:
        if Path(p).exists():
            return p
    sys.exit("cloudflared not found.\n  Windows: winget install Cloudflare.cloudflared\n"
             "  Linux:   https://pkg.cloudflare.com (or the .deb from GitHub releases)")


def site_up(port):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
        return True
    except Exception:
        return False


def admin_key():
    try:
        c = sqlite3.connect(HERE / "bms_server.db")
        r = c.execute("SELECT v FROM settings WHERE k='admin_token'").fetchone()
        return r[0] if r else ""
    except Exception:
        return ""


def save_public_url(url):
    """The site puts this link in the Approve / Reject buttons of auto-hold
    request alerts; quick-tunnel links change every run, so keep it current."""
    try:
        c = sqlite3.connect(HERE / "bms_server.db")
        with c:
            c.execute("INSERT OR REPLACE INTO settings VALUES ('public_url', ?)", (url,))
        c.close()
    except Exception as e:
        print(f"  (couldn't save public link for the site: {e})")


def push_link(url):
    """Send the fresh link to your phone so you can forward it."""
    try:
        cfg = json.loads((HERE / "watch_config.json").read_text())
        topic = cfg.get("status_topic") or cfg.get("ntfy_topic")
        if not topic:
            return
        h = {"Title": "Seat Watch link", "Click": url}
        tok = (cfg.get("ntfy_token") or "").strip()
        tf = HERE / "ntfy_token.txt"
        if not tok and tf.exists():
            tok = tf.read_text().strip()
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=url.encode(), headers=h)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"  (couldn't push link to phone: {e})")


def site_settings():
    """The "site" section of watch_config.json: which kind of public link to run.

      "site": {"tunnel": "quick"}                                  default, new link every run
      "site": {"tunnel": "cloudflare", "cloudflare_tunnel": "seatwatch",
               "hostname": "seats.example.com"}                    fixed link, needs a domain on Cloudflare
      "site": {"tunnel": "ngrok", "ngrok_domain": "xyz.ngrok-free.app"}   fixed free link
    """
    try:
        return json.loads((HERE / "watch_config.json").read_text(encoding="utf-8")).get("site") or {}
    except (OSError, ValueError):
        return {}


def find_ngrok():
    found = shutil.which("ngrok")
    if found:
        return found
    for p in (Path.home() / "AppData/Local/Microsoft/WinGet/Links/ngrok.exe",
              Path(r"C:\Program Files\ngrok\ngrok.exe"), HERE / "ngrok.exe"):
        if p.exists():
            return str(p)
    return None


def announce(url):
    key = admin_key()
    print("\n" + "=" * 64)
    print(f"  SHARE THIS:  {url}")
    if key:
        print(f"  ADMIN:       {url}/admin?key={key}   (keep private)")
    print("=" * 64 + "\n")
    save_public_url(url)
    push_link(url)


def tunnel_command(cf, port):
    """(command, fixed public url or None, text that means 'connected')."""
    s = site_settings()
    kind = (s.get("tunnel") or "quick").lower()
    if kind == "cloudflare" and s.get("cloudflare_tunnel") and s.get("hostname"):
        # named tunnel: set up once with `cloudflared tunnel login / create / route dns`
        return ([cf, "tunnel", "--no-autoupdate", "run", "--url", f"http://localhost:{port}",
                 s["cloudflare_tunnel"]], f"https://{s['hostname']}", "Registered tunnel connection")
    if kind == "ngrok" and s.get("ngrok_domain"):
        ngrok = find_ngrok()
        if ngrok:
            return ([ngrok, "http", f"--url={s['ngrok_domain']}", str(port), "--log=stdout"],
                    f"https://{s['ngrok_domain']}", "started tunnel")
        print("ngrok not found (winget install ngrok.ngrok); using a quick tunnel instead")
    elif kind != "quick":
        print(f'"site" tunnel "{kind}" is missing its settings; using a quick tunnel instead')
    # A throwaway config stops cloudflared picking up a named-tunnel config.yml
    # from ~/.cloudflared, which would make the quick tunnel refuse to start.
    quiet_cfg = HERE / ".quick_tunnel.yml"
    quiet_cfg.write_text("no-autoupdate: true\n")
    return ([cf, "tunnel", "--config", str(quiet_cfg), "--url", f"http://localhost:{port}"], None, None)


def run_tunnel(cf, port, stop):
    while not stop.is_set():
        cmd, fixed_url, ready = tunnel_command(cf, port)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        run_tunnel.proc = proc
        announced = False
        for line in proc.stdout:
            if announced:
                continue
            if fixed_url:
                if ready.lower() in line.lower():
                    announced = True
                    announce(fixed_url)
                elif "error" in line.lower() or "err_" in line.lower():
                    print("  tunnel:", line.strip()[:160])
            else:
                m = URL_RE.search(line)
                if m:
                    announced = True
                    announce(m.group(0))
        proc.wait()
        if stop.is_set():
            break
        print("tunnel dropped — reconnecting in 5s"
              + ("" if fixed_url else " (you'll get a new link)"))
        time.sleep(5)


run_tunnel.proc = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--watch", action="store_true", help="also run your personal watcher")
    ap.add_argument("--cloudflared", help="path to cloudflared if it isn't found")
    args = ap.parse_args()

    cf = find_cloudflared(args.cloudflared)
    procs = []

    def start_server():
        # -u: unbuffered, otherwise the site's messages never reach server.log
        log = open(HERE / "server.log", "a")
        p = subprocess.Popen(
            [sys.executable, "-u", "bms_server.py", "--host", "127.0.0.1", "--port", str(args.port)],
            cwd=HERE, stdout=log, stderr=subprocess.STDOUT)
        p.started = time.time()
        procs.append(p)
        return p

    server = None
    if site_up(args.port):
        print(f"site already running on :{args.port} — just adding a public link")
    else:
        server = start_server()
        for _ in range(40):
            if site_up(args.port):
                break
            if server.poll() is not None:
                sys.exit("website failed to start — see server.log")
            time.sleep(0.5)
        else:
            sys.exit("website didn't answer in 20s — see server.log")
        print(f"website running on http://localhost:{args.port}")

    if args.watch:
        procs.append(subprocess.Popen(
            [sys.executable, "bms_seat_watch.py", "--config", "watch_config.json", "--force"],
            cwd=HERE))
        print("personal watcher running")

    stop = threading.Event()
    threading.Thread(target=run_tunnel, args=(cf, args.port, stop), daemon=True).start()
    print("opening public link…")

    # watchdog: the website must always be up (the tracker windows come and go,
    # the site doesn't).
    #  - the site process we started exits (crash, or stopped to load new code)
    #    -> start it again within ~1s. That's also the way to update the site:
    #    stop bms_server.py and let this bring it back; never start it by hand.
    #  - the site stops answering (hung, or started elsewhere and died)
    #    -> two failed checks 10s apart, then start it again.
    misses, last_check = 0, time.time()
    try:
        while True:
            time.sleep(1)
            if server is not None and server.poll() is not None:
                # died within seconds of starting (e.g. port still held by a hung
                # copy): don't spin, leave it to the HTTP check below
                if time.time() - server.started < 5:
                    print(f"{time.strftime('%H:%M:%S')}  website exited right after starting; "
                          "retrying via the health check")
                    server = None
                    continue
                print(f"{time.strftime('%H:%M:%S')}  website exited, starting it again")
                server = start_server()
                last_check = time.time()      # give it a moment before HTTP checks
                continue
            if time.time() - last_check < 10:
                continue
            last_check = time.time()
            if site_up(args.port):
                misses = 0
                continue
            misses += 1
            if misses >= 2:
                print(f"{time.strftime('%H:%M:%S')}  website not answering, restarting it")
                server = start_server()
                misses = 0
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        stop.set()
        for p in [run_tunnel.proc, *procs]:
            if p and p.poll() is None:
                p.terminate()
        print("stopped")


if __name__ == "__main__":
    main()
