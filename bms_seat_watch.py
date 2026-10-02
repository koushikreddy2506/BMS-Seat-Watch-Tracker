#!/usr/bin/env python3
"""
bms_seat_watch.py — BookMyShow availability watcher.

Watches two kinds of thing:
  1. cinema_page  — a venue's day page, for a NEW SHOW appearing on a screen
                    you care about (e.g. DOLBY CINEMA)
  2. seat_api     — /api/movies-data/seatlayout/v1/primary, for categories
                    opening up on shows that already exist

Runs through YOUR real Chrome over the debugging port, so requests carry a
genuine session. Chrome must already be running:

  & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\\bms\\chrome-profile"

Usage:
  python bms_seat_watch.py --config watch_config.json --dry-run   # one cycle, no alerts
  python bms_seat_watch.py --config watch_config.json --test-alert
  python bms_seat_watch.py --config watch_config.json --test-count --url "<seat layout url>"
  python bms_seat_watch.py --config watch_config.json            # run forever

Email credentials come from environment variables, never the config file:
  SMTP_HOST  SMTP_PORT  SMTP_USER  SMTP_PASS
"""

import argparse
import csv
import json
import logging
import os
import random
import re
import smtplib
import sqlite3
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import requests

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    # Only needed when use_browser is true. Browserless mode runs on plain
    # HTTP, so this must not be a hard requirement.
    sync_playwright = None


LOG = logging.getLogger("bms")


# ==========================================================================
# Parsing
# ==========================================================================

def extract_initial_state(html: str):
    """Pull window.__INITIAL_STATE__ out of a BookMyShow page."""
    i = html.find("__INITIAL_STATE__")
    if i == -1:
        return None
    start = html.find("{", i)
    if start == -1:
        return None

    # brace scanner that respects strings and escapes
    depth = 0
    in_str = False
    esc = False
    for j in range(start, len(html)):
        c = html[j]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:j + 1])
                except json.JSONDecodeError:
                    break
    # fallback: up to closing script tag
    sc = html.find("</script>", start)
    if sc != -1:
        try:
            return json.loads(html[start:sc].rstrip().rstrip(";"))
        except json.JSONDecodeError:
            pass
    return None


def parse_cinema_page(html: str):
    """
    Return list of shows from a venue day page:
      {movie, event_code, dimension, screen, attributes, show_time,
       show_time_code, avail, categories:{areaCatCode: availStatus}}
    """
    state = extract_initial_state(html)
    if not state:
        return []

    queries = (state.get("venueShowtimesFunctionalApi") or {}).get("queries") or {}
    key = next((k for k in queries if "getShowtimesByVenue" in k), None)
    if not key:
        return []
    tra = ((queries[key] or {}).get("data") or {}).get("showDetailsTransformed") or {}

    shows = []
    for ev in tra.get("Event") or []:
        title = ev.get("EventTitle")
        for ce in ev.get("ChildEvents") or []:
            for st in ce.get("ShowTimes") or []:
                cats = {c.get("AreaCatCode"): str(c.get("AvailStatus"))
                        for c in (st.get("Categories") or []) if c.get("AreaCatCode")}
                cat_names = {c.get("AreaCatCode"): c.get("PriceDesc")
                             for c in (st.get("Categories") or []) if c.get("AreaCatCode")}
                cat_best = {c.get("AreaCatCode"): str(c.get("BestAvailableSeats") or "0")
                            for c in (st.get("Categories") or []) if c.get("AreaCatCode")}
                cat_prices = {c.get("AreaCatCode"): c.get("UpdatedPrice") or c.get("CurPrice")
                              for c in (st.get("Categories") or []) if c.get("AreaCatCode")}
                shows.append({
                    "movie": title,
                    "event_code": ce.get("EventCode"),
                    "dimension": ce.get("EventDimension"),
                    "screen": st.get("ScreenName"),
                    "attributes": st.get("Attributes"),
                    "show_time": st.get("ShowTime"),
                    "show_time_code": str(st.get("ShowTimeCode")),
                    "session_id": st.get("SessionId"),
                    "avail": str(st.get("AvailStatus")),
                    "best_avail": str(st.get("BestAvailableSeats") or "0"),
                    "categories": cats,
                    "category_names": cat_names,
                    "category_prices": cat_prices,
                    "cat_best": cat_best,
                })
    return shows


def parse_seat_api(payload: dict):
    """Parse /api/movies-data/seatlayout/v1/primary into the same show shape."""
    data = (payload or {}).get("data") or {}
    ev = data.get("eventData") or {}
    shows = []
    for st in data.get("showTimes") or []:
        cats = {c.get("areaCatCode"): str(c.get("availStatus"))
                for c in (st.get("categories") or []) if c.get("areaCatCode")}
        cat_names = {c.get("areaCatCode"): c.get("priceDesc")
                     for c in (st.get("categories") or []) if c.get("areaCatCode")}
        sess = st.get("sessionId")
        key = st.get("seatSelectorKey") or ""
        if (not sess or sess == "<REDACTED>") and "_" in key:
            sess = key.split("_", 1)[1]
        shows.append({
            "movie": ev.get("eventTitle"),
            "event_code": ev.get("eventCode"),
            "dimension": ev.get("eventDimension"),
            "screen": None,
            "attributes": st.get("attributes"),
            "show_time": st.get("showTime"),
            "show_time_code": str(st.get("showTimeCode")),
            "session_id": sess,
            "avail": str(st.get("availStatus")),
            "best_avail": "0",
            "categories": cats,
            "category_names": cat_names,
            "cat_best": {},
        })
    return shows


def show_key(s: dict) -> str:
    return f"{s.get('event_code')}|{s.get('show_time_code')}"


# ==========================================================================
# Diffing
# ==========================================================================

CLOSED = {"0", "None", "none", ""}


def as_int(v) -> int:
    try:
        return int(float(str(v).strip() or 0))
    except (TypeError, ValueError):
        return 0


def diff_shows(old: dict, new_shows: list, watch_filter=None,
               best_always: bool = False, best_repeat_seconds: int = 900,
               best_baseline_zero: bool = False, baseline_zero: bool = False,
               first_run: bool = False):
    """
    Compare previous snapshot against current shows.
    Returns (events, snapshot).

    first_run=True -> this target has never been polled, so record a baseline
                      silently. Distinct from "polled before and the day was
                      empty", which SHOULD alert when shows appear.
    baseline_zero=True -> ignore the stored baseline entirely.
    """
    events = []
    snapshot = {}
    now_ts = time.time()

    for s in new_shows:
        if watch_filter and not watch_filter(s):
            continue
        k = show_key(s)
        snapshot[k] = {"avail": s["avail"], "categories": dict(s["categories"]),
                       "best_avail": s.get("best_avail", "0"),
                       "cat_best": dict(s.get("cat_best") or {}),
                       "best_last_alert": 0}
        prev = old.get(k)

        if prev is None:
            if not first_run:
                events.append({"type": "NEW_SHOW", "show": s,
                               "detail": f"{s.get('show_time')} on {s.get('attributes') or s.get('screen')}"})
            continue

        if baseline_zero:
            # Ignore what we recorded before: pretend everything was closed.
            # Anything currently available therefore alerts on every poll.
            prev = {"avail": "0",
                    "categories": {c: "0" for c in s["categories"]},
                    "best_avail": "0",
                    "cat_best": {},
                    "best_last_alert": 0}

        # a category that didn't exist before = held-back block released
        for cat, status in s["categories"].items():
            cname = s['category_names'].get(cat, cat)
            if cat not in prev["categories"]:
                events.append({"type": "NEW_CATEGORY", "show": s, "category": cat,
                               "detail": f"{cname} appeared (status {status})"})
                continue
            was = prev["categories"][cat]
            if was == status:
                continue
            if was in CLOSED and status not in CLOSED:
                events.append({"type": "CATEGORY_OPENED", "show": s, "category": cat,
                               "detail": f"{cname} went {was} -> {status}"})
            elif status in CLOSED:
                # sold out / withdrawn: logged, but not worth waking you up
                events.append({"type": "CATEGORY_CLOSED", "show": s, "category": cat,
                               "quiet": True,
                               "detail": f"{cname} went {was} -> {status} (no longer available)"})
            else:
                # both non-zero: fill level moved. A lower number generally
                # means more seats, so this is how extra stock shows up in a
                # category that was already open.
                direction = ("MORE seats likely" if as_int(status) < as_int(was)
                             else "filling up")
                events.append({"type": "CATEGORY_CHANGED", "show": s, "category": cat,
                               "detail": f"{cname} status {was} -> {status} ({direction})"})

        # BestAvailableSeats: centre seats, the strongest release signal
        prev_best = str(prev.get("best_avail", "0"))
        now_best = str(s.get("best_avail", "0"))
        last_alert = float(prev.get("best_last_alert", 0) or 0)

        if best_baseline_zero:
            # treat the baseline as 0 every poll: any non-zero fires, every cycle
            if now_best not in CLOSED:
                events.append({"type": "BEST_SEATS", "show": s,
                               "detail": f"BEST (centre) seats available: {now_best}"})
            snapshot[k]["best_last_alert"] = now_ts
        else:
            fired = False
            if now_best not in CLOSED:
                opened = prev_best in CLOSED
                grew = as_int(now_best) > as_int(prev_best)
                due = best_always and (now_ts - last_alert) >= best_repeat_seconds
                if opened or grew or due:
                    if opened:
                        why = f"BEST (centre) seats appeared: {prev_best} -> {now_best}"
                    elif grew:
                        why = f"BEST seats increased: {prev_best} -> {now_best}"
                    else:
                        why = f"BEST seats still available: {now_best}"
                    events.append({"type": "BEST_SEATS", "show": s, "detail": why})
                    fired = True
            snapshot[k]["best_last_alert"] = now_ts if fired else last_alert

        prev_cb = prev.get("cat_best") or {}
        for cat, best in (s.get("cat_best") or {}).items():
            if str(best) in CLOSED:
                continue
            pb = "0" if best_baseline_zero else str(prev_cb.get(cat, "0"))
            if pb in CLOSED or as_int(best) > as_int(pb):
                events.append({"type": "BEST_SEATS", "show": s, "category": cat,
                               "detail": f"BEST seats in {s['category_names'].get(cat, cat)}: {best}"})

        if prev["avail"] in CLOSED and s["avail"] not in CLOSED:
            events.append({"type": "SHOW_OPENED", "show": s,
                           "detail": f"show availability {prev['avail']} -> {s['avail']}"})

    return events, snapshot


# ==========================================================================
# Browser plumbing
# ==========================================================================

class Chrome:
    """Thin wrapper around a CDP-attached Chrome."""

    def __init__(self, port: int):
        if sync_playwright is None:
            raise SystemExit(
                "This config has use_browser: true, which needs Playwright.\n"
                "Either install it:   pip install playwright && playwright install chromium\n"
                'or set  "use_browser": false  in the config (recommended).')
        self._pw = sync_playwright().start()
        try:
            self.browser = self._pw.chromium.connect_over_cdp(f"http://localhost:{port}")
        except Exception as e:
            self._pw.stop()
            raise SystemExit(
                f"\nCan't attach to Chrome on port {port}.\n  {e}\n\n"
                "Start Chrome first:\n"
                '  & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
                '--remote-debugging-port=9222 --user-data-dir="C:\\bms\\chrome-profile"\n'
            )
        self.ctx = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        self.page = self._bms_page()

    def _bms_page(self):
        for p in self.ctx.pages:
            try:
                if "bookmyshow.com" in (p.url or ""):
                    return p
            except Exception:
                continue
        p = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        try:
            if "bookmyshow.com" not in (p.url or ""):
                p.goto("https://in.bookmyshow.com/", wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        return p

    def fetch(self, url: str, as_json: bool = True):
        """Fetch inside the page so cookies and origin come along."""
        _t0 = time.time()
        try:
            res = self.page.evaluate(
                """async (u) => {
                    const sep = u.includes('?') ? '&' : '?';
                    const busted = u + sep + '_ts=' + Date.now();
                    const r = await fetch(busted, {
                        credentials: 'include',
                        cache: 'no-store',
                        headers: {'accept': 'application/json, text/html',
                                  'cache-control': 'no-cache',
                                  'pragma': 'no-cache'}});
                    return {status: r.status, body: await r.text(),
                            fromCache: r.headers.get('cf-cache-status') || '',
                            age: r.headers.get('age') || ''};
                }""", url)
        except Exception as e:
            LOG.warning("fetch failed for %s: %s", url[:70], e)
            self.page = self._bms_page()
            return None
        if res.get("status", 0) >= 400:
            LOG.warning("HTTP %s for %s", res["status"], url[:70])
            return None
        self.last_fetch_ms = int((time.time() - _t0) * 1000)
        self.last_cache = (res.get("fromCache") or "").upper()
        self.last_age = str(res.get("age") or "")
        if self.last_cache in ("HIT", "STALE") or (res.get("age") or "0") not in ("", "0"):
            self.cache_hits = getattr(self, "cache_hits", 0) + 1
        body = res.get("body") or ""
        if not as_json:
            return body
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            LOG.warning("non-JSON response from %s", url[:70])
            return None

    def count_seats(self, url: str):
        """Open a seat layout page and count seats by status. Best effort."""
        page = self.ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            for _ in range(40):  # up to ~20s for the grid to render
                page.wait_for_timeout(500)
                n = page.evaluate("document.querySelectorAll('[class*=seat],[data-seat],[id*=seat]').length")
                if n and n > 20:
                    break
            result = page.evaluate("""() => {
                const nodes = Array.from(document.querySelectorAll(
                    '[class*=seat],[data-seat],[id*=seat],[class*=Seat]'));
                const byClass = {};
                let available = 0;
                for (const el of nodes) {
                    if (el.children.length > 0) continue;      // leaf nodes only
                    const cls = (el.className || '').toString().trim().slice(0, 60);
                    byClass[cls] = (byClass[cls] || 0) + 1;
                    const s = (el.getAttribute('data-status') || '').trim();
                    const blob = (cls + ' ' + s).toLowerCase();
                    if (s === '1' || s === '4' ||
                        (blob.includes('avail') && !blob.includes('unavail')))
                        available++;
                }
                return {total: nodes.length, available,
                        byClass: Object.fromEntries(
                            Object.entries(byClass).sort((a,b)=>b[1]-a[1]).slice(0,12))};
            }""")
            # diagnostics: what did the page actually show?
            result["url"] = page.url
            try:
                result["visible_text"] = page.inner_text("body")[:300]
                result["div_count"] = page.evaluate("document.querySelectorAll('div').length")
            except Exception:
                pass
            return result
        except Exception as e:
            LOG.warning("seat count failed: %s", e)
            return None
        finally:
            try:
                page.close()
            except Exception:
                pass

    def seat_page_status(self, url: str, wait_seconds: int = 40,
                         cinema_url: str = None, session_id: str = None):
        """
        Open the seat page and wait for real seats. Returns (status, detail).
        Status is BOOKABLE only when seat elements actually render.
        """
        page = self.ctx.new_page()
        try:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                return "UNREACHABLE", f"navigation failed: {str(e)[:70]}"

            deadline = time.time() + wait_seconds
            last_seats = 0
            while time.time() < deadline:
                page.wait_for_timeout(1000)
                try:
                    seats = page.evaluate(
                        "document.querySelectorAll("
                        "'[class*=seat],[class*=Seat],[data-seat],[id*=seat]').length")
                    body = (page.inner_text("body") or "")[:4000]
                except Exception:
                    continue
                last_seats = seats
                low = body.lower()

                if any(m in low for m in ERROR_MARKERS):
                    return "DOWN", "error page"
                if seats >= 20:
                    return BOOKABLE, f"{seats} seat elements rendered"
                if not any(m in low for m in STUCK_MARKERS) and seats > 0:
                    return BOOKABLE, f"{seats} seat elements rendered"

            # timed out
            try:
                body = (page.inner_text("body") or "")[:300].replace("\n", " ")
            except Exception:
                body = ""
            status = classify_page(body) if body else "UNREACHABLE"
            if status == "UP":
                status = "STUCK"
            return status, f"after {wait_seconds}s: seats={last_seats} text={body[:90]!r}"
        finally:
            try:
                page.close()
            except Exception:
                pass

    def close(self):
        try:
            self._pw.stop()
        except Exception:
            pass


class HttpClient:
    """
    Plain HTTP fetching — no browser. The cinema pages are server-rendered and
    served with cf-cache-status DYNAMIC, so requests is enough.

    Same interface as Chrome so check_target doesn't care which is in use.
    Runs anywhere Python does: Pi, old laptop, phone under Termux, server.
    """

    def __init__(self, cfg=None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (cfg or {}).get("user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        })
        self.last_fetch_ms = 0
        self.last_cache = ""
        self.last_age = ""
        self.ctx = None

    def fetch(self, url: str, as_json: bool = True):
        t0 = time.time()
        try:
            r = self.session.get(url, timeout=25)
        except Exception as e:
            LOG.warning("fetch failed for %s: %s", url[:70], str(e)[:90])
            return None
        self.last_fetch_ms = int((time.time() - t0) * 1000)
        self.last_cache = (r.headers.get("cf-cache-status") or "").upper()
        self.last_age = r.headers.get("age") or ""
        if r.status_code >= 400:
            LOG.warning("HTTP %s for %s", r.status_code, url[:70])
            return None
        low = r.text[:4000].lower()
        if "just a moment" in low or "cf-challenge" in low:
            LOG.warning("Cloudflare challenge — consider use_browser: true")
            return None
        if not as_json:
            return r.text
        try:
            return r.json()
        except ValueError:
            LOG.warning("non-JSON response from %s", url[:70])
            return None

    def count_seats(self, url: str):
        return None       # needs a browser; not used by cinema_page targets

    def seat_page_status(self, url, wait_seconds=40, cinema_url=None, session_id=None):
        html = self.fetch(url, as_json=False)
        return (classify_page(html), "no browser: page text only")

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass


def make_client(cfg: dict):
    if cfg.get("use_browser", True):
        c = Chrome(cfg.get("cdp_port", 9222))
        LOG.info("attached to Chrome")
        return c
    LOG.info("browserless mode — plain HTTP, no Chrome needed")
    return HttpClient(cfg)


# ==========================================================================
# Alerting
# ==========================================================================

# ==========================================================================
# Delivery queue: nothing is dropped. If ntfy rate-limits us, messages wait
# and retry until they go through. Alerts always jump ahead of status/heartbeat.
# ==========================================================================

import itertools
import queue


def ntfy_auth(cfg=None):
    """
    Authorization header for ntfy.sh. Without it, ntfy treats every request as
    anonymous and applies the free per-IP limits, paid plan or not.
    Looked up in order: config "ntfy_token", env NTFY_TOKEN, file ntfy_token.txt.
    Keep it in the file — it's gitignored, so the token never lands in the repo.
    """
    tok = ((cfg or {}).get("ntfy_token") or os.environ.get("NTFY_TOKEN") or "").strip()
    if not tok:
        f = Path(__file__).resolve().parent / "ntfy_token.txt"
        if f.exists():
            tok = f.read_text(encoding="utf-8").strip()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


OUTBOX = queue.PriorityQueue()
_SEQ = itertools.count()
_SENDER = {"started": False}
PRIO_ALERT, PRIO_STATUS = 0, 1


def _sender_loop():
    backoff = 5
    while True:
        prio, _, url, data, headers, what = OUTBOX.get()
        errors = 0
        while True:
            try:
                r = requests.post(url, data=data, headers=headers, timeout=15)
            except Exception as e:
                errors += 1
                if errors >= 8:
                    LOG.error("%s gave up after network errors: %s", what, e)
                    break
                time.sleep(min(5 * errors, 60))
                continue
            if r.status_code == 200:
                if backoff > 5:
                    LOG.info("ntfy accepting again — delivered %s (%d still queued)",
                             what, OUTBOX.qsize())
                backoff = 5
                break
            if r.status_code == 429:
                LOG.warning("%s held by ntfy rate limit — retrying in %ss (%d queued behind it)",
                            what, backoff, OUTBOX.qsize())
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
            if r.status_code in (401, 403):
                LOG.error("%s REFUSED: ntfy says the token is invalid or lacks access (HTTP %s). "
                          "Check ntfy_token.txt.", what, r.status_code)
            else:
                LOG.warning("%s rejected by ntfy: HTTP %s %s", what, r.status_code, r.text[:80])
            break
        OUTBOX.task_done()


def _enqueue(prio, url, body, headers, what):
    if not _SENDER["started"]:
        threading.Thread(target=_sender_loop, daemon=True).start()
        _SENDER["started"] = True
    OUTBOX.put((prio, next(_SEQ), url, body.encode("utf-8"), headers, what))


def flush_outbox(timeout=8):
    """Give queued messages a moment to leave before the process exits."""
    end = time.time() + timeout
    while OUTBOX.unfinished_tasks and time.time() < end:   # includes the one in flight
        time.sleep(0.2)


def send_ntfy(cfg: dict, title: str, body: str, click: str = "", priority: str = "default"):
    topic = cfg.get("ntfy_topic")
    if not topic:
        return
    server = cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    headers = {
        "Title": title.encode("ascii", "ignore").decode(),
        "Priority": priority,
        "Tags": "clapper",
    }
    if click:
        headers["Click"] = click
    headers.update(ntfy_auth(cfg))
    _enqueue(PRIO_ALERT, f"{server}/{topic}", body, headers, f"ALERT '{title[:40]}'")
    LOG.info("ntfy queued: %s", title)


def send_email(cfg: dict, subject: str, body: str):
    to = cfg.get("email_to")
    if not to:
        return
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    if not user or not password:
        LOG.warning("SMTP_USER / SMTP_PASS not set — skipping email")
        return
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = ", ".join(to) if isinstance(to, list) else to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ctx)
                s.login(user, password)
                s.send_message(msg)
        LOG.info("email sent: %s", subject)
    except Exception as e:
        LOG.error("email failed: %s", e)


def alert(cfg: dict, title: str, body: str, click: str = "", loud: bool = True):
    send_ntfy(cfg, title, body, click, "urgent" if loud else "default")
    send_email(cfg, title, body + (f"\n\n{click}" if click else ""))


# ==========================================================================
# Event log
# ==========================================================================

def log_event(path: Path, target: str, ev: dict):
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "target", "type", "movie", "show_time",
                        "screen", "event_code", "session_id", "detail"])
        s = ev.get("show", {})
        w.writerow([datetime.now().isoformat(timespec="seconds"), target, ev["type"],
                    s.get("movie"), s.get("show_time"),
                    s.get("attributes") or s.get("screen"),
                    s.get("event_code"), s.get("session_id"), ev.get("detail")])


# ==========================================================================
# Targets
# ==========================================================================

SEAT_API = ("https://in.bookmyshow.com/api/movies-data/seatlayout/v1/primary"
            "?eventCode={eventCode}&dateCode={dateCode}&regionCode={regionCode}&venueCode={venueCode}")

SEAT_PAGE = ("https://in.bookmyshow.com/movies/movies/seat-layout"
             "/{eventCode}/{venueCode}/{sessionId}/{dateCode}")


def make_filter(target: dict):
    want_screens = [w.upper() for w in target.get("match_screens", [])]
    want_events = [e.upper() for e in target.get("match_events", [])]
    want_sessions = [str(s).strip() for s in target.get("match_sessions", [])]
    want_movies = [m.upper() for m in target.get("match_movies", [])]
    if not (want_screens or want_events or want_sessions or want_movies):
        return None

    def f(s):
        if want_sessions:
            if str(s.get("session_id") or "").strip() not in want_sessions:
                return False
        if want_events:
            if str(s.get("event_code") or "").upper() not in want_events:
                return False
        if want_movies:
            title = str(s.get("movie") or "").upper()
            if not any(m in title for m in want_movies):
                return False
        if want_screens:
            blob = " ".join(str(s.get(k) or "") for k in ("attributes", "screen", "dimension")).upper()
            if not any(w in blob for w in want_screens):
                return False
        return True
    return f


ERROR_MARKERS = (
    "something is not right",
    "something went wrong",
    "please try again later",
    "service unavailable",
    "temporarily unavailable",
    "504 gateway",
    "502 bad gateway",
)

STUCK_MARKERS = (
    "please wait, while we load the seats",
    "please wait while we load",
)


def classify_page(html: str) -> str:
    """
    DOWN    - BookMyShow served an error page
    STUCK   - shell rendered but seats never arrived (you cannot book)
    UP      - page served and not obviously stuck
    """
    if not html:
        return "UNREACHABLE"
    low = html[:200000].lower()
    if any(m in low for m in ERROR_MARKERS):
        return "DOWN"
    if any(m in low for m in STUCK_MARKERS):
        return "STUCK"
    if "seat" in low or "bookmyshow" in low:
        return "UP"
    return "UNKNOWN"


# only this state means "you can actually book right now"
BOOKABLE = "BOOKABLE"
NOT_BOOKABLE = ("DOWN", "STUCK", "UNREACHABLE", "UNKNOWN", "UP")


def check_health(chrome, cfg: dict, target: dict, state: dict, dry: bool):
    """
    Load the seat page like a person would and decide whether seats actually
    render. Alerts only when it becomes genuinely bookable.
    """
    name = target["name"]
    url = target["url"]
    wait_s = int(target.get("render_wait_seconds", 40))

    status, detail = chrome.seat_page_status(url, wait_s,
                                             cinema_url=target.get("cinema_url"),
                                             session_id=target.get("session_id"))

    prev = (state.get(name) or {}).get("status")
    state[name] = {"status": status}
    LOG.info("[%s] %s  (was %s)  %s", name, status, prev or "-", detail)

    if dry or prev is None:
        return

    log_path = Path(cfg.get("event_log", "events.csv"))
    if prev != BOOKABLE and status == BOOKABLE:
        log_event(log_path, name, {"type": "BOOKABLE", "show": {}, "detail": f"{prev} -> {status}: {detail}"})
        alert(cfg,
              "YOU CAN BOOK NOW",
              "The seat map is actually loading — booking should work.\n\n"
              f"{url}\n\nWas: {prev}\nNow: {status}\n{detail}\n"
              f"Detected: {datetime.now():%Y-%m-%d %H:%M:%S}",
              url, loud=True)
    elif prev == BOOKABLE and status != BOOKABLE:
        log_event(log_path, name, {"type": "NOT_BOOKABLE", "show": {}, "detail": f"{prev} -> {status}"})
        LOG.warning("[%s] no longer bookable: %s", name, status)


def page_date(html: str) -> str:
    """
    Which date did BookMyShow actually answer for? The query key looks like
    getShowtimesByVenue-SUDA-20260807, so a mismatch means the site ignored
    our date and served a different one.
    """
    state = extract_initial_state(html)
    if not state:
        return ""
    queries = (state.get("venueShowtimesFunctionalApi") or {}).get("queries") or {}
    key = next((k for k in queries if "getShowtimesByVenue" in k), "")
    m = re.search(r"(\d{8})\s*$", key or "")
    return m.group(1) if m else ""


def parse_date_strip(html: str):
    """
    Return the venue's date strip: [{date_code, disp_date, disabled}, ...]
    A new bookable day shows up either as a new DateCode or as an existing
    one flipping isDisabled from true to false.
    """
    state = extract_initial_state(html)
    if not state:
        return []
    queries = (state.get("venueShowtimesFunctionalApi") or {}).get("queries") or {}
    key = next((k for k in queries if "getShowtimesByVenue" in k), None)
    if not key:
        return []
    data = (queries[key] or {}).get("data") or {}
    out = []
    for e in data.get("ShowDatesArray") or []:
        if not isinstance(e, dict):
            continue
        code = str(e.get("DateCode") or "").strip()
        if not code:
            continue
        out.append({
            "date_code": code,
            "disp_date": e.get("DispDate") or code,
            "disabled": bool(e.get("isDisabled")),
        })
    return out


def check_dates(chrome, cfg: dict, target: dict, state: dict, dry: bool):
    """Alert when the venue opens bookings for a NEW DAY."""
    name = target["name"]
    url = target["url"]

    # keep the URL fresh so it never points at a past date
    if target.get("auto_date", True):
        url = re.sub(r"/(\d{8})(/?)$", f"/{datetime.now():%Y%m%d}\\2", url)

    html = chrome.fetch(url, as_json=False)
    if not html:
        LOG.warning("[%s] no response", name)
        return

    strip = parse_date_strip(html)
    if not strip:
        LOG.warning("[%s] couldn't read the date strip", name)
        return

    enabled = {d["date_code"]: d["disp_date"] for d in strip if not d["disabled"]}
    all_codes = {d["date_code"] for d in strip}

    first_run = name not in state
    prev = state.get(name) or {}
    prev_enabled = set(prev.get("enabled") or [])
    prev_all = set(prev.get("all") or [])

    state[name] = {"enabled": sorted(enabled), "all": sorted(all_codes)}

    furthest = max(enabled) if enabled else "-"
    LOG.info("[%s] %d date(s) bookable, furthest %s", name, len(enabled), furthest)

    if dry or first_run:
        if dry and enabled:
            LOG.info("    bookable: %s", ", ".join(sorted(enabled)))
        return

    new_dates = sorted(set(enabled) - prev_enabled)
    if not new_dates:
        return

    log_path = Path(cfg.get("event_log", "events.csv"))
    for code in new_dates:
        why = "new date added" if code not in prev_all else "date became bookable"
        log_event(log_path, name, {"type": "NEW_DATE", "show": {"show_time": enabled[code]},
                                   "detail": f"{code} — {why}"})

    day_url = re.sub(r"/\d{8}/?$", f"/{new_dates[0]}", url)
    label = enabled[new_dates[0]]
    headline = (f"NEW DAY OPEN: {label}" if len(new_dates) == 1
                else f"NEW DAYS OPEN: {len(new_dates)} dates")
    body = ["Bookings just opened for:", ""]
    body += [f"  {enabled[c]}  ({c})" for c in new_dates]
    body += ["", day_url, "", f"Detected: {datetime.now():%Y-%m-%d %H:%M:%S}"]
    alert(cfg, headline, "\n".join(body), day_url, loud=True)


def resolve_target_date(target: dict):
    """
    date_offset: 0 = today, 1 = tomorrow, ... recomputed every poll so the
    target never points at a date that has passed.
    Returns (url, date_code).
    """
    url = target.get("url", "")
    offset = target.get("date_offset")
    if offset is None:
        return url, target.get("dateCode", "")
    code = f"{datetime.now() + timedelta(days=int(offset)):%Y%m%d}"
    url = re.sub(r"/(\d{8})(/?)$", f"/{code}\\2", url)
    return url, code


META_KEYS = ("__date__", "__alerts__", "__seats__")
SEAT_LAYOUT_PAGE = "https://in.bookmyshow.com/movies/{region}/seat-layout/{event}/{venue}/{session}/{date}"

_seat_chrome = None


def seat_client(client, cfg: dict):
    """Seat maps are decrypted in-page, so they need Chrome even in browserless mode.
    Attach lazily; None (and category-only alerts) if Chrome isn't there."""
    global _seat_chrome
    if getattr(client, "ctx", None) is not None:
        return client
    if _seat_chrome is None:
        try:
            _seat_chrome = Chrome(cfg.get("cdp_port", 9222))
            LOG.info("attached to Chrome for seat maps")
        except SystemExit as e:
            LOG.warning("seat_filter needs Chrome on port %s — falling back to categories. %s",
                        cfg.get("cdp_port", 9222), str(e).strip().splitlines()[0])
            _seat_chrome = False
    return _seat_chrome or None


def seat_layout_url(target: dict, s: dict, date: str) -> str:
    venue = target.get("venueCode")
    region = target.get("regionCode")
    m = re.search(r"/cinemas/([^/]+)/[^/]+/buytickets/([^/]+)/", target.get("url", ""))
    if m:
        region, venue = region or m.group(1), venue or m.group(2)
    return SEAT_LAYOUT_PAGE.format(region=region or "HYD", event=s["event_code"], venue=venue,
                                   session=s["session_id"], date=date)


def send_hold_request(cfg: dict, target: dict, s: dict, date: str):
    """Machine-readable twin of an alert, for seat_holder.py. NEW_SHOW-only
    shows are included too: a freshly listed show is the best time to hold."""
    if not (s.get("session_id") and s.get("event_code")):
        return
    url = seat_layout_url(target, s, date)
    m = re.search(r"/movies/([^/]+)/seat-layout/[^/]+/([^/]+)/", url)
    body = json.dumps({"event": s["event_code"], "venue": m.group(2), "region": m.group(1),
                       "session": str(s["session_id"]), "date": date,
                       "movie": s.get("movie"), "show_time": s.get("show_time")})
    server = cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    headers = {"Title": "hold request", **ntfy_auth(cfg)}
    _enqueue(PRIO_ALERT, f"{server}/{cfg['hold_topic']}", body, headers, "hold request")
    LOG.info("    hold request queued for %s", s.get("show_time"))


def apply_seat_filter(client, cfg, target, shows, events, stored, snapshot, date):
    """
    target["seat_filter"] = {"together": 2, "categories": [...], "rows": [...],
                             "poll": false, "every_seconds": 30}

    Category events for a show are replaced by one SEATS_MATCH event listing the
    free blocks that fit, or dropped if nothing fits. With poll: true, open shows
    are also re-checked every every_seconds and alert when NEW matching seats
    appear (cancellations, expired holds) even if no category status changed.
    NEW_SHOW events always pass through.
    """
    from seat_layout import block_name, live_layout, matching_blocks, parse_layout

    sf = target["seat_filter"]
    n = int(sf.get("together", 2))
    poll = bool(sf.get("poll", False))
    every = int(sf.get("every_seconds", 30))
    f = make_filter(target)
    prev_seen = dict(stored.get("__seats__") or {})
    seen = dict(prev_seen)
    now = time.time()

    by_show = {}
    for ev in events:
        by_show.setdefault(show_key(ev["show"]), []).append(ev)

    out = []
    chrome = None
    for s in shows:
        if f and not f(s):
            continue
        k = show_key(s)
        evs = by_show.pop(k, [])
        out += [e for e in evs if e["type"] == "NEW_SHOW"]
        triggered = any(e["type"] != "NEW_SHOW" and not e.get("quiet") for e in evs)
        is_open = any(v not in CLOSED for v in s["categories"].values())
        due = poll and is_open and now - float((prev_seen.get(k) or {}).get("ts", 0)) >= every
        if not (triggered or due) or not s.get("session_id"):
            out += [e for e in evs if e["type"] != "NEW_SHOW"]
            continue

        chrome = chrome or seat_client(client, cfg)
        text = live_layout(chrome, seat_layout_url(target, s, date)) if chrome else None
        if not text:
            LOG.warning("    %s: seat map unavailable, keeping category alert", s.get("show_time"))
            out += [e for e in evs if e["type"] != "NEW_SHOW"]
            continue

        blocks = matching_blocks(parse_layout(text), n, sf.get("categories", []), sf.get("rows", []))
        ids = sorted({f"{c}:{row}{st['num']}" for c, row, b in blocks for st in b})
        old = set((prev_seen.get(k) or {}).get("ids", []))
        seen[k] = {"ids": ids, "ts": now}
        fresh = [i for i in ids if i not in old]
        names = [f"{c} {block_name(row, b)}" for c, row, b in blocks]
        LOG.info("    %s seats: %d block(s) of %d+ match%s", s.get("show_time"), len(blocks), n,
                 f" ({', '.join(names[:6])})" if names else "")

        if blocks and (triggered or fresh):
            out.append({"type": "SEATS_MATCH", "show": s, "category": "-",
                        "detail": f"{n}+ together: " + ", ".join(names[:12])
                                  + (f" (+{len(names) - 12} more)" if len(names) > 12 else "")})
        elif triggered:
            LOG.info("    %s: category opened but no %d-together seats in wanted rows — not alerting",
                     s.get("show_time"), n)

    for evs in by_show.values():   # events for shows the filter skipped
        out += evs
    snapshot["__seats__"] = seen
    return out


def check_target(chrome: Chrome, cfg: dict, target: dict, state: dict, dry: bool):
    name = target["name"]
    ttype = target.get("type", "seat_api")

    if ttype == "health":
        return check_health(chrome, cfg, target, state, dry)

    if ttype == "date_strip":
        return check_dates(chrome, cfg, target, state, dry)

    if ttype == "cinema_page":
        page_url, resolved_date = resolve_target_date(target)
        html = chrome.fetch(page_url, as_json=False)
        note_fetch(bool(html), cfg)
        if not html:
            return
        served = page_date(html)
        if resolved_date and served and served != resolved_date:
            LOG.warning("[%s] asked for %s but site served %s — not tracking this date",
                        name, resolved_date, served)
            state[name] = {"__date__": resolved_date}
            return
        shows = parse_cinema_page(html)
    else:
        resolved_date, rd = "", ""
        _, rd = resolve_target_date(target)
        resolved_date = rd
        url = SEAT_API.format(eventCode=target["eventCode"],
                              dateCode=rd or target["dateCode"],
                              regionCode=target.get("regionCode", "HYD"),
                              venueCode=target["venueCode"])
        payload = chrome.fetch(url, as_json=True)
        note_fetch(bool(payload), cfg)
        if not payload:
            return
        shows = parse_seat_api(payload)

    if not shows:
        LOG.info("[%s] no shows listed yet", name)
        state.setdefault(name, {})   # mark as seen so shows appearing later alert
        return

    resolved = resolved_date or target.get("dateCode", "")
    stored = state.get(name) or {}
    prev_date = stored.get("__date__")
    first_run = name not in state

    # rolling targets change date at midnight: last night's baseline is not
    # comparable to today's shows, so start clean rather than mis-diff
    if resolved and prev_date and prev_date != resolved:
        LOG.info("[%s] date rolled %s -> %s, resetting baseline", name, prev_date, resolved)
        stored = {}
        first_run = True

    alert_hist = dict(stored.get("__alerts__") or {})
    prev = {k: v for k, v in stored.items() if k not in META_KEYS}
    events, snapshot = diff_shows(
        prev, shows, make_filter(target),
        best_always=cfg.get("best_seats_always_alert", True),
        best_repeat_seconds=int(cfg.get("best_repeat_minutes", 15)) * 60,
        best_baseline_zero=cfg.get("best_baseline_zero", True),
        baseline_zero=cfg.get("baseline_zero", False),
        first_run=first_run)
    snapshot["__date__"] = resolved

    if target.get("seat_filter"):
        events = apply_seat_filter(chrome, cfg, target, shows, events,
                                   {} if first_run else stored, snapshot, resolved)

    # --- cooldown: a flapping category must not alert every few seconds ---
    cooldown = int(cfg.get("alert_cooldown_minutes", 10)) * 60
    now_ts = time.time()
    kept, suppressed = [], 0
    for ev in events:
        key = f"{show_key(ev['show'])}|{ev.get('category', '-')}|{ev['type']}"
        if now_ts - float(alert_hist.get(key, 0)) < cooldown:
            suppressed += 1
            continue
        kept.append(ev)
        if not ev.get("quiet"):
            alert_hist[key] = now_ts
    if suppressed:
        LOG.info("    %d repeat event(s) suppressed (cooldown %dm)",
                 suppressed, cooldown // 60)
    events = kept
    # forget history older than a day so it can't grow forever
    alert_hist = {k: v for k, v in alert_hist.items() if now_ts - float(v) < 86400}
    snapshot["__alerts__"] = alert_hist
    state[name] = snapshot

    cache_note = getattr(chrome, "last_cache", "")
    want = [str(x) for x in (target.get("match_sessions") or [])]
    if want:
        got = [str((s_.get("session_id") or "")) for s_ in shows]
        missing = [x for x in want if x not in got]
        if missing:
            LOG.warning("[%s] requested session(s) %s not present. available: %s",
                        name, ",".join(missing), ",".join(g for g in got if g) or "none")
    n_shows = len([k for k in snapshot if k not in META_KEYS])
    LOG.info("[%s] %d show(s) tracked, %d event(s)  (fetch %dms%s)",
             name, n_shows, len(events), getattr(chrome, "last_fetch_ms", 0),
             (f" cache={cache_note}" if cache_note else "")
             + (f" age={getattr(chrome,'last_age','')}s"
                if getattr(chrome, "last_age", "") else ""))

    if dry:
        allcats = {}
        for s_ in shows:
            for c, nm in (s_.get("category_names") or {}).items():
                allcats[c] = nm
        if allcats:
            LOG.info("    %d distinct categor%s: %s", len(allcats),
                     "y" if len(allcats) == 1 else "ies",
                     ", ".join(f"{nm} [{c}]" for c, nm in sorted(allcats.items(), key=lambda x: x[1])))
        if events:
            LOG.info("    WOULD ALERT on %d event(s):", len(events))
            for ev in events:
                LOG.info("       %-16s %s", ev["type"], ev.get("detail"))
        f = make_filter(target)
        for s in shows:
            if f and not f(s):
                continue
            cats = ", ".join(f"{s['category_names'].get(c, c)}={v}" for c, v in s["categories"].items())
            best = s.get("best_avail", "0")
            cb = s.get("cat_best") or {}
            cb_str = ("  catBest=[" + ", ".join(f"{s['category_names'].get(c, c)}={v}"
                                                for c, v in cb.items()) + "]") if cb else ""
            LOG.info("    %-9s sess=%-6s %-16s avail=%s BEST=%s  [%s]%s",
                     s.get("show_time"), str(s.get("session_id") or "?"),
                     s.get("attributes") or s.get("screen") or "",
                     s["avail"], best, cats, cb_str)
        return

    log_path = Path(cfg.get("event_log", "events.csv"))
    for ev in events:
        log_event(log_path, name, ev)

    if not events:
        return

    loud_events = [e for e in events if not e.get("quiet")]
    for e in events:
        if e.get("quiet"):
            LOG.info("    (quiet) %s", e.get("detail"))
    if not loud_events:
        return
    events = loud_events

    # --- group events by show ---
    by_show = {}
    for ev in events:
        by_show.setdefault(show_key(ev["show"]), []).append(ev)

    movie = events[0]["show"].get("movie")

    # hold requests go out first: seat_holder.py --listen picks them up.
    # auto_hold per target, or for every target via the top-level config key;
    # only for shows that have a category open right now
    if target.get("auto_hold", cfg.get("auto_hold")) and cfg.get("hold_topic"):
        for evs in by_show.values():
            s = evs[0]["show"]
            if any(v not in CLOSED for v in (s.get("categories") or {}).values()):
                send_hold_request(cfg, target, s, resolved)

    def link_for(s):
        # alert_link: "seat" -> deep link to the seat map (default)
        #             "cinema" -> the venue day page, which always renders
        if cfg.get("alert_link", "seat") == "cinema" and ttype == "cinema_page":
            return target["url"]
        if s.get("session_id") and s.get("event_code"):
            return SEAT_PAGE.format(eventCode=s["event_code"],
                                    venueCode=target.get("venueCode", "ALUC"),
                                    sessionId=s["session_id"],
                                    dateCode=target.get("dateCode", ""))
        if ttype == "cinema_page":
            return target["url"]
        return ""

    stamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    mode = cfg.get("alert_grouping", "per_show")
    first_click = ""

    opened_once = False
    if mode == "per_show":
        # one notification per affected show
        for k, evs in by_show.items():
            s = evs[0]["show"]
            is_new = any(e["type"] == "NEW_SHOW" for e in evs)
            is_best = any(e["type"] == "BEST_SEATS" for e in evs)
            label = "NEW SHOW" if is_new else ("BEST SEATS" if is_best else "SEATS OPEN")
            headline = f"{label}: {movie} {s.get('show_time')}"

            lines = [f"{s.get('show_time')}  [{s.get('attributes') or s.get('screen')}]", ""]
            for ev in evs:
                lines.append(f"  - {ev.get('detail')}")
            link = link_for(s)
            first_click = first_click or link
            if link:
                lines += ["", link]
            lines += ["", f"Detected: {stamp}"]

            alert(cfg, headline, "\n".join(lines), link, loud=True)
            if cfg.get("open_page_on_alert") and link and not opened_once:
                try:
                    pg = chrome.ctx.new_page()
                    pg.goto(link, wait_until="domcontentloaded", timeout=30000)
                    LOG.info("    opened seat page in Chrome for you")
                    opened_once = True
                except Exception as e:
                    LOG.warning("couldn't open page: %s", e)
            STATS["events"] += 1
            STATS["last_event"] = f"{datetime.now():%H:%M} {headline[:44]}"
            time.sleep(0.4)   # keep phone notification order sane
    else:
        # single grouped notification for the whole target
        new_shows = [e for e in events if e["type"] == "NEW_SHOW"]
        if new_shows:
            headline = f"NEW SHOW: {movie} - {len(new_shows)} added"
        elif len(by_show) == 1:
            headline = f"SEATS OPEN: {movie} {events[0]['show'].get('show_time')}"
        else:
            headline = f"SEATS OPEN: {movie} - {len(by_show)} shows"

        lines = [f"{len(events)} change(s) across {len(by_show)} show(s)", ""]
        for k, evs in by_show.items():
            s = evs[0]["show"]
            lines.append(f"{s.get('show_time')}  [{s.get('attributes') or s.get('screen')}]")
            for ev in evs:
                lines.append(f"    - {ev.get('detail')}")
            link = link_for(s)
            if link:
                lines.append(f"    {link}")
                first_click = first_click or link
            lines.append("")
        lines.append(f"Detected: {stamp}")

        alert(cfg, headline, "\n".join(lines), first_click, loud=True)

    # Then, optionally, a slower follow-up with seat counts
    if cfg.get("seat_count_on_alert") and first_click:
        counts = chrome.count_seats(first_click)
        LOG.info("    seat count: %s", counts)
        if counts and counts.get("total", 0) > 0:
            send_ntfy(cfg, f"Seat count: {movie}",
                      f"{counts.get('available')} available of {counts.get('total')} seats\n{first_click}",
                      first_click, "default")


# ==========================================================================
# Main
# ==========================================================================

def acquire_lock(path: Path, force: bool = False) -> bool:
    """Stop two live watchers from fighting over the same state file."""
    if path.exists():
        try:
            info = json.loads(path.read_text())
            age = time.time() - info.get("ts", 0)
        except Exception:
            info, age = {}, 1e9
        if age < 300 and not force:
            LOG.error("Another watcher appears to be running (pid %s, %.0fs ago).",
                      info.get("pid"), age)
            LOG.error("Stop it first, or re-run with --force if you're sure it's dead.")
            return False
        LOG.warning("Found stale lock (%.0fs old) — taking over.", age)
    path.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}))
    return True


def refresh_lock(path: Path):
    try:
        path.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}))
    except Exception:
        pass


def release_lock(path: Path):
    try:
        if path.exists():
            info = json.loads(path.read_text())
            if info.get("pid") == os.getpid():
                path.unlink()
    except Exception:
        pass


SEAT_URL_RE = re.compile(
    r"in\.bookmyshow\.com/movies/[^/]+/seat-layout/(ET\d+)/([A-Z0-9]+)/(\d+)/(\d{8})", re.I)
CINEMA_URL_RE = re.compile(
    r"in\.bookmyshow\.com/cinemas/([^/]+)/([^/]+)/buytickets/([A-Z0-9]+)/(\d{8})", re.I)

STATS = {"passes": 0, "events": 0, "started": None, "last_event": None, "paused": False}

# set by the 'stop' command from your phone; also drops a flag file so
# run_watcher.bat won't just restart the process 15 seconds later
SHUTDOWN = threading.Event()
STOP_FLAG = Path("stop.flag")


MIN_INTERVAL = 3   # below this, requests overlap and add load without adding freshness

# consecutive failures -> interval multiplier. Throttling should slow us
# down automatically, not silently kill monitoring.
BACKOFF_STEPS = [(3, 4), (6, 10), (12, 30)]
BACKOFF_CAP = 180


def backoff_factor():
    f = 1
    fails = STATS.get("fails", 0)
    for threshold, mult in BACKOFF_STEPS:
        if fails >= threshold:
            f = mult
    return f


def note_fetch(ok: bool, cfg: dict):
    """Track consecutive failures and announce entering/leaving backoff."""
    if ok:
        if STATS.get("fails", 0) >= BACKOFF_STEPS[0][0]:
            LOG.info("requests recovered — back to normal speed")
            send_status(cfg, "Watcher recovered",
                        "requests are succeeding again, back to full speed")
        STATS["fails"] = 0
        return
    STATS["fails"] = STATS.get("fails", 0) + 1
    n = STATS["fails"]
    if n in [t for t, _ in BACKOFF_STEPS]:
        LOG.warning("%d consecutive fetch failures — slowing to x%d", n, backoff_factor())
        send_status(cfg, "Watcher slowing down",
                    f"{n} failed requests in a row.\n"
                    f"Backing off to x{backoff_factor()} interval.\n"
                    "Still running; will speed up when requests succeed.")


def effective_interval(cfg: dict) -> tuple:
    """
    Normal interval, unless the clock is inside a configured burst window.
    burst_windows: [{"from": "23:45", "to": "00:30", "interval": 5}]
    Backoff multiplies the result when requests are failing.
    """
    base = int(cfg.get("poll_seconds", 40))
    bursting = False
    now = datetime.now().strftime("%H:%M")
    for wdw in cfg.get("burst_windows") or []:
        a, b = str(wdw.get("from", "")), str(wdw.get("to", ""))
        if not a or not b:
            continue
        inside = (a <= now <= b) if a <= b else (now >= a or now <= b)   # handles midnight
        if inside:
            base = int(wdw.get("interval", base))
            bursting = True
            break
    val = max(MIN_INTERVAL, base) * backoff_factor()
    return min(val, BACKOFF_CAP), bursting


def status_topic(cfg):
    return cfg.get("status_topic") or cfg.get("ntfy_topic")


def send_status(cfg, title, body):
    """Status messages go to their own topic so they never bury real alerts."""
    t = status_topic(cfg)
    if not t:
        return
    server = cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    _enqueue(PRIO_STATUS, f"{server}/{t}", body,
             {"Title": title.encode("ascii", "ignore").decode(),
              "Priority": "low", "Tags": "gear", **ntfy_auth(cfg)}, f"status '{title[:30]}'")


def status_text(cfg, targets):
    up = ""
    if STATS["started"]:
        secs = int(time.time() - STATS["started"])
        up = f"{secs // 3600}h {secs % 3600 // 60}m"
    lines = [
        f"RUNNING{' (PAUSED)' if STATS['paused'] else ''}",
        f"uptime   : {up}",
        f"passes   : {STATS['passes']}",
        f"alerts   : {STATS['events']}",
        f"last     : {STATS['last_event'] or 'none yet'}",
        f"interval : {effective_interval(cfg)[0]}s"
        + (f"  (BACKOFF x{backoff_factor()}, {STATS.get('fails',0)} fails)"
           if backoff_factor() > 1 else ""),
        "",
        f"{len(targets)} target(s):",
    ]
    for t in targets:
        extra = ""
        if t.get("match_sessions"):
            extra = f" sessions {','.join(t['match_sessions'])}"
        lines.append(f"  - {t['name']}{extra}")
    site_db = Path(__file__).resolve().parent / "bms_server.db"
    if site_db.exists():
        try:
            c = sqlite3.connect(site_db)
            try:
                lines.append(f"website watches : {c.execute('SELECT COUNT(*) FROM subs').fetchone()[0]}")
            finally:
                c.close()
        except sqlite3.Error:
            lines.append("website watches : unavailable")
    return "\n".join(lines)


def target_from_url(url: str, cfg: dict):
    """Build a watch target from a BookMyShow URL. Returns (target, note) or (None, why)."""
    m = SEAT_URL_RE.search(url)
    if m:
        ev, venue, sess, date = m.group(1).upper(), m.group(2).upper(), m.group(3), m.group(4)
        return ({
            "name": f"{ev} {venue} {date}",
            "type": "seat_api", "eventCode": ev, "dateCode": date,
            "venueCode": venue, "regionCode": cfg.get("default_region", "HYD"),
            "match_sessions": [sess],
        }, f"session {sess}")
    m = CINEMA_URL_RE.search(url)
    if m:
        region, slug, venue, date = m.group(1).upper(), m.group(2), m.group(3).upper(), m.group(4)
        return ({
            "name": f"{venue} {date} (all shows)",
            "type": "cinema_page", "url": url.split("?")[0],
            "venueCode": venue, "dateCode": date,
        }, "whole day")
    return None, "not a BookMyShow seat-layout or cinema URL"


def apply_command(text: str, cfg: dict, lock, cfg_path: Path):
    """
    Handle one remote command. Deliberately narrow: BookMyShow URLs and a
    handful of fixed keywords. Nothing here executes arbitrary input.
    """
    text = (text or "").strip()
    low = text.lower()

    if low in ("health", "status", "?", "ping"):
        with lock:
            return status_text(cfg, cfg["targets"])

    if low in ("stop", "shutdown", "quit", "kill", "off", "turn off"):
        try:
            STOP_FLAG.write_text(f"stopped from phone {datetime.now():%Y-%m-%d %H:%M:%S}")
        except Exception:
            pass
        SHUTDOWN.set()
        return ("SHUTTING DOWN — the watcher is stopping and will NOT auto-restart.\n"
                "To run it again, start run_watcher.bat on the PC.\n\n"
                "Tip: 'pause' stops alerts but keeps it listening, so you can 'resume' "
                "from here.")

    if low in ("pause", "stop watching"):
        STATS["paused"] = True
        return "PAUSED — send 'resume' to continue"

    if low == "resume":
        STATS["paused"] = False
        return "RESUMED"

    if low in ("list", "targets"):
        with lock:
            if not cfg["targets"]:
                return "no targets"
            return "\n".join(f"{i}. {t['name']}" for i, t in enumerate(cfg["targets"]))

    if low == "clear all":
        with lock:
            n = len(cfg["targets"])
            cfg_path.write_text(json.dumps({**cfg, "targets": []}, indent=2))
            cfg["targets"] = []
            STATS["clear_count"] = STATS.get("clear_count", 0) + 1
            site_db = Path(__file__).resolve().parent / "bms_server.db"
            web = 0
            if site_db.exists():
                try:
                    c = sqlite3.connect(site_db)
                    try:
                        with c:
                            web = c.execute("SELECT COUNT(*) FROM subs").fetchone()[0]
                            c.execute("DELETE FROM subs")
                            c.execute("DELETE FROM snapshots")
                    finally:
                        c.close()
                except sqlite3.Error as e:
                    return (f"cleared {n} bot target(s); website watches could not be cleared: {e}")
            return (f"cleared {n} bot target(s) and {web} website watch(es). "
                    "Both listeners remain running; new watches start automatically.")

    if low in ("clear", "remove all", "removeall"):
        with lock:
            n = len(cfg["targets"])
            cfg_path.write_text(json.dumps({**cfg, "targets": []}, indent=2))
            cfg["targets"] = []
            STATS["clear_count"] = STATS.get("clear_count", 0) + 1
            return f"cleared {n} bot target(s); command listener remains active"

    if low.startswith("remove "):
        try:
            i = int(low.split()[1])
        except (ValueError, IndexError):
            return "usage: remove <number>  (see 'list')"
        with lock:
            if 0 <= i < len(cfg["targets"]):
                gone = cfg["targets"].pop(i)
                cfg_path.write_text(json.dumps(cfg, indent=2))
                return f"removed: {gone['name']}"
            return f"no target {i}"

    if "bookmyshow.com" in low:
        tgt, note = target_from_url(text, cfg)
        if not tgt:
            return f"couldn't read that URL — {note}"
        with lock:
            for ex in cfg["targets"]:
                same = (ex.get("type") == tgt["type"] and
                        ex.get("eventCode") == tgt.get("eventCode") and
                        ex.get("dateCode") == tgt.get("dateCode") and
                        ex.get("venueCode") == tgt.get("venueCode"))
                if same and tgt.get("match_sessions"):
                    for s in tgt["match_sessions"]:
                        if s not in ex.setdefault("match_sessions", []):
                            ex["match_sessions"].append(s)
                    cfg_path.write_text(json.dumps(cfg, indent=2))
                    return f"added {note} to existing target\n{ex['name']}\nnow: {','.join(ex['match_sessions'])}"
                if same:
                    return f"already watching {ex['name']}"
            cfg["targets"].append(tgt)
            cfg_path.write_text(json.dumps(cfg, indent=2))
            warn = ("\n\nNOTE: seat-layout links use an endpoint BookMyShow "
                    "caches for minutes. For live data send the cinema "
                    "'buytickets' page link instead."
                    if tgt["type"] == "seat_api" else "")
            return (f"NOW WATCHING\n{tgt['name']}\n{note}\n\n"
                    f"{len(cfg['targets'])} target(s) total{warn}")

    return ("commands:\n"
            "  <paste a BookMyShow link>  - watch it\n"
            "  health / status / list\n"
            "  remove <number> / clear (bot only)\n"
            "  clear all  - bot and website watches\n"
            "  pause / resume\n"
            "  stop  - shut it down completely")


def command_listener(cfg, lock, cfg_path, stop_evt):
    """
    Listen for phone commands over ONE long-lived streaming connection.

    The previous version polled every 5 seconds -- about 17,000 requests a day
    -- which drained ntfy.sh's per-connection allowance and got every push from
    this machine refused, alerts included. Streaming costs one request per
    reconnect; ntfy pushes messages down the open connection as they arrive.
    """
    topic = cfg.get("command_topic")
    if not topic:
        return
    server = cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    last_id = None
    backoff = 5
    LOG.info("listening for commands on ntfy topic %r (streaming, %s)", topic,
             "authenticated" if ntfy_auth(cfg) else "ANONYMOUS - free limits apply")
    while not stop_evt.is_set():
        params = {"since": last_id} if last_id else {}
        try:
            with requests.get(f"{server}/{topic}/json", params=params, headers=ntfy_auth(cfg),
                              stream=True, timeout=(15, 150)) as r:
                if r.status_code == 429:
                    LOG.warning("command stream rate limited by ntfy; retrying in %ss", backoff)
                    stop_evt.wait(backoff)
                    backoff = min(backoff * 2, 900)
                    continue
                r.raise_for_status()
                backoff = 5
                for line in r.iter_lines(decode_unicode=True):
                    if stop_evt.is_set():
                        break
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("event") != "message":
                        continue          # open / keepalive
                    last_id = msg.get("id") or last_id
                    body = msg.get("message", "")
                    LOG.info("command received: %r", body[:90])
                    reply = apply_command(body, cfg, lock, cfg_path)
                    LOG.info("  -> %s", (reply or "").replace("\n", " | ")[:150])
                    send_status(cfg, "Watcher", reply)
        except Exception as e:
            LOG.debug("command stream dropped: %s", e)
        stop_evt.wait(backoff)


PANEL_HTML = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Watcher</title><style>
:root{--house:#160f1c;--seat:#211829;--edge:#3a2c46;--marquee:#ffb63d;
      --open:#5fd08a;--dim:#9c8fab;--ink:#f3eef7;--bad:#ff6b6b}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--house);color:var(--ink);
  font:16px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  padding:max(20px,env(safe-area-inset-top)) 18px 40px}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace}
h1{font-size:12px;letter-spacing:.4em;text-transform:uppercase;color:var(--marquee);margin:0 0 18px}
h2{font-size:10px;letter-spacing:.3em;text-transform:uppercase;color:var(--dim);margin:28px 0 10px}
.card{background:var(--seat);border:1px solid var(--edge);border-radius:10px;padding:16px;margin-bottom:12px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px}
.live{background:var(--open);box-shadow:0 0 10px var(--open)}
.hold{background:var(--marquee)}
.big{font-size:26px;margin:2px 0 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.k{font-size:10px;letter-spacing:.2em;text-transform:uppercase;color:var(--dim)}
.stub{position:relative;background:var(--seat);border:1px solid var(--edge);
  border-radius:10px;padding:14px 84px 14px 16px;margin-bottom:10px}
.stub:before{content:"";position:absolute;top:0;bottom:0;right:70px;width:1px;
  background:repeating-linear-gradient(180deg,var(--edge) 0 5px,transparent 5px 10px)}
.stub .v{font-size:10px;letter-spacing:.24em;text-transform:uppercase;color:var(--marquee)}
.stub .d{font-size:17px;margin-top:3px}
.stub .f{color:var(--dim);font-size:13px;margin-top:2px}
.x{position:absolute;right:12px;top:50%;transform:translateY(-50%)}
input{width:100%;padding:13px;background:var(--house);color:var(--ink);
  border:1px solid var(--edge);border-radius:8px;font-size:16px;margin-bottom:8px}
input:focus{outline:2px solid var(--marquee);outline-offset:1px}
button{padding:13px 18px;border:0;border-radius:8px;font-size:15px;font-weight:700;
  background:var(--marquee);color:#241703}
button.g{background:transparent;color:var(--dim);border:1px solid var(--edge);font-weight:600;padding:9px 14px;font-size:13px}
button.r{background:transparent;color:var(--bad);border:1px solid var(--bad);font-weight:600}
.row{display:flex;gap:8px;flex-wrap:wrap}
.ev{font-size:13px;color:var(--dim);padding:7px 0;border-bottom:1px solid var(--edge)}
.ev b{color:var(--ink);font-weight:600}
#msg{font-size:13px;color:var(--open);min-height:18px;margin-top:6px}
</style></head><body>
<h1>Watcher</h1>
<div class="card">
  <div><span id="dot" class="dot live"></span><span id="state" class="mono">connecting…</span></div>
  <div class="big mono" id="uptime">—</div>
  <div class="grid" style="margin-top:14px">
    <div><div class="k">Checks</div><div class="mono" id="passes">—</div></div>
    <div><div class="k">Alerts</div><div class="mono" id="alerts">—</div></div>
  </div>
</div>

<h2>Watching</h2><div id="targets"></div>

<h2>Add</h2>
<div class="card">
  <input id="url" placeholder="paste a BookMyShow cinema link" autocapitalize="off" autocorrect="off" spellcheck="false">
  <button onclick="send($('url').value)">Watch it</button>
  <div id="msg"></div>
</div>

<h2>Control</h2>
<div class="row" style="margin-bottom:14px">
  <button class="g" onclick="send('pause')">Pause</button>
  <button class="g" onclick="send('resume')">Resume</button>
  <button class="g" onclick="load()">Refresh</button>
  <button class="r" onclick="if(confirm('Remove all bot and website watches?'))send('clear all')">Clear all watches</button>
  <button class="r" onclick="if(confirm('Stop the watcher? It will not restart on its own.'))send('stop')">Stop</button>
</div>

<h2>Recent</h2><div id="events" class="card"></div>

<script>
const $=i=>document.getElementById(i);
async function load(){
  try{
    const s=await (await fetch('/api/state')).json();
    $('state').textContent = s.paused? 'PAUSED' : 'RUNNING · '+s.interval+'s';
    $('dot').className='dot '+(s.paused?'hold':'live');
    $('uptime').textContent=s.uptime;
    $('passes').textContent=s.passes; $('alerts').textContent=s.alerts;
    $('targets').innerHTML = s.targets.length ? s.targets.map((t,i)=>`
      <div class="stub"><div class="v">${t.venue}</div>
        <div class="d mono">${t.date}</div>
        <div class="f">${t.filters}</div>
        <div class="x"><button class="g" onclick="send('remove ${i}')">Stop</button></div>
      </div>`).join('') : '<div class="card" style="color:var(--dim)">nothing being watched</div>';
    $('events').innerHTML = s.events.length
      ? s.events.map(e=>`<div class="ev"><b>${e.t}</b> ${e.d}</div>`).join('')
      : '<div style="color:var(--dim);font-size:13px">no changes yet</div>';
  }catch(e){ $('state').textContent='watcher not reachable'; $('dot').className='dot'; }
}
async function send(text){
  if(!text) return;
  $('msg').textContent='…';
  const r=await fetch('/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:text})});
  const d=await r.json();
  $('msg').textContent=(d.reply||'').split('\n')[0];
  $('url').value='';
  load();
}
load(); setInterval(load,5000);
</script></body></html>"""


def start_panel(cfg, lock, cfg_path, port):
    """Small local web UI, so a phone can drive the watcher from Safari."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def recent_events(n=12):
        path = Path(cfg.get("event_log", "events.csv"))
        if not path.exists():
            return []
        try:
            rows = path.read_text(encoding="utf-8").splitlines()[1:][-n:]
        except Exception:
            return []
        out = []
        for line in reversed(rows):
            parts = next(csv.reader([line]), [])
            if len(parts) >= 9:
                out.append({"t": parts[0][11:16], "d": f"{parts[4] or ''} {parts[8]}"[:70]})
        return out

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _s(self, code, body, ctype="application/json"):
            b = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/":
                return self._s(200, PANEL_HTML, "text/html; charset=utf-8")
            if self.path == "/api/state":
                secs = int(time.time() - (STATS["started"] or time.time()))
                with lock:
                    tg = []
                    for t in cfg["targets"]:
                        try:
                            d = datetime.strptime(t.get("dateCode", ""), "%Y%m%d").strftime("%a %d %b")
                        except Exception:
                            d = t.get("dateCode", "")
                        f = " · ".join(filter(None, [
                            ",".join(t.get("match_screens") or []) or "all screens",
                            ",".join(t.get("match_movies") or []) or "all movies"]))
                        tg.append({"venue": t.get("venueCode", "?"), "date": d, "filters": f})
                return self._s(200, json.dumps({
                    "uptime": f"{secs // 3600}h {secs % 3600 // 60}m",
                    "passes": STATS["passes"], "alerts": STATS["events"],
                    "paused": STATS["paused"], "interval": effective_interval(cfg)[0],
                    "targets": tg, "events": recent_events()}))
            return self._s(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            if self.path != "/api/cmd":
                return self._s(404, json.dumps({"error": "not found"}))
            try:
                n = int(self.headers.get("Content-Length") or 0)
                text = (json.loads(self.rfile.read(n) or "{}").get("text") or "").strip()
            except Exception:
                return self._s(400, json.dumps({"error": "bad request"}))
            reply = apply_command(text, cfg, lock, cfg_path)
            LOG.info("panel: %r -> %s", text[:60], (reply or "").replace("\n", " | ")[:90])
            return self._s(200, json.dumps({"reply": reply}))

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    LOG.info("control panel on http://<this-pc-ip>:%d", port)


def run(cfg: dict, args):
    state_path = Path(cfg.get("state_file", "watch_state.json"))
    lock_path = state_path.with_suffix(".lock")

    # dry runs are read-only: no lock needed, no state written
    if not args.dry_run:
        if not acquire_lock(lock_path, getattr(args, "force", False)):
            return

    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            LOG.warning("state file unreadable, starting fresh")

    chrome = make_client(cfg)

    stop = threading.Event()
    tlock = threading.RLock()
    cfg_path = Path(getattr(args, "config", "watch_config.json"))
    try:
        if STOP_FLAG.exists():
            STOP_FLAG.unlink()
    except Exception:
        pass
    STATS["started"] = time.time()
    STATS["passes"] = 0
    STATS["events"] = 0
    STATS["paused"] = False

    if not args.dry_run:
        threading.Thread(target=command_listener,
                         args=(cfg, tlock, cfg_path, stop), daemon=True).start()
        if cfg.get("panel_port"):
            try:
                start_panel(cfg, tlock, cfg_path, int(cfg["panel_port"]))
            except Exception as e:
                LOG.warning("couldn't start control panel: %s", e)
        send_status(cfg, "Watcher started", status_text(cfg, cfg["targets"]))
    hb_every = int(cfg.get("heartbeat_minutes", 0)) * 60
    next_hb = time.time() + hb_every

    def waiter():
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            # no console attached (scheduled task / background launcher):
            # never stop just because stdin is closed
            if getattr(args, "daemon", False):
                while not stop.is_set():
                    time.sleep(3600)
                return
        stop.set()

    if not args.dry_run:
        threading.Thread(target=waiter, daemon=True).start()
        LOG.info("running — %s",
                 "daemon mode, control from your phone" if getattr(args, "daemon", False)
                 else "press Enter to stop")

    interval = cfg.get("poll_seconds", 90)
    jitter = cfg.get("jitter_seconds", 15)
    due = {}          # target name -> next timestamp it should run
    last_clear_count = STATS.get("clear_count", 0)

    burst_note = [False]

    def target_interval(t):
        eff, bursting = effective_interval(cfg)
        burst_note[0] = bursting
        return int(t.get("interval_seconds", eff))

    LOG.info("polling: %s", ", ".join(
        f"{t['name'][:22]}={target_interval(t)}s" for t in cfg["targets"]))

    try:
        while not stop.is_set():
            if SHUTDOWN.is_set():
                LOG.info("shutdown requested from phone")
                stop.set()
                break

            if STATS["paused"]:
                time.sleep(1)
                continue

            now_ts = time.time()
            ran = []
            with tlock:
                snapshot_targets = list(cfg["targets"])
                cleared = STATS.get("clear_count", 0)
            if cleared != last_clear_count:
                due.clear()
                last_clear_count = cleared
            for target in snapshot_targets:
                if stop.is_set():
                    break
                with tlock:
                    if target not in cfg["targets"]:
                        continue
                tname = target.get("name")
                if due.get(tname, 0) > now_ts:
                    continue
                try:
                    check_target(chrome, cfg, target, state, args.dry_run)
                except Exception:
                    LOG.exception("[%s] check failed", tname)
                ran.append(target)
                time.sleep(random.uniform(0.8, 2.0))

            # reschedule from the END of the pass, with ONE jitter value for
            # the whole batch, so same-interval targets stay in lockstep
            if ran:
                STATS["passes"] += 1
            if hb_every and time.time() >= next_hb and not args.dry_run:
                send_status(cfg, "Watcher alive", status_text(cfg, snapshot_targets))
                next_hb = time.time() + hb_every

            finished = time.time()
            batch_jitter = random.uniform(0, jitter)
            for target in ran:
                due[target["name"]] = finished + target_interval(target) + batch_jitter

            if ran and not args.dry_run:
                state_path.write_text(json.dumps(state, indent=2))
                refresh_lock(lock_path)
            if args.dry_run:
                LOG.info("dry run complete — state file NOT modified")
                break

            nxt = min(due.values()) if due else time.time() + interval
            wait = max(1.0, nxt - time.time())
            if ran:
                LOG.info("next check in %.0fs%s", wait, "  [BURST]" if burst_note[0] else "")
            slept = 0.0
            while slept < wait and not stop.is_set():
                time.sleep(0.5)
                slept += 0.5
    finally:
        if not args.dry_run:
            state_path.write_text(json.dumps(state, indent=2))
        release_lock(lock_path)
        chrome.close()
        if not args.dry_run:
            send_status(cfg, "Watcher STOPPED",
                        f"no longer monitoring\npasses: {STATS['passes']}  alerts: {STATS['events']}")
            flush_outbox()
        LOG.info("stopped")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="watch_config.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="one cycle, print state, no alerts, does NOT write state file")
    ap.add_argument("--force", action="store_true", help="ignore an existing lock file")
    ap.add_argument("--daemon", action="store_true",
                    help="run without a console; ignore Enter-to-stop (use Ctrl+C or the phone)")
    ap.add_argument("--test-alert", action="store_true", help="send a test notification")
    ap.add_argument("--test-count", action="store_true", help="test seat counting")
    ap.add_argument("--url", help="url for --test-count")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler("watch.log", encoding="utf-8")])

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"Config not found: {cfg_path}")
        return 1
    cfg = json.loads(cfg_path.read_text())

    if args.test_alert:
        alert(cfg, "BMS watcher test", "If you got this on your phone and by email, alerts work.",
              "https://in.bookmyshow.com/", loud=False)
        print("Test sent.")
        return 0

    if args.test_count:
        if not args.url:
            print("--test-count needs --url")
            return 1
        chrome = make_client(cfg)
        try:
            print(json.dumps(chrome.count_seats(args.url), indent=2))
        finally:
            chrome.close()
        return 0

    run(cfg, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
