#!/usr/bin/env python3
"""
bms_server.py. pick a cinema, pick a day, choose what to watch.

No pasting URLs. Choose a venue from a list, pick a date, and the server loads
that day's actual shows so you can watch one showtime, a whole screen (DOLBY,
HDR BY BARCO), or the entire cinema.

One poller for everyone: subscriptions are grouped by page, so ten people
watching the same cinema and date cost a single request.

Needs: pip install requests
Reuses the parser from bms_seat_watch.py (same folder).

    python bms_server.py --host 0.0.0.0
"""

import argparse
import base64
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

sys.path.insert(0, str(Path(__file__).parent))
from bms_seat_watch import (  # noqa: E402
    parse_cinema_page, diff_shows, page_date, extract_initial_state,
    SEAT_PAGE, HttpClient,
)

DB = Path("bms_server.db")
LOCK = threading.RLock()
POLL_SECONDS = 30
STATE = {"started": time.time(), "passes": 0, "alerts": 0}
CLIENT = None
MOVIE_CACHE = {"at": 0, "items": []}


def tracker_panel_url():
    """Use only the co-located tracker's loopback control panel."""
    try:
        cfg = json.loads((Path(__file__).resolve().parent / "watch_config.json").read_text())
        port = int(cfg.get("panel_port", 8787))
        if not 1 <= port <= 65535:
            raise ValueError("invalid panel port")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        port = 8787
    return f"http://127.0.0.1:{port}"


def tracker_state():
    try:
        return requests.get(tracker_panel_url() + "/api/state", timeout=2).json()
    except (requests.RequestException, ValueError):
        return {"error": "Local tracker control panel is offline."}

CINEMA_URL = "https://in.bookmyshow.com/cinemas/{region}/{slug}/buytickets/{code}/{date}"

# enough to be useful on day one; the catalogue grows via Import
SEED = [
    ("ALUC", "ALLU Cinemas: Kokapet", "HYD"),
    ("AMBH", "AMB Cinemas: Gachibowli", "HYD"),
    ("SUDA", "Sudarshan 35MM 4k Laser & Dolby Atmos: RTC X Roads", "HYD"),
]


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def venue_url(code, name, region, date):
    return CINEMA_URL.format(region=region or "HYD", slug=slugify(name),
                             code=code, date=date)


# ---------------------------------------------------------------- storage
def db():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS venues (
            code TEXT PRIMARY KEY, name TEXT, region TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS subs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL, venue TEXT, venue_name TEXT, region TEXT,
            date_code TEXT, screen TEXT, movie TEXT, session TEXT,
            url TEXT, created TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS snapshots (
            url TEXT PRIMARY KEY, data TEXT, updated TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS blocked (topic TEXT PRIMARY KEY, added TEXT)""")
        # saved preferences, one row per visitor name (never the ntfy topic: a
        # name is easy to type, and the topic is what guards someone's watches)
        c.execute("""CREATE TABLE IF NOT EXISTS prefs (name TEXT PRIMARY KEY, data TEXT, updated TEXT)""")
        # "track a movie everywhere": a movie (on one date, or any date) in chosen
        # areas / formats; state remembers which dates and cinemas were already seen
        c.execute("""CREATE TABLE IF NOT EXISTS mwatch (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, topic TEXT, code TEXT, title TEXT,
            slug TEXT, date TEXT, areas TEXT, formats TEXT, created TEXT, state TEXT, token TEXT)""")
        # every alert a watch sent ("GOLD went 0 -> 2"), for its History list
        c.execute("""CREATE TABLE IF NOT EXISTS alert_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sub_id INTEGER, at TEXT, kind TEXT, detail TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS alert_log_sub ON alert_log(sub_id)")
        # the website's side of the admin activity log (the holder's side is holds.json)
        c.execute("""CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, who TEXT, kind TEXT, detail TEXT, sub_id INTEGER)""")
        # group booking: friends join one watch's auto-hold, so the holder finds
        # seats together for everyone. The organiser pays BookMyShow (their own UPI
        # QR, as for any hold); friends pay the organiser back their share.
        c.execute("""CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sub_id INTEGER UNIQUE, code TEXT UNIQUE,
            organizer TEXT, seats INTEGER, upi TEXT, created TEXT, notified TEXT DEFAULT '')""")
        c.execute("""CREATE TABLE IF NOT EXISTS group_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, name TEXT, seats INTEGER,
            topic TEXT, joined TEXT, paid INTEGER DEFAULT 0, UNIQUE(group_id, name))""")
        # a friend's own holder: their PC, their Chrome, their BookMyShow account.
        # Hold requests for their watches go to its private ntfy topic; it reports
        # back with a key (heartbeat + each hold result -> remote_ledger)
        c.execute("""CREATE TABLE IF NOT EXISTS personal_holders (
            owner TEXT PRIMARY KEY, topic TEXT, key TEXT, notify TEXT, created TEXT,
            last_beat REAL DEFAULT 0, chrome INTEGER DEFAULT 0, armed INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS remote_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sub_id INTEGER, owner TEXT, data TEXT, at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS visitors (
            name TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT, visits INTEGER DEFAULT 1)""")
        # movie posters found off BookMyShow: url '' = looked, nothing sure enough
        c.execute("""CREATE TABLE IF NOT EXISTS posters (title TEXT PRIMARY KEY, url TEXT, source TEXT, at REAL)""")
        # expected release dates for "Coming soon" (never from BookMyShow): '' = not known
        c.execute("""CREATE TABLE IF NOT EXISTS releases (title TEXT PRIMARY KEY, day TEXT, source TEXT, at REAL)""")
        # the front page's "Just opened" strip: what opened where, never who was watching
        c.execute("""CREATE TABLE IF NOT EXISTS openings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, kind TEXT, what TEXT, venue TEXT, venue_name TEXT,
            date_code TEXT, movie TEXT, show_time TEXT, movie_code TEXT, key TEXT)""")
        # auto-hold requests ride on the watch they're for:
        #   hold_status  '' | pending | approved | rejected | triggered
        #   hold_opts    {"qty", "categories", "rows", "max_total"} as JSON
        #   hold_code    one-time secret behind the Approve/Reject buttons in the ntfy alert
        have = {r["name"] for r in c.execute("PRAGMA table_info(subs)").fetchall()}
        for col in ("hold_status", "hold_opts", "hold_code", "hold_by", "hold_at"):
            if col not in have:
                c.execute(f"ALTER TABLE subs ADD COLUMN {col} TEXT DEFAULT ''")
        # owner: the visitor who made the watch (knowing the topic alone isn't enough
        #        to see or change it). '' = made before owners existed; the first
        #        visitor to open it with the right topic claims it.
        # token: secret behind the "Stop watching" button in that watch's alerts
        # seat-level alerts (single-showtime watches): seat_filter {"together",
        # "categories", "rows"}; seat_seen = the matching seat ids last seen
        for col in ("owner", "token", "seat_filter", "seat_seen", "ip"):
            if col not in have:
                c.execute(f"ALTER TABLE subs ADD COLUMN {col} TEXT DEFAULT ''")
        if "ip" not in {r["name"] for r in c.execute("PRAGMA table_info(mwatch)").fetchall()}:
            c.execute("ALTER TABLE mwatch ADD COLUMN ip TEXT DEFAULT ''")
        for r in c.execute("SELECT id FROM subs WHERE IFNULL(token,'')=''").fetchall():
            c.execute("UPDATE subs SET token=? WHERE id=?", (secrets.token_urlsafe(12), r["id"]))
        if not c.execute("SELECT v FROM settings WHERE k='admin_token'").fetchone():
            c.execute("INSERT INTO settings VALUES ('admin_token', ?)",
                      (secrets.token_urlsafe(18),))
        for k, v in (("paused", "0"), ("max_subs", "25"), ("interval", "10")):
            if not c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone():
                c.execute("INSERT INTO settings VALUES (?,?)", (k, v))
        for code, name, region in SEED:
            c.execute("INSERT OR IGNORE INTO venues VALUES (?,?,?)", (code, name, region))


def setting(k, default=""):
    with db() as c:
        r = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def set_setting(k, v):
    with LOCK, db() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (k, str(v)))


def is_admin(handler):
    """Admin key from ?key= or the cookie set on first successful visit."""
    token = setting("admin_token")
    if not token:
        return False
    q = parse_qs(urlparse(handler.path).query)
    given = (q.get("key") or [""])[0]
    if not given:
        for part in (handler.headers.get("Cookie") or "").split(";"):
            if part.strip().startswith("adm="):
                given = part.strip()[4:]
                break
    return bool(given) and hmac.compare_digest(given, token)


def access_token(name):
    raw = base64.urlsafe_b64encode(name.encode("utf-8")).decode().rstrip("=")
    sig = hmac.new(setting("admin_token").encode(), raw.encode(), "sha256").hexdigest()
    return f"{raw}.{sig}"


def visitor_name(handler):
    token = ""
    for part in (handler.headers.get("Cookie") or "").split(";"):
        if part.strip().startswith("visitor="):
            token = part.strip()[8:]
            break
    try:
        raw, sig = token.rsplit(".", 1)
        expected = hmac.new(setting("admin_token").encode(), raw.encode(), "sha256").hexdigest()
        if not hmac.compare_digest(sig, expected):
            return ""
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    except Exception:
        return ""


def remember_visitor(name):
    now = datetime.now().isoformat(timespec="seconds")
    with LOCK, db() as c:
        row = c.execute("SELECT visits FROM visitors WHERE name=?", (name,)).fetchone()
        if row:
            c.execute("UPDATE visitors SET last_seen=?, visits=? WHERE name=?",
                      (now, int(row["visits"] or 0) + 1, name))
        else:
            c.execute("INSERT INTO visitors VALUES (?,?,?,1)", (name, now, now))


HISTORY_KEEP = 30          # alerts remembered per watch


def log_alert(sub_id, kind, detail):
    with LOCK, db() as c:
        c.execute("INSERT INTO alert_log (sub_id, at, kind, detail) VALUES (?,?,?,?)",
                  (sub_id, datetime.now().isoformat(timespec="seconds"), kind, str(detail)[:200]))
        c.execute("DELETE FROM alert_log WHERE sub_id=? AND id NOT IN "
                  "(SELECT id FROM alert_log WHERE sub_id=? ORDER BY id DESC LIMIT ?)",
                  (sub_id, sub_id, HISTORY_KEEP))


def watch_history(sub_id, limit=HISTORY_KEEP):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT at, kind, detail FROM alert_log WHERE sub_id=? "
                                           "ORDER BY id DESC LIMIT ?", (sub_id, limit)).fetchall()]


def log_activity(kind, who, detail, sub_id=None):
    """Admin activity log: requests, approvals, holds sent, tracker on/off asks."""
    with LOCK, db() as c:
        c.execute("INSERT INTO activity (at, who, kind, detail, sub_id) VALUES (?,?,?,?,?)",
                  (datetime.now().isoformat(timespec="seconds"), who or "", kind, str(detail)[:300], sub_id))
        c.execute("DELETE FROM activity WHERE id NOT IN (SELECT id FROM activity ORDER BY id DESC LIMIT 500)")


OPEN_KINDS = {"NEW_SHOW", "NEW_CATEGORY", "CATEGORY_OPENED", "SHOW_OPENED"}


def log_opening(kind, what, venue="", venue_name="", date="", movie="", show_time="", movie_code="", key=""):
    """One line for the "Just opened" strip. Built from checks the poller already
    makes, so it costs nothing extra on BookMyShow."""
    now = datetime.now()
    with LOCK, db() as c:
        if key and c.execute("SELECT 1 FROM openings WHERE key=? AND at>=?",
                             (key, (now - timedelta(minutes=30)).isoformat(timespec="seconds"))).fetchone():
            return                              # open, closed, open again: once per half hour
        c.execute("INSERT INTO openings (at, kind, what, venue, venue_name, date_code, movie, show_time, "
                  "movie_code, key) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (now.isoformat(timespec="seconds"), kind, what[:80], venue or "", venue_name or "",
                   date or "", (movie or "")[:80], (show_time or "")[:40], movie_code or "", key))
        c.execute("DELETE FROM openings WHERE id NOT IN (SELECT id FROM openings ORDER BY id DESC LIMIT 400)")


def log_page_openings(watchers, date, events):
    """A cinema-day page's changes -> openings (a new date's shows become one line per movie)."""
    venue = watchers[0]["venue"]
    vname = watchers[0].get("venue_name") or venue
    new, n = {}, 0
    for e in events:
        kind, sh = e.get("type"), e["show"]
        if kind not in OPEN_KINDS:
            continue
        movie = sh.get("movie") or ""
        if kind == "NEW_SHOW":
            new.setdefault(movie, []).append(sh)
            continue
        if n >= 6:
            continue
        n += 1
        cat = e.get("category") or ""
        cname = (sh.get("category_names") or {}).get(cat, cat)
        try:
            price = int(float((sh.get("category_prices") or {}).get(cat) or 0))
        except (TypeError, ValueError):
            price = 0
        what = {"CATEGORY_OPENED": f"{cname} open", "NEW_CATEGORY": f"{cname} released",
                "SHOW_OPENED": "Bookings open"}[kind] + (f" · ₹{price}" if price else "")
        log_opening(kind, what, venue, vname, date, movie, sh.get("show_time"), sh.get("event_code"),
                    key=f"{venue}|{date}|{sh.get('session_id')}|{kind}|{cat}")
    for movie, shows in new.items():
        times = ", ".join(x.get("show_time") or "" for x in shows[:3]) + (f" +{len(shows) - 3}" if len(shows) > 3 else "")
        log_opening("NEW_SHOW", "New show" if len(shows) == 1 else f"{len(shows)} new shows", venue, vname,
                    date, movie, times, shows[0].get("event_code"), key=f"{venue}|{date}|{movie}|NEW_SHOW|{times}")


# ---------------------------------------------------------------- posters
# Posters come from TMDB (free key, set on the admin page) or, without a key, from
# Wikipedia. Never from BookMyShow. A poster is only used when the match is sure:
# every word of the title, a film, released this year give or take one.
# Otherwise the movie keeps its title card.
POSTERS = {}                    # title -> (url, source, at)
POSTER_UA = {"User-Agent": "SeatWatch/1.0 (private seat-alert tool)"}
POSTER_STOP = {"the", "a", "an", "of", "and", "in"}


def poster_title(t):
    """'Dorothy (Telugu)' -> 'Dorothy'; 'Avengers Endgame: Encore' stays (a re-release won't match a year)."""
    return re.sub(r"\s*\((telugu|hindi|tamil|malayalam|kannada|english|3d|2d|imax)[^)]*\)\s*$", "", t, flags=re.I).strip()


def poster_words(t):
    import unicodedata
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()     # "Boiúna" -> "Boiuna"
    return set(re.findall(r"[a-z0-9]+", t.lower())) - POSTER_STOP


def tmdb_poster(title, key, since=""):
    """Poster url, '' when nothing matches, None when TMDB couldn't be asked.
    since: YYYY-MM-DD; for a coming-soon film an older release is a namesake."""
    t = poster_title(title)
    hdr, params = dict(POSTER_UA), {"query": t, "include_adult": "false"}
    if len(key) > 40:
        hdr["Authorization"] = "Bearer " + key       # a v4 read access token
    else:
        params["api_key"] = key                      # a v3 API key
    try:
        r = requests.get("https://api.themoviedb.org/3/search/movie", params=params, headers=hdr, timeout=15)
        if r.status_code != 200:
            return None
        results = r.json().get("results") or []
    except (requests.RequestException, ValueError):
        return None
    want, year = poster_words(t), datetime.now().year
    good = [x for x in results if x.get("poster_path") and (x.get("release_date") or "")[:4].isdigit()
            and abs(int(x["release_date"][:4]) - year) <= 1 and x["release_date"] >= since
            and want <= poster_words(f"{x.get('title', '')} {x.get('original_title', '')}")]
    good.sort(key=lambda x: (poster_words(x.get("title", "")) != want,
                             x.get("original_language") not in ("te", "hi", "ta", "ml", "kn", "en"),
                             -(x.get("popularity") or 0)))
    return f"https://image.tmdb.org/t/p/w342{good[0]['poster_path']}" if good else ""


def wiki_poster(title):
    """Poster url from the film's Wikipedia page, '' when no page is sure enough, None on error."""
    found = wiki_film(title)
    return None if found is None else found[1]


def wiki_film(title):
    """(page title, poster url or '') of the film's Wikipedia page when the match is
    sure (every title word, a film, this year give or take one); ('', '') when not;
    None when Wikipedia couldn't be asked."""
    t = poster_title(title)
    try:
        r = requests.get("https://en.wikipedia.org/w/api.php", headers=POSTER_UA, timeout=15, params={
            "action": "query", "format": "json", "generator": "search", "gsrsearch": t + " film", "gsrlimit": 5,
            "prop": "pageimages|description", "piprop": "thumbnail", "pithumbsize": 342, "pilicense": "any"})
        if r.status_code != 200:
            return None
        pages = sorted((r.json().get("query") or {}).get("pages", {}).values(), key=lambda x: x.get("index", 0))
    except (requests.RequestException, ValueError):
        return None
    want, year = poster_words(t), datetime.now().year
    for pg in pages:
        desc = (pg.get("description") or "").lower()
        years = [int(y) for y in re.findall(r"\b(19\d\d|20\d\d)\b", desc)]
        src = (pg.get("thumbnail") or {}).get("source") or ""
        if ("film" in desc and years and abs(years[0] - year) <= 1
                and want <= poster_words(re.sub(r"\(.*?\)", "", pg.get("title", "")))):
            return pg["title"], src
    return "", ""


MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"), 1)}


def wiki_release(page):
    """The release date in a film page's infobox (the last one listed: festival
    screenings come first, the theatrical release last) as YYYY-MM-DD; '' / None on error."""
    try:
        r = requests.get("https://en.wikipedia.org/w/api.php", headers=POSTER_UA, timeout=15, params={
            "action": "query", "format": "json", "prop": "revisions", "rvprop": "content", "rvslots": "main",
            "rvsection": 0, "titles": page})
        if r.status_code != 200:
            return None
        pages = list((r.json().get("query") or {}).get("pages", {}).values())
        text = ((pages[0].get("revisions") or [{}])[0].get("slots", {}).get("main", {}).get("*", "")) if pages else ""
    except (requests.RequestException, ValueError):
        return None
    m = re.search(r"\|\s*released?\s*=(.*?)(?:\n\s*\|\s*[a-z_ ]+=|\n\}\})", text, re.S | re.I)
    if not m:
        return ""
    seg, days = m.group(1), []
    for y, mo, d in re.findall(r"(?<!\d)(20\d\d)\s*\|\s*(\d{1,2})\s*\|\s*(\d{1,2})(?!\d)", seg):
        days.append((int(y), int(mo), int(d)))
    for d, mo, y in re.findall(r"(\d{1,2})\s+([A-Za-z]+)\s+(20\d\d)", seg):
        if mo.lower() in MONTHS:
            days.append((int(y), MONTHS[mo.lower()], int(d)))
    for mo, d, y in re.findall(r"([A-Za-z]+)\s+(\d{1,2}),\s*(20\d\d)", seg):
        if mo.lower() in MONTHS:
            days.append((int(y), MONTHS[mo.lower()], int(d)))
    good = []
    for y, mo, d in days:
        try:
            good.append(datetime(y, mo, d).strftime("%Y-%m-%d"))
        except ValueError:
            pass
    return max(good) if good else ""


def tmdb_release(title, key):
    """Release date from TMDB (same sure-match rule as posters); '' / None on error."""
    t = poster_title(title)
    hdr, params = dict(POSTER_UA), {"query": t, "include_adult": "false"}
    if len(key) > 40:
        hdr["Authorization"] = "Bearer " + key
    else:
        params["api_key"] = key
    try:
        r = requests.get("https://api.themoviedb.org/3/search/movie", params=params, headers=hdr, timeout=15)
        if r.status_code != 200:
            return None
        results = r.json().get("results") or []
    except (requests.RequestException, ValueError):
        return None
    want, year = poster_words(t), datetime.now().year
    good = [x for x in results if (x.get("release_date") or "")[:4].isdigit()
            and abs(int(x["release_date"][:4]) - year) <= 1
            and want <= poster_words(f"{x.get('title', '')} {x.get('original_title', '')}")]
    good.sort(key=lambda x: (poster_words(x.get("title", "")) != want, -(x.get("popularity") or 0)))
    return good[0]["release_date"] if good else ""


RELEASES = {}           # title -> (YYYY-MM-DD or '', source, at)


def load_releases():
    with db() as c:
        for r in c.execute("SELECT title, day, source, at FROM releases").fetchall():
            RELEASES[r["title"]] = (r["day"] or "", r["source"] or "", r["at"] or 0)


def release_lookups(key):
    """Expected release dates for "Coming soon" films, re-checked every 2 days (they move).
    A date over a month old is a different film with the same name: dropped."""
    oldest = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    for m in [m for m in list(MOVIE_CACHE["items"] or []) if m.get("upcoming")]:
        title = m["title"]
        day, _, at = RELEASES.get(title, (None, "", 0))
        if day is not None and time.time() - at < 2 * 86400:
            continue
        found, src, failed = "", "", False
        if key:
            got = tmdb_release(title, key)
            time.sleep(2)
            if got is None:
                failed = True
            elif got >= oldest:
                found, src = got, "tmdb"
        if not found:
            page = wiki_film(title)
            time.sleep(2)
            if page is None:
                failed = True
            elif page[0]:
                got = wiki_release(page[0])
                time.sleep(2)
                if got is None:
                    failed = True
                elif got >= oldest:
                    found, src = got, "wikipedia"
        if not found and failed:
            continue
        RELEASES[title] = (found, src, time.time())
        with LOCK, db() as c:
            c.execute("INSERT OR REPLACE INTO releases VALUES (?,?,?,?)", (title, found, src, time.time()))


UPCOMING_EVERY = 3600   # each "Coming soon" film's booking dates, once an hour, in the background


def upcoming_worker():
    """Keeps the release calendar's booking dates fresh: one request per coming-soon
    film an hour, at low priority (watches and visitors go first)."""
    time.sleep(90)
    while True:
        try:
            if setting("paused", "0") != "1":
                for m in [m for m in list(MOVIE_CACHE["items"] or []) if m.get("upcoming")]:
                    with low_priority():
                        movie_dates(m, max_age=UPCOMING_EVERY - 60)
        except Exception:
            traceback.print_exc()
        time.sleep(UPCOMING_EVERY)


def upcoming_view():
    out = []
    for m in with_posters(movie_catalog()):
        if not m.get("upcoming"):
            continue
        day, src, _ = RELEASES.get(m["title"], ("", "", 0))
        hit = DATES_CACHE.get(m["code"])
        out.append({"code": m["code"], "title": m["title"], "poster": m["poster"], "release": day, "source": src,
                    "dates": hit[1] if hit else None, "checked": int(hit[0]) if hit else None})
    return out


def load_posters():
    with db() as c:
        for r in c.execute("SELECT title, url, source, at FROM posters").fetchall():
            POSTERS[r["title"]] = (r["url"] or "", r["source"] or "", r["at"] or 0)


def poster_worker():
    """Looks up posters for new titles, gently (one request every 2s), every 10 minutes."""
    time.sleep(15)
    while True:
        try:
            key = setting("tmdb_key", "").strip()
            for m in list(MOVIE_CACHE["items"] or []):
                title = m["title"]
                url, _, at = POSTERS.get(title, (None, "", 0))
                if url is not None and time.time() - at < (30 if url else 3) * 86400:
                    continue
                found, src, failed = "", "", False
                since = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d") if m.get("upcoming") else ""
                for name, look in ((("tmdb", lambda: tmdb_poster(title, key, since)),) if key else ()) + (
                        ("wikipedia", lambda: wiki_poster(title)),):
                    got = look()
                    time.sleep(2)
                    if got is None:
                        failed = True
                    elif got:
                        found, src = got, name
                        break
                if not found and failed:
                    continue                     # ask again next round
                POSTERS[title] = (found, src, time.time())
                with LOCK, db() as c:
                    c.execute("INSERT OR REPLACE INTO posters VALUES (?,?,?,?)", (title, found, src, time.time()))
        except Exception:
            traceback.print_exc()
        try:
            release_lookups(setting("tmdb_key", "").strip())
        except Exception:
            traceback.print_exc()
        time.sleep(600)


def poster_state():
    titles = [m["title"] for m in MOVIE_CACHE["items"] or []]
    got = [POSTERS[t][1] for t in titles if POSTERS.get(t, ("",))[0]]
    return {"movies": len(titles), "tmdb": got.count("tmdb"), "wikipedia": got.count("wikipedia"),
            "key": bool(setting("tmdb_key", "").strip())}


def with_posters(movies):
    return [{**m, "poster": POSTERS.get(m["title"], ("",))[0] or m.get("poster") or ""} for m in movies]


def popular_now():
    """What people here track most (distinct people, not watches), topped up with the
    films playing at the most cinemas. Counts only, never who."""
    with db() as c:
        subs = c.execute("SELECT topic, movie, venue, venue_name FROM subs").fetchall()
        mws = c.execute("SELECT topic, title FROM mwatch").fetchall()
    catalog = [m for m in movie_catalog()]
    by_title = {m["title"].upper(): m for m in catalog}

    def film(name):
        name = (name or "").strip().upper()
        if not name:
            return None
        return by_title.get(name) or next((m for m in catalog if name in m["title"].upper()), None)
    fans, vfans, vname = {}, {}, {}
    for r in subs:
        m = film(r["movie"])
        if m:
            fans.setdefault(m["code"], set()).add(r["topic"])
        vfans.setdefault(r["venue"], set()).add(r["topic"])
        vname[r["venue"]] = r["venue_name"]
    for r in mws:
        m = film(r["title"])
        if m:
            fans.setdefault(m["code"], set()).add(r["topic"])
    by_code = {m["code"]: m for m in with_posters(catalog)}
    movies = [{"code": c, "title": by_code[c]["title"], "poster": by_code[c]["poster"],
               "upcoming": bool(by_code[c].get("upcoming")), "watching": len(t)}
              for c, t in sorted(fans.items(), key=lambda x: -len(x[1])) if c in by_code][:10]
    seen = {m["code"] for m in movies}
    for m in sorted((m for m in by_code.values() if not m.get("upcoming") and m["code"] not in seen),
                    key=lambda m: -(m.get("cinemas") or 0)):
        if len(movies) >= 10:
            break
        movies.append({"code": m["code"], "title": m["title"], "poster": m["poster"], "upcoming": False,
                       "cinemas": m.get("cinemas") or 0})
    venues = [{"code": v, "name": vname[v], "watching": len(t)}
              for v, t in sorted(vfans.items(), key=lambda x: -len(x[1]))][:6]
    return {"movies": movies, "venues": venues}


def openings_feed(limit=20):
    """Last two days of openings for shows that haven't passed (no names, no topics)."""
    today = datetime.now().strftime("%Y%m%d")
    since = (datetime.now() - timedelta(hours=48)).isoformat(timespec="seconds")
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT at, kind, what, venue, venue_name, date_code AS date, movie, show_time, movie_code "
            "FROM openings WHERE at>=? AND (date_code='' OR date_code>=?) ORDER BY id DESC LIMIT ?",
            (since, today, limit)).fetchall()]


LEDGER_WORDS = {"held": "seats held", "selected": "seats found (test mode)", "pay_ask": "payment asked",
                "awaiting_payment": "UPI QR sent", "booked": "BOOKED", "released": "seats released",
                "pay_failed": "payment step failed", "over_budget": "over price cap", "no_seats": "no seats",
                "limit": "daily hold limit", "already_held": "already held", "mismatch": "seat click failed",
                "click_failed": "seat click failed", "no_map": "seat map didn't load"}


def activity_log(limit=80):
    """One timeline for the admin: the website's events (requests, approvals,
    holds sent, tracker on/off) plus the holder's ledger (held, paid, released)."""
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT at, who, kind, detail, sub_id FROM activity "
                                           "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    try:
        ledger = json.loads((Path(__file__).resolve().parent / "holds.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ledger = []
    for e in ledger[-limit:]:
        if not e.get("day") or not e.get("at"):
            continue
        amount = f" · Rs {e['payable']}" if e.get("payable") else (f" · Rs {e['total']}" if e.get("total") else "")
        rows.append({"at": f"{e['day']}T{e['at']}", "who": e.get("for") or "owner",
                     "kind": LEDGER_WORDS.get(e.get("stage"), e.get("stage")),
                     "detail": f"{e.get('seats') or ''}{amount} · {e.get('detail') or ''} ({e.get('key', '')})".strip(" ·"),
                     "sub_id": e.get("sub_id")})
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows[:limit]


def trusted():
    try:
        return set(json.loads(setting("trusted", "[]")))
    except ValueError:
        return set()


def purge_stale():
    """
    Drop subscriptions for dates that have passed. Without this the poller
    keeps fetching dead pages for ever.
    """
    today = datetime.now().strftime("%Y%m%d")
    with LOCK, db() as c:
        # history of watches that no longer exist (stopped, dropped, expired);
        # movie watches keep theirs under negative ids
        c.execute("DELETE FROM alert_log WHERE sub_id > 0 AND sub_id NOT IN (SELECT id FROM subs)")
        c.execute("DELETE FROM alert_log WHERE sub_id < 0 AND -sub_id NOT IN (SELECT id FROM mwatch)")
        c.execute("DELETE FROM groups WHERE sub_id NOT IN (SELECT id FROM subs)")
        c.execute("DELETE FROM group_members WHERE group_id NOT IN (SELECT id FROM groups)")
        rows = c.execute("SELECT id,url FROM subs WHERE date_code < ?", (today,)).fetchall()
        if not rows:
            return 0
        c.execute("DELETE FROM subs WHERE date_code < ?", (today,))
        live = {r["url"] for r in c.execute("SELECT DISTINCT url FROM subs").fetchall()}
        for r in rows:
            if r["url"] not in live:
                c.execute("DELETE FROM snapshots WHERE url=?", (r["url"],))
    print(f"  purged {len(rows)} expired subscription(s)")
    return len(rows)


def load_snapshot(url):
    with db() as c:
        r = c.execute("SELECT data FROM snapshots WHERE url=?", (url,)).fetchone()
    try:
        return json.loads(r["data"]) if r else None
    except json.JSONDecodeError:
        return None


def save_snapshot(url, snap):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?)",
                  (url, json.dumps(snap), datetime.now().isoformat(timespec="seconds")))


# ---------------------------------------------------------------- venues
CODE_KEYS = ("VenueCode", "venueCode", "venue_code", "VenueStrCode", "code", "id")
NAME_KEYS = ("VenueName", "venueName", "venue_name", "VenueStrName",
             "name", "title", "displayName")
VENUE_CODE_RE = re.compile(r"^[A-Z0-9]{3,6}$")


def find_venues(obj, found=None):
    """
    Pull venue code/name pairs out of any BookMyShow page state.
    Different pages name these fields differently, so try several shapes and
    fall back to a code-format check to avoid picking up unrelated objects.
    """
    if found is None:
        found = {}
    if isinstance(obj, dict):
        code = name = None
        for k in CODE_KEYS:
            if obj.get(k):
                code = str(obj[k]).strip().upper()
                break
        for k in NAME_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and 3 < len(v) < 90:
                name = v.strip()
                break
        explicit = any(k in obj for k in ("VenueCode", "venueCode", "venue_code"))
        if code and name and VENUE_CODE_RE.match(code):
            # a bare code+name pair could be anything; only trust it when the
            # object also looks venue-ish
            venueish = explicit or any(
                k in obj for k in ("VenueName", "venueName", "venue_name",
                                   "VenueAddress", "venueAddress", "ShowTimes",
                                   "showTimes", "isFnBAvailable"))
            if venueish:
                found[code] = name
        for v in obj.values():
            find_venues(v, found)
    elif isinstance(obj, list):
        for v in obj:
            find_venues(v, found)
    return found


def import_venues(client, url, region="HYD"):
    html = client.fetch(url, as_json=False)
    if not html:
        return 0, "couldn't load that page"
    state = extract_initial_state(html)
    if not state:
        return 0, "no data found on that page"
    found = find_venues(state)
    if not found:
        return 0, "no cinemas found on that page"
    added = 0
    with LOCK, db() as c:
        for code, name in found.items():
            if not c.execute("SELECT code FROM venues WHERE code=?", (code,)).fetchone():
                c.execute("INSERT INTO venues VALUES (?,?,?)", (code, name, region))
                added += 1
    return added, f"added {added} new cinema(s) ({len(found)} on that page)"


class MovieMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.script = None
        self.movies = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type") == "application/ld+json":
            self.script = ""

    def handle_data(self, data):
        if self.script is not None:
            self.script += data

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            try:
                data = json.loads(self.script)
                if data.get("@type") in ("ItemList", "Movie"):
                    self.movies.append(data)
            except (ValueError, AttributeError):
                pass
            self.script = None


def cinema_movies(date):
    """{event code: {"title", "cinemas"}} for every movie on any known cinema's
    page for `date`. BookMyShow's explore page lists only ~10 films; the cinemas
    show everything (about 100 quick page fetches)."""
    with db() as c:
        venues = [dict(r) for r in c.execute("SELECT code, name, region FROM venues").fetchall()]

    refused = [0]

    def one(v):
        if refused[0] >= 5 or time.time() < BACKOFF["until"]:
            return []                          # being rate-limited: stop the sweep
        client = HttpClient({})
        try:
            html = client.fetch(venue_url(v["code"], v["name"], v["region"], date), as_json=False)
            if not html:
                if BACKOFF["n"]:                # a real refusal (not just waiting for the budget)
                    refused[0] += 1
                return []
            if page_date(html) != date:
                return []
            return [(s["event_code"], s.get("movie") or "") for s in parse_cinema_page(html) if s.get("event_code")]
        except Exception:
            return []
        finally:
            client.close()

    found = {}
    # one page at a time, low priority: spread over several minutes by the budget
    with low_priority():
        for shows in map(one, venues):
            for code, title in set(shows):
                f = found.setdefault(code, {"title": title, "cinemas": 0})
                f["cinemas"] += 1
    if refused[0] >= 5:
        raise RuntimeError("BookMyShow is rate-limiting; keeping the last sweep")
    return found


MOVIE_DETAILS = {}          # film code -> {"poster", "genre", "languages"}: read once per film
CINEMA_SWEEP = {"day": "", "at": 0.0, "found": {}}
CATALOG_REFRESH = {"busy": False}


def movie_catalog():
    """The movie list, always answered from memory once built: when it's older than
    10 minutes a background refresh starts and callers keep the current list
    (building it reads ~100 cinema pages and takes ~15s)."""
    if MOVIE_CACHE["items"]:
        if time.time() - MOVIE_CACHE["at"] > 3 * 3600 and not CATALOG_REFRESH["busy"]:
            CATALOG_REFRESH["busy"] = True

            def refresh():
                try:
                    build_catalog()
                except Exception:
                    traceback.print_exc()
                finally:
                    CATALOG_REFRESH["busy"] = False
            threading.Thread(target=refresh, daemon=True).start()
        return MOVIE_CACHE["items"]
    # nothing built yet: one build at a time, and not again for a minute after a
    # failed one (BookMyShow refusing us must not trigger a sweep per page view)
    if CATALOG_REFRESH["busy"] or time.time() - CATALOG_REFRESH.get("failed", 0) < 60:
        return []
    CATALOG_REFRESH["busy"] = True
    try:
        items = build_catalog()
        if not items:
            CATALOG_REFRESH["failed"] = time.time()
        return items
    finally:
        CATALOG_REFRESH["busy"] = False


def build_catalog():
    html = CLIENT.fetch("https://in.bookmyshow.com/explore/movies-hyderabad", as_json=False)
    parser = MovieMetadata()
    if html:
        parser.feed(html)
    listing = next((x for x in parser.movies if x.get("@type") == "ItemList"), {})
    items = []
    for entry in listing.get("itemListElement", []):
        match = re.search(r"/([^/]+)/(ET\d+)$", entry.get("url", ""))
        if match:
            items.append({"title": entry.get("name", ""), "slug": match[1],
                          "code": match[2], "poster": "", "genre": ""})
    try:
        saved = json.loads(setting("custom_movies", "[]"))
    except ValueError:
        saved = []
    known = {item["code"] for item in items}
    items.extend(item for item in saved if item.get("code") not in known)
    # plus everything the cinemas are actually showing today, most widely shown first
    today = datetime.now().strftime("%Y%m%d")
    if not CINEMA_SWEEP["day"]:                  # after a restart: today's saved sweep, if any
        try:
            saved = json.loads(setting("cinema_sweep", "{}") or "{}")
            if saved.get("day"):
                CINEMA_SWEEP.update(day=saved["day"], at=saved.get("at", 0), found=saved.get("found") or {})
        except ValueError:
            pass
    showing = CINEMA_SWEEP["found"]              # last sweep (today's once it has run)
    if CINEMA_SWEEP["day"] != today and not CINEMA_SWEEP.get("running"):
        # the sweep takes minutes at background pace: build the list now from what we
        # have, and rebuild it once the sweep is done
        CINEMA_SWEEP["running"] = True

        def sweep():
            try:
                found = cinema_movies(today)
                if found:
                    CINEMA_SWEEP.update(day=today, at=time.time(), found=found)
                    set_setting("cinema_sweep", json.dumps({"day": today, "at": time.time(), "found": found}))
                    build_catalog()
            except Exception as e:
                print("  cinema sweep stopped:", e)
            finally:
                CINEMA_SWEEP["running"] = False
        threading.Thread(target=sweep, daemon=True).start()
    for item in items:
        item["cinemas"] = showing.get(item["code"], {}).get("cinemas", 0)
    # one card per film: BookMyShow gives each language / format its own code, and
    # a film's page already gathers the others (movie_shows follows its format selector)
    known = {item["code"] for item in items}
    titles = {item["title"].strip().lower() for item in items}
    for code, f in sorted(showing.items(), key=lambda kv: -kv[1]["cinemas"]):
        t = f["title"].strip().lower()
        if code not in known and t and t not in titles:
            items.append({"title": f["title"], "slug": slugify(f["title"]), "code": code,
                          "poster": "", "genre": "", "cinemas": f["cinemas"]})
            known.add(code)
            titles.add(t)
    # "Coming soon": films BMS lists before bookings open, so they can be tracked
    # and people told the moment the first date opens
    known = {item["code"] for item in items}
    up = CLIENT.fetch("https://in.bookmyshow.com/explore/upcoming-movies-hyderabad", as_json=False)
    if up:
        parser = MovieMetadata()
        parser.feed(up)
        listing = next((x for x in parser.movies if x.get("@type") == "ItemList"), {})
        for entry in listing.get("itemListElement", []):
            match = re.search(r"/([^/]+)/(ET\d+)$", entry.get("url", ""))
            if match and match[2] not in known:
                items.append({"title": entry.get("name", ""), "slug": match[1], "code": match[2],
                              "poster": "", "genre": "", "upcoming": True})
                known.add(match[2])

    # no film pages (posters, genres, languages): names and codes only, to keep
    # requests to BookMyShow down; the cards show the title instead of a poster
    if items:
        MOVIE_CACHE.update(at=time.time(), items=items)
    return items


DATES_CACHE = {}
VENUE_DATES_CACHE = {}


# Hyderabad localities, to turn messy cinema names into one area each
# ("PVR: Atrium Gachibowli, Hyderabad" and "AMB Cinemas: Gachibowli" -> Gachibowli)
LOCALITIES = [
    "Gachibowli", "Kokapet", "Kondapur", "Madhapur", "Hitech City", "Kukatpally", "KPHB", "Miyapur",
    "Chandanagar", "Nallagandla", "Lingampally", "Nizampet", "Bachupally", "Moosapet", "Balanagar",
    "Kompally", "Suchitra", "Alwal", "Bowenpally", "Secunderabad", "Begumpet", "Ameerpet", "Panjagutta",
    "Somajiguda", "Banjara Hills", "Jubilee Hills", "Khairatabad", "Himayatnagar", "Abids", "Nampally",
    "Koti", "RTC X Roads", "Musheerabad", "Kachiguda", "Amberpet", "Tarnaka", "Malkajgiri", "ECIL",
    "AS Rao Nagar", "Kapra", "Uppal", "Nagole", "LB Nagar", "Dilsukhnagar", "Vanasthalipuram",
    "Hayathnagar", "Saroornagar", "Chaitanyapuri", "Malakpet", "Santoshnagar", "Mehdipatnam", "Tolichowki",
    "Attapur", "Rajendranagar", "Shamshabad", "Narsingi", "Manikonda", "Puppalaguda", "Financial District",
    "Shameerpet", "Medchal", "Patancheru", "Beeramguda", "Chandanagar", "Langer House", "Karkhana",
    "Sainikpuri", "Neredmet", "Boduppal", "Peerzadiguda", "Ghatkesar", "Shadnagar", "Ibrahimpatnam",
    "RC Puram", "Kothapet", "Nacharam", "Kavadiguda", "Erragadda", "Musarambagh", "Chintal", "Karmanghat",
    "Borabanda", "Madinaguda",
]
# names that aren't a locality but mean one (malls, landmarks, spellings)
AREA_ALIASES = {"LAKESHORE": "Kukatpally", "LULU MALL": "Kukatpally", "GSM MALL": "Miyapur",
                "KAIRATHABAD": "Khairatabad", "PRASADS": "Khairatabad", "CYBERABAD": "Hitech City",
                "HITEC CITY": "Hitech City", "IRRUM MANZIL": "Panjagutta", "BANJARA HIL": "Banjara Hills"}
_LOC = sorted({**{l.upper(): l for l in LOCALITIES}, **AREA_ALIASES}.items(), key=lambda kv: -len(kv[0]))


def venue_area(name):
    """BMS names cinemas 'Brand: Area' ("AMB Cinemas: Gachibowli") -> "Gachibowli".
    That's the only location BMS gives, so areas are by name, not distance. A known
    locality anywhere in the name wins; otherwise the last part after the brand,
    minus the city."""
    text = str(name or "")
    up = re.sub(r"[^A-Z0-9 ]", " ", text.upper())
    for key, pretty in _LOC:
        if re.search(r"\b" + re.escape(key) + r"\b", up):
            return pretty
    rest = text.split(":", 1)[1] if ":" in text else text
    parts = [p.strip() for p in rest.split(",")
             if p.strip() and p.strip().lower() not in ("hyderabad", "telangana", "india")]
    return re.sub(r"\s+", " ", parts[-1] if parts else "").strip()[:40]


PAGE_CACHE = {}              # cinema day-page url -> (at, html), shared by everything
STALE_AFTER = 90             # older than this and the page says "what we saw N min ago"
FRESH = threading.local()    # per request: .age = oldest copy served, .wait = budget wait cap


def mark_stale(at):
    """This request is being answered from an older copy (BookMyShow busy or refusing)."""
    FRESH.age = max(getattr(FRESH, "age", 0), time.time() - at)


class quick_wait:
    """While a fallback copy exists, wait only a few seconds for the request budget."""
    def __init__(self, has_fallback):
        self.on = has_fallback

    def __enter__(self):
        self.prev = getattr(FRESH, "wait", None)
        if self.on:
            FRESH.wait = 4

    def __exit__(self, *exc):
        FRESH.wait = self.prev


def cinema_page(code, date, max_age=180):
    """(venue row, html) for a cinema's day page, from the shared copy when it's
    fresh enough. The watch checks keep watched pages fresh, so visitors looking
    at those cost BookMyShow nothing."""
    with db() as c:
        v = c.execute("SELECT * FROM venues WHERE code=?", (code,)).fetchone()
    if not v:
        return None, None
    url = venue_url(code, v["name"], v["region"], date)
    hit = PAGE_CACHE.get(url)
    if hit and time.time() - hit[0] <= max_age:
        return dict(v), hit[1]
    with quick_wait(bool(hit)):
        html = CLIENT.fetch(url, as_json=False)
    if html:
        remember_page(url, html)
    elif hit:
        mark_stale(hit[0])
        return dict(v), hit[1]                 # refused / busy: an older copy beats nothing
    return dict(v), html


def remember_page(url, html):
    now = time.time()
    PAGE_CACHE[url] = (now, html)
    if len(PAGE_CACHE) > 250:                  # keep older copies as fallbacks, within reason
        for k, _ in sorted(PAGE_CACHE.items(), key=lambda x: x[1][0])[:len(PAGE_CACHE) - 200]:
            PAGE_CACHE.pop(k, None)


def venue_dates(code):
    """The days a cinema lists on BookMyShow, from its own date strip:
    [{"code": "20260924", "open": True}, ...] (open False = listed, not bookable yet)."""
    from bms_seat_watch import parse_date_strip
    hit = VENUE_DATES_CACHE.get(code)
    if hit and time.time() - hit[0] < 1800:          # a cinema's date strip changes a few times a day
        return hit[1]
    v, html = cinema_page(code, datetime.now().strftime("%Y%m%d"), max_age=1800)
    if not v or not html:
        if hit:
            mark_stale(hit[0])
            return hit[1]
        return None
    dates = [{"code": d["date_code"], "open": not d["disabled"]} for d in parse_date_strip(html)]
    VENUE_DATES_CACHE[code] = (time.time(), dates)
    return dates


def movie_dates(movie, max_age=1800):
    """
    The dates BookMyShow lists for a movie, from its own date strip: it starts at
    the first bookable day (asking for today redirects there) and marks days
    without shows as disabled. [{"code": "20260927", "open": True}, ...]
    max_age: seconds a cached answer may be reused (the movie-watch checker asks fresher)
    """
    hit = DATES_CACHE.get(movie["code"])
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    today = datetime.now().strftime("%Y%m%d")
    with quick_wait(bool(hit)):
        html = CLIENT.fetch(f"https://in.bookmyshow.com/movies/hyderabad/{movie['slug']}"
                            f"/buytickets/{movie['code']}/{today}", as_json=False)
    if not html:
        if hit:
            mark_stale(hit[0])
            return hit[1]
        return None
    state = extract_initial_state(html) or {}
    queries = (state.get("showtimesFunctionalApi") or {}).get("queries") or {}
    dynamic = next((v for k, v in queries.items() if "fetchPrimaryDynamic" in k), {})
    data = (dynamic.get("data") or {}).get("data") or {}
    dates = []
    for widget in data.get("topStickyWidgets") or []:
        for d in widget.get("data") or []:
            code = str(d.get("id") or "")
            if re.fullmatch(r"\d{8}", code) and "date" in str(d.get("styleId") or ""):
                dates.append({"code": code, "open": "disabled" not in str(d.get("styleId"))})
        if dates:
            break
    DATES_CACHE[movie["code"]] = (time.time(), dates)
    return dates


SHOWS_CACHE = {}            # (film code, date) -> (at, venues)
SHOWS_STALE = {}            # the same, kept 6 hours as a fallback when BookMyShow is busy
SHOWS_INFLIGHT = {}
SHOWS_LOCK = threading.Lock()
PREFETCH = ThreadPoolExecutor(max_workers=3)


def shows_cached(movie, date, max_age=300):
    """movie_shows, kept for a minute; callers asking for the same movie and date
    while it's being fetched share that one fetch."""
    from concurrent.futures import Future
    key = (movie["code"], date)
    hit = SHOWS_CACHE.get(key)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    with SHOWS_LOCK:
        fut, mine = SHOWS_INFLIGHT.get(key), False
        if fut is None:
            fut, mine = Future(), True
            SHOWS_INFLIGHT[key] = fut
    if mine:
        try:
            with quick_wait(key in SHOWS_STALE):
                venues = movie_shows(movie, date)
            if venues is not None:
                SHOWS_CACHE[key] = SHOWS_STALE[key] = (time.time(), venues)
            fut.set_result(venues)
        except Exception:
            fut.set_result(None)
        finally:
            with SHOWS_LOCK:
                SHOWS_INFLIGHT.pop(key, None)
            now = time.time()
            for k in [k for k, (at, _) in list(SHOWS_CACHE.items()) if now - at > 600]:
                SHOWS_CACHE.pop(k, None)
            for k in [k for k, (at, _) in list(SHOWS_STALE.items()) if now - at > 6 * 3600]:
                SHOWS_STALE.pop(k, None)
    venues = fut.result(timeout=45)
    if venues is None and key in SHOWS_STALE:
        at, venues = SHOWS_STALE[key]
        mark_stale(at)
    return venues


def prefetch_shows(movie, dates):
    for d in dates:
        hit = SHOWS_CACHE.get((movie["code"], d))
        if not hit or time.time() - hit[0] > 45:
            PREFETCH.submit(shows_cached, movie, d)


def movie_shows(movie, date):
    url = (f"https://in.bookmyshow.com/movies/hyderabad/{movie['slug']}"
           f"/buytickets/{movie['code']}/{date}")
    html = CLIENT.fetch(url, as_json=False)
    if not html:
        return None
    state = extract_initial_state(html) or {}
    queries = (state.get("showtimesFunctionalApi") or {}).get("queries") or {}
    dynamic = next((v for k, v in queries.items() if "fetchPrimaryDynamic" in k), {})
    data = (dynamic.get("data") or {}).get("data") or {}
    if (data.get("additionalData") or {}).get("dateCode") != date:
        return []
    # BookMyShow gives special formats separate event codes, linked by this selector.
    formats = [(movie["code"], "", data)]
    seen = {movie["code"]}
    for widget in (data.get("bottomSheetData") or {}).get("format-selector", {}).get("widgets") or []:
        for option in widget.get("data") or []:
            extra = (option.get("cta") or {}).get("additionalData") or {}
            code, slug = extra.get("eventCode"), extra.get("eventUrl")
            label = " ".join(filter(None, [extra.get("language"), option.get("title")]))
            if code == movie["code"]:
                formats[0] = (code, label, data)
            elif code and code != "*" and slug and code not in seen:
                formats.append((code, label, slug))
                seen.add(code)

    def load_format(item):
        code, label, slug = item
        client = HttpClient({})
        try:
            page = client.fetch(
                f"https://in.bookmyshow.com/movies/hyderabad/{slug}/buytickets/{code}/{date}",
                as_json=False)
            state = extract_initial_state(page or "") or {}
            queries = (state.get("showtimesFunctionalApi") or {}).get("queries") or {}
            dynamic = next((v for k, v in queries.items() if "fetchPrimaryDynamic" in k), {})
            return label, (dynamic.get("data") or {}).get("data") or {}
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=5) as pool:
        pages = [(formats[0][1], data)] + list(pool.map(load_format, formats[1:]))

    venues = {}
    for label, page in pages:
        if (page.get("additionalData") or {}).get("dateCode") != date:
            continue
        for widget in page.get("showtimeWidgets") or []:
            for group in widget.get("data") or []:
                for venue in group.get("data") or []:
                    info = venue.get("additionalData") or {}
                    code = info.get("venueCode")
                    if not code:
                        continue
                    shows = []
                    for section in venue.get("showtimesSections") or []:
                        for show in section.get("showtimes") or []:
                            extra = show.get("additionalData") or {}
                            shows.append({"time": extra.get("showTime") or show.get("title"),
                                          "screen": extra.get("attributes") or show.get("screenAttr") or "",
                                          "format": label,
                                          "session": extra.get("sessionId"),
                                          "open": str(extra.get("availStatus")) not in ("0", "None", ""),
                                          "status": ("Sold" if str(extra.get("availStatus")) == "0"
                                                     else "Fast filling" if show.get("styleId") == "orange-pill-with-border"
                                                     else "Available")})
                    if shows:
                        venues.setdefault(code, {"code": code,
                                                  "name": info.get("venueName") or code,
                                                  "area": venue_area(info.get("venueName") or ""),
                                                  "shows": []})["shows"].extend(shows)
    with LOCK, db() as c:
        for venue in venues.values():
            c.execute("INSERT OR IGNORE INTO venues VALUES (?,?,?)",
                      (venue["code"], venue["name"], "HYD"))
    return list(venues.values())


# ---------------------------------------------------------------- alerts
def _token():
    tf = Path(__file__).resolve().parent / "ntfy_token.txt"
    return (os.environ.get("NTFY_TOKEN") or (tf.read_text().strip() if tf.exists() else "")).strip()


def push(topic, title, body, click=""):
    h = {"Title": title.encode("ascii", "ignore").decode(),
         "Priority": "urgent", "Tags": "clapper"}
    if _token():
        h["Authorization"] = f"Bearer {_token()}"
    if click:
        h["Click"] = click
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                      headers=h, timeout=15)
        STATE["alerts"] += 1
        return True
    except Exception as e:
        print(f"  push failed {topic}: {e}")
        return False


# ---------------------------------------------------------------- auto-hold requests
# Anyone can ask for an auto-hold on one of their watches; the owner approves it
# (admin portal, or the Approve button in the ntfy alert). Once approved, the
# first time a matching show has seats open, a hold request goes to the owner's
# seat_holder.py (hold_topic) carrying the requester's seat settings. Holds happen
# in the owner's Chrome, on the owner's BookMyShow account.

CLOSED = {"0", "None", "none", ""}


def owner_cfg():
    try:
        return json.loads((Path(__file__).resolve().parent / "watch_config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def push_json(topic, title, body, click="", actions=None, priority=4):
    """ntfy publish as JSON, which is what action buttons need."""
    msg = {"topic": topic, "title": title, "message": body, "priority": priority, "tags": ["seat"]}
    if click:
        msg["click"] = click
    if actions:
        msg["actions"] = actions
    h = {"Authorization": f"Bearer {_token()}"} if _token() else {}
    try:
        requests.post("https://ntfy.sh/", json=msg, headers=h, timeout=15)
        return True
    except Exception as e:
        print(f"  push failed {topic}: {e}")
        return False


def hold_opts_extra(o):
    bits = []
    if o.get("expire_min"):
        m = o["expire_min"]
        bits.append(f"expires if not approved in {m // 60} h" if m >= 60 else f"expires if not approved in {m} min")
    if o.get("retries"):
        bits.append(f"retries {o['retries']}× if it fails")
    return bits


def hold_opts_text(o):
    parts = [f"{o.get('qty', 2)} seat(s)"]
    if o.get("categories"):
        parts.append("category " + "/".join(o["categories"]))
    if o.get("rows"):
        parts.append("rows " + ",".join(o["rows"]))
    if o.get("max_total"):
        parts.append(f"max Rs {o['max_total']} incl. fees")
    return " · ".join(parts + hold_opts_extra(o))


# ---------------------------------------------------------------- is auto-hold live?
# Website holds are done by seat_holder.py --listen in the owner's Chrome; the
# website's own poller does the watching. So "live" = the holder is running and
# connected to Chrome, which it reports in holder_status.json at least every ~45s.
HOLDER_STALE = 150
ASKED = {"any": 0.0, "by": {}, "topics": set()}   # turn-on requests (rate limits, who to tell)


CHROME_PROBE = {"at": 0, "ok": False}


def chrome_up():
    """Is the tracker's debug Chrome really there? The holder's own flag can stay
    True after Chrome is closed, so ask Chrome's port directly (cached 5s)."""
    if time.time() - CHROME_PROBE["at"] > 5:
        try:
            port = int(json.loads(Path("watch_config.json").read_text()).get("cdp_port", 9222))
        except Exception:
            port = 9222
        try:
            ok = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1).ok
        except requests.RequestException:
            ok = False
        CHROME_PROBE.update(at=time.time(), ok=ok)
    return CHROME_PROBE["ok"]


PERSONAL_STALE = 150         # seconds without a heartbeat before a personal holder counts as off


def personal_holder(owner):
    """The visitor's own holder if it's set up, switched on and alive (with Chrome)."""
    if not owner:
        return None
    with db() as c:
        r = c.execute("SELECT * FROM personal_holders WHERE owner=?", (owner,)).fetchone()
    if not r or not r["enabled"] or not r["chrome"] or time.time() - (r["last_beat"] or 0) > PERSONAL_STALE:
        return None
    return dict(r)


def target_live(sub):
    """Can this watch's auto-hold run right now? Its owner's own holder, or yours."""
    return bool(personal_holder(sub.get("owner"))) or holder_state()["live"]


PACK_FILES = ("seat_holder.py", "bms_seat_watch.py", "seat_capture.py", "seat_decode_probe.py", "seat_layout.py")

PACK_START = r"""@echo off
rem start_my_holder.bat -- your own Seat Watch holder: holds seats on YOUR BookMyShow
rem account when a watch of yours with auto-hold finds seats open. Leave it running.
cd /d %~dp0
where python >nul 2>nul || (echo  Python 3 is needed first: https://www.python.org/downloads/ ^(tick "Add to PATH"^) & pause & exit /b 1)
if not exist .venv\Scripts\python.exe (
    echo  First run: setting up ^(a minute^)...
    python -m venv .venv
    .venv\Scripts\python -m pip install -q requests playwright
)
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (echo  Google Chrome is needed: https://www.google.com/chrome/ & pause & exit /b 1)
start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%~dp0chrome-profile" https://in.bookmyshow.com/
echo.
echo  A separate Chrome window opened. The first time, log in to BookMyShow there
echo  ^(your own account^). Keep that window open. Then press any key here.
pause >nul
echo  Holder running. Keep this window and that Chrome open while you want auto-holds.
.venv\Scripts\python seat_holder.py --config watch_config.json --listen
pause
"""

PACK_README = r"""Seat Watch - your own holder
============================

What it does
  When one of your watches on {site} has auto-hold and its seats open, the
  website sends the request here. This holder picks the seats in YOUR
  BookMyShow account (in the Chrome window it opens), accepts the terms and
  stops on the payment page. You pay there yourself, normally. Tickets are
  yours, in your account. Nobody else sees your login.

Setup (once)
  1. Install Python 3 (python.org, tick "Add to PATH") and Google Chrome.
  2. Unzip this folder somewhere, e.g. Documents\SeatWatchHolder.
  3. Double-click start_my_holder.bat. A separate Chrome window opens:
     log in to BookMyShow there. Press a key in the black window.
  4. The website shows "Your own holder: online" within a minute.

Every time
  Double-click start_my_holder.bat and keep both windows open while you
  want auto-holds. Requests on your watches skip the owner's approval.

Settings (watch_config.json)
  "armed": true   holds seats up to the payment page. Set false to test:
                  it then only selects seats and tells you what it would hold.
  "max_total": 0  no price cap (a cap you set on a request still applies).

Keep this folder private: watch_config.json contains your holder's key.
Lost it, or shared it by mistake? Use "Reset" on the website and download a new pack.
"""


def personal_pack(row):
    """Zip of the holder files plus this visitor's config. No owner secrets: the
    files carry no tokens, and the config only this visitor's topic and key."""
    import io
    import zipfile
    here = Path(__file__).resolve().parent
    site = setting("public_url", "").rstrip("/")
    cfg = {"cdp_port": 9222, "use_browser": False, "ntfy_topic": row["notify"] or "",
           "hold_topic": row["topic"], "holder_port": 8799, "targets": [],
           "holder": {"armed": True, "qty": 2, "categories": [], "rows": [], "max_total": 0,
                      "max_holds_per_day": 0, "max_age_seconds": 120, "accept_terms": True,
                      "upi_pay": False, "warm_cinema_urls": []},
           "personal": {"site": site, "key": row["key"], "owner": row["owner"]}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in PACK_FILES:
            z.write(here / f, f"SeatWatchHolder/{f}")
        z.writestr("SeatWatchHolder/watch_config.json", json.dumps(cfg, indent=2))
        z.writestr("SeatWatchHolder/start_my_holder.bat", PACK_START.replace("\n", "\r\n"))
        z.writestr("SeatWatchHolder/README.txt", PACK_README.format(site=site or "the website").replace("\n", "\r\n"))
    return buf.getvalue()


def holder_state():
    f = Path(__file__).resolve().parent / "holder_status.json"
    try:
        hb = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"live": False, "why": "not started"}
    age = time.time() - float(hb.get("at") or 0)
    if not hb.get("running"):
        return {"live": False, "why": "stopped"}
    if age > HOLDER_STALE:
        return {"live": False, "why": f"no heartbeat for {int(age // 60)} min"}
    if not hb.get("chrome") or not chrome_up():
        return {"live": False, "why": "Chrome not connected"}
    return {"live": True, "armed": bool(hb.get("armed")), "warm": hb.get("warm") or []}


def requests_topic():
    cfg = owner_cfg()
    return cfg.get("requests_topic") or (f"{cfg['ntfy_topic']}-requests" if cfg.get("ntfy_topic") else "")


def start_trackers():
    """Run start_all.bat on this PC (starts only what isn't running). Returns a message."""
    bat = Path(__file__).resolve().parent / "start_all.bat"
    if os.name != "nt" or not bat.exists():
        return "start_all.bat isn't available on this machine"
    if time.time() - float(STATE.get("started_trackers", 0)) < 60:
        return "already starting, give it a minute"
    STATE["started_trackers"] = time.time()
    set_setting("start_code", "")          # a used Turn-on button can't be pressed again
    subprocess.Popen(["cmd", "/c", str(bat)], cwd=str(bat.parent),
                     creationflags=subprocess.CREATE_NEW_CONSOLE)
    print("  start_all.bat launched")
    log_activity("tracker started", "owner", "start_all.bat run from the website")
    return "starting the trackers; auto-hold should be live within a minute"


def ask_to_turn_on(name, topic):
    """A visitor asks the owner to turn the trackers on. Rate-limited."""
    now = time.time()
    if topic:
        ASKED["topics"].add(topic)       # told when it comes back
    if now - ASKED["by"].get(name, 0) < 1800:
        return "You already asked. The owner has been told."
    if now - ASKED["any"] < 300:
        ASKED["by"][name] = now
        return "Someone just asked. The owner has been told."
    ASKED["any"] = ASKED["by"][name] = now
    rt = requests_topic()
    if not rt:
        return "The owner can't be reached right now."
    base = setting("public_url", "").rstrip("/")
    code = secrets.token_urlsafe(12)
    set_setting("start_code", code)
    actions = []
    if base and os.name == "nt":
        actions.append({"action": "http", "label": "Turn on", "method": "POST", "clear": True,
                        "url": f"{base}/api/admin/tracker-start",
                        "headers": {"Content-Type": "application/json"},
                        "body": json.dumps({"code": code})})
    why = holder_state().get("why", "offline")
    log_activity("asked to turn on", name, f"tracker was off ({why})")
    push_json(rt, "Trackers need to be turned on",
              f"{name} wants to request an auto-hold, but the tracker is off ({why}).\n\n"
              + ("Tap Turn on to run start_all.bat on the PC." if actions else
                 "Run start_all.bat on the PC."), f"{base}/admin" if base else "", actions)
    return "Asked. You'll get a notification when auto-hold is live."


def tell_askers_if_back(was_live):
    """Poller hook: when the holder comes back, tell everyone who asked."""
    live = holder_state()["live"]
    if live != was_live:
        log_activity("tracker on" if live else "tracker off", "", "holder " + ("running" if live else "stopped"))
    if live and not was_live and ASKED["topics"]:
        for t in list(ASKED["topics"]):
            push(t, "Tracker is on", "The tracker is on. You can request an auto-hold now.")
        ASKED["topics"].clear()
    return live


def clean_prefs(b):
    """What a visitor may save as preferences, validated and trimmed."""
    split = lambda v, n, ln: [str(x).strip()[:ln] for x in (v if isinstance(v, list) else str(v or "").split(","))
                              if str(x).strip()][:n]
    try:
        qty = min(10, max(1, int(b.get("qty") or 2)))
        max_total = min(100000, max(0, int(b.get("max_total") or 0)))
    except (TypeError, ValueError):
        qty, max_total = 2, 0
    return {"qty": qty, "max_total": max_total,
            "categories": [c.upper() for c in split(b.get("categories"), 5, 30)],
            "rows": [r.upper() for r in split(b.get("rows"), 12, 3) if re.fullmatch(r"[A-Za-z]{1,2}", r)],
            "formats": [f.upper() for f in split(b.get("formats"), 8, 40)],
            "languages": split(b.get("languages"), 6, 20),
            "areas": split(b.get("areas"), 12, 40),
            "venues": [v.upper() for v in split(b.get("venues"), 40, 12)],
            "quiet_from": str(b.get("quiet_from") or "")[:5] if re.fullmatch(r"\d\d:\d\d", str(b.get("quiet_from") or "")) else "",
            "quiet_to": str(b.get("quiet_to") or "")[:5] if re.fullmatch(r"\d\d:\d\d", str(b.get("quiet_to") or "")) else ""}


def quiet_now(name):
    """True while this person's quiet hours are on (e.g. 23:00-07:00)."""
    p = get_prefs(name) if name else {}
    a, b = p.get("quiet_from"), p.get("quiet_to")
    if not a or not b or a == b:
        return False
    now = datetime.now().strftime("%H:%M")
    return a <= now < b if a < b else (now >= a or now < b)


def get_prefs(name):
    with db() as c:
        row = c.execute("SELECT data FROM prefs WHERE name=?", (name,)).fetchone()
    try:
        return json.loads(row["data"]) if row else {}
    except ValueError:
        return {}


def clean_hold_opts(b):
    """Validate what a visitor asked for. Returns (opts, error)."""
    try:
        qty = int(b.get("qty") or 2)
        max_total = int(b.get("max_total") or 0)
    except (TypeError, ValueError):
        return None, "Seats and price cap must be numbers."
    if not 1 <= qty <= 10:
        return None, "Seats must be between 1 and 10."
    if not 0 <= max_total <= 100000:
        return None, "Price cap must be between 0 and 100000."
    split = lambda s, n, ln: [x.strip().upper() for x in str(s or "").split(",") if x.strip()][:n]
    cats = [c[:30] for c in split(b.get("categories"), 5, 30)]
    rows = [r for r in split(b.get("rows"), 12, 3) if re.fullmatch(r"[A-Z]{1,2}", r)]
    try:
        expire = int(b.get("expire_min") or 0)       # 0 = the request never expires
        retries = int(b.get("retries") or 0)
    except (TypeError, ValueError):
        expire, retries = 0, 0
    expire = expire if expire in (0, 30, 60, 180, 720, 1440) else 0
    retries = min(3, max(0, retries))
    return {"qty": qty, "categories": cats, "rows": rows, "max_total": max_total,
            "expire_min": expire, "retries": retries, "retried": 0}, ""


def notify_owner_of_request(sub):
    """ntfy alert to the owner with Approve / Reject buttons. The buttons POST to
    the public site with the request's one-time code, so no admin key is sent."""
    cfg = owner_cfg()
    topic = cfg.get("ntfy_topic")
    if not topic:
        return
    opts = json.loads(sub["hold_opts"] or "{}")
    base = setting("public_url", "").rstrip("/")
    g = group_for_sub(sub["id"])
    body = (f"{sub['hold_by'] or sub['topic']} wants seats held\n"
            f"{sub['venue_name']} · {sub['date_code']} · {describe(sub)}\n"
            f"{hold_opts_text(opts)}\n"
            + (f"Group booking: {group_names(g)}\n" if g and group_members(g) else "") + "\n"
            + ("Tap Approve / Reject, or use the admin portal." if base else
               "Approve it in the admin portal (no public link known for buttons)."))
    actions = []
    if base:
        for label, decision in (("Approve", "approve"), ("Reject", "reject")):
            actions.append({"action": "http", "label": label, "method": "POST", "clear": True,
                            "url": f"{base}/api/admin/hold-decide",
                            "headers": {"Content-Type": "application/json"},
                            "body": json.dumps({"id": sub["id"], "code": sub["hold_code"],
                                                "decision": decision})})
    push_json(topic, f"Auto-hold request #{sub['id']}", body,
              f"{base}/admin" if base else "", actions)


def decide_hold(sub_id, approve, by="owner"):
    """Approve or reject a pending request. Returns a message. by: who decided
    (owner / trusted list), for the activity log."""
    with LOCK, db() as c:
        row = c.execute("SELECT * FROM subs WHERE id=?", (sub_id,)).fetchone()
        if not row or row["hold_status"] != "pending":
            return "no pending request with that id"
        c.execute("UPDATE subs SET hold_status=?, hold_code='', hold_at=? WHERE id=?",
                  ("approved" if approve else "rejected",
                   datetime.now().isoformat(timespec="seconds"), sub_id))
        sub = dict(c.execute("SELECT * FROM subs WHERE id=?", (sub_id,)).fetchone())
    opts = json.loads(sub["hold_opts"] or "{}")
    log_activity("approved" if approve else "rejected", sub["hold_by"],
                 f"{sub['venue_name']} {sub['date_code']} · {describe(sub)} · {hold_opts_text(opts)} (by {by})",
                 sub_id)
    if approve:
        push(sub["topic"], "Auto-hold approved",
             f"{sub['venue_name']} · {sub['date_code']}\n{hold_opts_text(opts)}\n\n"
             "Seats will be held as soon as a matching show opens. You'll be told here.")
        threading.Thread(target=trigger_if_open, args=(sub,), daemon=True).start()
        return f"approved #{sub_id}"
    push(sub["topic"], "Auto-hold declined", f"{sub['venue_name']} · {sub['date_code']}\n"
         "The owner declined this auto-hold. Your watch is still active.")
    return f"rejected #{sub_id}"


def publish_hold(sub, show):
    """Send one hold request to the owner's seat_holder for this approved watch."""
    cfg = owner_cfg()
    hold_topic = cfg.get("hold_topic")
    if not hold_topic or not show.get("session_id") or not show.get("event_code"):
        return False
    own = personal_holder(sub.get("owner"))
    if not own and not holder_state()["live"]:
        return False          # stays approved; the poller retries once the holder is back
    # claim it in one statement: the approval's immediate check and the poller
    # can both get here at once, and only one of them may send (seen: 2 requests)
    with LOCK, db() as c:
        claimed = c.execute("UPDATE subs SET hold_status='triggered', hold_at=? "
                            "WHERE id=? AND hold_status='approved'",
                            (datetime.now().isoformat(timespec="seconds"), sub["id"])).rowcount
    if not claimed:
        return False
    opts = json.loads(sub["hold_opts"] or "{}")
    req = {"event": show["event_code"], "venue": sub["venue"], "region": sub["region"] or "HYD",
           "session": str(show["session_id"]), "date": sub["date_code"],
           "movie": show.get("movie"), "show_time": show.get("show_time"),
           "qty": opts.get("qty", 2), "categories": opts.get("categories", []),
           "rows": opts.get("rows", []), "max_total": opts.get("max_total") or None,
           "requester": sub["hold_by"] or sub["topic"], "notify_topic": sub["topic"],
           "sub_id": sub["id"]}
    req["sent_at"] = time.time()
    try:
        if own:
            # their own holder on their PC: its private topic (no owner token on it)
            requests.post(f"https://ntfy.sh/{own['topic']}", data=json.dumps(req).encode(),
                          headers={"Title": "hold request"}, timeout=15).raise_for_status()
        elif not send_local_hold(req):
            h = {"Title": "hold request"}
            if _token():
                h["Authorization"] = f"Bearer {_token()}"
            requests.post(f"https://ntfy.sh/{hold_topic}", data=json.dumps(req).encode(),
                          headers=h, timeout=15).raise_for_status()
    except Exception as e:
        print(f"  hold request failed: {e}")
        with LOCK, db() as c:           # not sent: let the poller try again
            c.execute("UPDATE subs SET hold_status='approved' WHERE id=?", (sub["id"],))
        return False
    push(sub["topic"], "Holding seats now",
         f"{show.get('movie')} {show.get('show_time')} has seats open.\n"
         f"Asking for {hold_opts_text(opts)}. You'll get the result here.")
    print(f"  hold requested for #{sub['id']} ({sub['hold_by']}) {show.get('show_time')}")
    log_activity("sent to holder", sub["hold_by"],
                 f"{show.get('movie')} {show.get('show_time')} at {sub['venue_name']} · {hold_opts_text(opts)}",
                 sub["id"])
    log_alert(sub["id"], "HOLD", f"{show.get('show_time')}: seats open, auto-hold sent to the tracker")
    return True


NO_ANSWER_AFTER = 180      # seconds; a hold normally reports back in under 15s
HOLDER_KEY = Path(__file__).resolve().parent / "holder_local.key"


def send_local_hold(req):
    """Give the hold request straight to seat_holder on this PC (127.0.0.1), which
    skips the ~0.8s ntfy round trip. False = not delivered; the caller uses ntfy.
    Never both, so one request can't become two holds."""
    try:
        key = HOLDER_KEY.read_text(encoding="utf-8").strip()
        port = int(owner_cfg().get("holder_port", 8799))
        r = requests.post(f"http://127.0.0.1:{port}/hold", data=json.dumps(req).encode(),
                          headers={"X-Key": key, "Content-Type": "application/json"}, timeout=1.5)
        return r.status_code == 200
    except (OSError, ValueError, requests.RequestException):
        return False


def pay_deadline(sub):
    """While a payment step is waiting on the person (approve, or pay the QR),
    when it runs out, as epoch seconds; else None. For the countdown on the badge."""
    if sub.get("hold_status") != "triggered":
        return None
    mine = ledger_since(sub)
    if not mine or mine[-1].get("stage") not in ("pay_ask", "awaiting_payment"):
        return None
    e, h = mine[-1], owner_cfg().get("holder") or {}
    minutes = int(h.get("pay_approve_minutes", 4) if e["stage"] == "pay_ask" else h.get("pay_minutes", 8))
    try:
        started = datetime.strptime(f"{e['day']} {e['at']}", "%Y-%m-%d %H:%M:%S").timestamp()
    except (KeyError, ValueError):
        return None
    return started + minutes * 60


def hold_outcome(sub):
    """
    What the visitor should see for a request's hold. 'triggered' only means the
    request was sent; the result comes from the holder's ledger (holds.json, same
    PC), matched by sub_id. Returns (status, text).
    """
    st = sub.get("hold_status") or ""
    if st != "triggered":
        return st, ""
    mine = ledger_all(sub["id"])
    # only what the holder recorded for THIS request: a retry or "Request again"
    # must not show (or act on) the previous attempt's result
    try:
        since = datetime.fromisoformat(sub.get("hold_at") or "").timestamp() - 5
        mine = [e for e in mine if datetime.strptime(f"{e['day']} {e['at']}", "%Y-%m-%d %H:%M:%S").timestamp() >= since]
    except (KeyError, ValueError):
        pass
    if mine:
        e = mine[-1]
        amount = (f" · Rs {e['payable']:.2f} incl. fees" if e.get("payable")
                  else f" · Rs {e['total']} + fees" if e.get("total") else "")
        seats = f"{e.get('seats') or ''}{amount}"
        try:
            age = time.time() - datetime.strptime(f"{e['day']} {e['at']}", "%Y-%m-%d %H:%M:%S").timestamp()
        except (KeyError, ValueError):
            age = 0
        if e.get("own") and e["stage"] == "held":
            return "held", (f"Seats held on your own account: {seats}. Pay in the Chrome window on your PC "
                            "to finish; once paid, the tickets are in your BookMyShow bookings.")
        if e["stage"] in ("held", "pay_ask", "awaiting_payment") and age > 1800:
            return "released", ("Seats released: no result was recorded (the tracker was stopped), "
                                "and BookMyShow drops unpaid holds well within 30 minutes")
        if e["stage"] == "already_held":
            return "failed", ("Not held: the tracker thinks you already have seats held or booked for this "
                              "show. If you don't, request again.")
        if e["stage"] == "held":
            return "held", f"Seats held: {seats}. The owner is completing the booking."
        if e["stage"] == "pay_ask":
            return "held", f"Seats held: {seats}. Approve or decline paying in your notification."
        if e["stage"] == "awaiting_payment":
            return "held", f"Seats held: {seats}. Scan the UPI QR sent to your phone to pay."
        if e["stage"] == "booked":
            return "booked", f"Booked: {seats}. Your ticket was sent to your phone."
        if e["stage"] == "released":
            return "released", "Seats released: " + (e.get("detail") or "not paid")
        if e["stage"] == "pay_failed":
            return "held", f"Seats held: {seats}. The owner will finish the payment ({e.get('detail')})."
        if e["stage"] == "selected":
            return "found", f"Seats found: {seats} (test mode, nothing reserved)"
        return "failed", "Couldn't hold seats: " + (e.get("detail") or e["stage"])
    try:
        age = (datetime.now() - datetime.fromisoformat(sub.get("hold_at") or "")).total_seconds()
    except ValueError:
        age = 0
    if age > NO_ANSWER_AFTER:
        return "noanswer", "No answer from the tracker. Ask the owner, or request again."
    return "triggered", ""


def open_for_hold(show):
    cats = show.get("categories") or {}
    return any(v not in CLOSED for v in cats.values()) if cats else str(show.get("avail")) not in CLOSED


def trigger_if_open(sub):
    """Right after approval: if a matching show is already bookable, hold now
    rather than waiting for a change that may never come."""
    if not target_live(sub):
        return                # the poller picks it up when the holder is back
    if seat_filter_of(sub) and seat_maps_working():
        SEAT_DUE.add(sub["id"])   # a seat-alert watch holds once its own seats are free
        return
    try:
        html = CLIENT.fetch(sub["url"], as_json=False)
        if not html or page_date(html) != sub["date_code"]:
            return
        for show in parse_cinema_page(html):
            if wanted(show, sub) and open_for_hold(show):
                publish_hold(sub, show)
                return
    except Exception as e:
        print("hold trigger error:", e)


def watch_alert(sub, title, body, link):
    """A seat alert with buttons, so people can act straight from the
    notification: Book now (BookMyShow), Request auto-hold (opens the site with
    the form ready, while the tracker is on and none is active yet), and Stop
    watching (the watch's own secret token, works without opening the site)."""
    base = setting("public_url", "").rstrip("/")
    actions = []
    if base and sub.get("token"):
        if sub.get("hold_status") in ("", None, "failed", "noanswer", "found", "released", "expired", "rejected") \
                and target_live(sub):
            actions.append({"action": "http", "label": "Request auto-hold", "method": "POST", "clear": True,
                            "url": f"{base}/api/admin/sub-hold",
                            "headers": {"Content-Type": "application/json"},
                            "body": json.dumps({"id": sub["id"], "token": sub["token"]})})
        if sub.get("token"):
            actions.append({"action": "http", "label": "Stop watching", "method": "POST", "clear": True,
                            "url": f"{base}/api/admin/sub-stop",
                            "headers": {"Content-Type": "application/json"},
                            "body": json.dumps({"id": sub["id"], "token": sub["token"]})})
    if not push_json(sub["topic"], title, body, link, actions, priority=2 if quiet_now(sub.get("owner")) else 5):
        push(sub["topic"], title, body, link)       # plain alert if the rich one failed
    STATE["alerts"] += 1


# ---------------------------------------------------------------- group booking
GROUP_MAX = 10            # BookMyShow's most seats in one booking
GROUP_DONE = ("held", "booked", "released", "failed", "noanswer", "rejected", "found")
UPI_RE = re.compile(r"[A-Za-z0-9._-]{2,64}@[A-Za-z][A-Za-z0-9]{1,30}")


ACTIVE_HOLD = {"pending": "an auto-hold request waiting for approval",
               "approved": "an approved auto-hold waiting for seats",
               "triggered": "seats being held right now", "held": "seats held",
               "payment": "a payment pending"}


def request_hold(sub, me, b):
    """Ask for an auto-hold on this watch (`sub`, already checked to be `me`'s).
    b: qty, max_total, categories, rows, expire_min, retries as the form sends them.
    Returns {"ok": True[, "auto": True]} or {"error": "..."}."""
    own = personal_holder(sub.get("owner"))
    if not own and not holder_state()["live"]:
        return {"error": "Auto-hold is off right now. Use \"Ask the owner to turn it on\"."}
    opts, err = clean_hold_opts(b)
    if err:
        return {"error": err}
    # per-person limit on requests per day (admin setting)
    limit = int(setting("hold_limit", "3") or 3)
    today = datetime.now().strftime("%Y-%m-%d")
    with db() as c:
        used = c.execute("SELECT COUNT(*) n FROM activity WHERE kind='requested' AND who=? AND at LIKE ?",
                         (me, today + "%")).fetchone()["n"]
    if limit and used >= limit:
        return {"error": f"You've made {used} auto-hold requests today, the daily limit. Try again tomorrow."}
    sf = seat_filter_of(sub)
    if sf:                                  # same rows / categories as the seat alert
        opts["categories"] = opts["categories"] or sf.get("categories", [])
        opts["rows"] = opts["rows"] or sf.get("rows", [])
        if not b.get("qty"):
            opts["qty"] = sf["together"]
    g = group_for_sub(sub["id"])
    if g and group_members(g):
        opts["qty"] = group_total(g)          # everyone's seats, side by side
        if opts["qty"] > GROUP_MAX:
            return {"error": f"The group wants {opts['qty']} seats; BookMyShow allows {GROUP_MAX} per booking."}
    clash = overlapping_hold(sub["id"], me)
    if clash:
        return {"error": clash}
    with LOCK, db() as c:
        row = c.execute("SELECT * FROM subs WHERE id=?", (sub["id"],)).fetchone()
        if not row:
            return {"error": "That watch has ended."}
        # a new request is fine after a failure, a test-mode find or no answer;
        # not while one is waiting, approved, in flight, or already holding seats
        status, _ = hold_outcome(dict(row))
        if status in ("pending", "approved", "triggered", "held", "booked"):
            return {"error": "Auto-hold is already requested or active for this watch."}
        c.execute("UPDATE subs SET hold_status='pending', hold_opts=?, hold_code=?, hold_by=?,"
                  " hold_at=? WHERE id=?",
                  (json.dumps(opts), secrets.token_urlsafe(12), me,
                   datetime.now().isoformat(timespec="seconds"), row["id"]))
        sub = dict(c.execute("SELECT * FROM subs WHERE id=?", (row["id"],)).fetchone())
    log_activity("requested", me, f"{sub['venue_name']} {sub['date_code']} · {describe(sub)} · "
                                  f"{hold_opts_text(opts)}" + (" · on their own account" if own else ""), sub["id"])
    if own:
        # their own BookMyShow account and money: no owner approval needed
        decide_hold(sub["id"], True, by="their own holder")
        return {"ok": True, "auto": True, "own": True}
    if me in trusted():
        # trusted people don't wait for the owner; the owner is still told
        cfg = owner_cfg()
        if cfg.get("ntfy_topic"):
            push(cfg["ntfy_topic"], f"Auto-approved #{sub['id']} ({me})",
                 f"{me} is on your trusted list, so this auto-hold was approved automatically.\n"
                 f"{sub['venue_name']} · {sub['date_code']} · {describe(sub)}\n{hold_opts_text(opts)}")
        decide_hold(sub["id"], True, by="trusted list")
        return {"ok": True, "auto": True}
    notify_owner_of_request(sub)
    return {"ok": True}


def overlapping_hold(sub_id, me):
    """Refuse a second auto-hold for the same person and show while one is active."""
    with db() as c:
        me_row = c.execute("SELECT * FROM subs WHERE id=?", (sub_id,)).fetchone()
        if not me_row:
            return ""
        others = [dict(r) for r in c.execute(
            "SELECT * FROM subs WHERE owner=? AND id<>? AND venue=? AND date_code=? AND IFNULL(hold_status,'')<>''",
            (me, sub_id, me_row["venue"], me_row["date_code"])).fetchall()]
    for o in others:
        if me_row["session"] and o["session"] and me_row["session"] != o["session"]:
            continue                               # different showtimes
        status, _ = hold_outcome(o)
        stage = (ledger_entry(o["id"]) or {}).get("stage", "") if status == "held" else ""
        key = "payment" if stage in ("pay_ask", "awaiting_payment") else status
        if key in ACTIVE_HOLD:
            return (f"You already have {ACTIVE_HOLD[key]} for this show on another watch "
                    f"({o['venue_name']}, {describe(o)}). Finish or cancel that one first.")
    return ""


RATE = {}

# ---------------------------------------------------------------- fair use
# Through the Cloudflare tunnel every request reaches us from 127.0.0.1; the
# visitor's real address is the one Cloudflare puts in CF-Connecting-IP. It's only
# believed on those local requests, so nobody outside can fake it.
LIMITS = {
    "browse": (60, 60),         # dates / showtimes / cinema pages: 60 a minute per address
    "seatmap": (8, 60),         # seat maps: 8 a minute per address (one Chrome serves everyone)
    "watch": (12, 600),         # new watches: 12 per 10 minutes per address
    "name": (6, 3600),          # new names: 6 an hour per address
}
BROWSE_PATHS = {"/api/venue-dates", "/api/movie-dates", "/api/movie-shows", "/api/shows"}
SLOW_DOWN = "You're going a bit fast. Wait a few seconds and try again."
BUSY = "BookMyShow is busy right now (lots of people checking). Try again in a minute."


def invite_cookie(code):
    """What the invite cookie holds: tied to the current code, so a new code
    (or turning invites off and on with another) sends everyone back to the door."""
    return hmac.new(setting("admin_token").encode(), ("invite:" + code).encode(), "sha256").hexdigest()[:32]


def has_invite(handler):
    code = setting("invite_code", "")
    if not code or is_admin(handler):
        return True
    for part in (handler.headers.get("Cookie") or "").split(";"):
        if part.strip().startswith("inv="):
            return hmac.compare_digest(part.strip()[4:], invite_cookie(code))
    return False


def invite_header(code):
    return {"Set-Cookie": f"inv={invite_cookie(code)}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax"}


# with an invite code on, strangers may still look at the front page; anything that
# asks BookMyShow, makes a watch, or reaches the owner needs the code
INVITE_FREE = {"/api/movies", "/api/venues", "/api/tracker", "/api/status", "/api/openings", "/api/popular",
               "/api/me", "/api/invite", "/api/access", "/api/groups/info", "/api/upcoming"}


def client_ip(handler):
    ip = handler.client_address[0]
    if ip in ("127.0.0.1", "::1"):
        cf = (handler.headers.get("CF-Connecting-IP") or "").strip()
        if re.fullmatch(r"[0-9a-fA-F:.]{3,45}", cf):
            return cf
    return ip


def is_owner(handler, topic=""):
    """The owner (admin cookie, or their own alert topic) is never capped."""
    return is_admin(handler) or (topic and topic == owner_cfg().get("ntfy_topic"))


def limited(handler, kind):
    """True when this visitor's address has used up `kind` (see LIMITS)."""
    if is_admin(handler):
        return False
    n, per = LIMITS[kind]
    return not rate_ok(f"{kind}@{client_ip(handler)}", n, per)


def rate_ok(key, limit, per_seconds):
    """At most `limit` actions per `per_seconds` for this key (in memory)."""
    now = time.time()
    hits = [t for t in RATE.get(key, []) if now - t < per_seconds]
    if len(hits) >= limit:
        RATE[key] = hits
        return False
    RATE[key] = hits + [now]
    return True


def group_for_sub(sub_id):
    with db() as c:
        r = c.execute("SELECT * FROM groups WHERE sub_id=?", (sub_id,)).fetchone()
    return dict(r) if r else None


def group_by_code(code):
    with db() as c:
        r = c.execute("SELECT * FROM groups WHERE code=?", (str(code or "")[:40],)).fetchone()
    return dict(r) if r else None


def group_members(g):
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM group_members WHERE group_id=? ORDER BY id", (g["id"],)).fetchall()]


def group_total(g, members=None):
    members = group_members(g) if members is None else members
    return int(g["seats"] or 1) + sum(int(m["seats"]) for m in members)


def group_names(g):
    return ", ".join([f"{g['organizer']} ×{g['seats']}"] +
                     [f"{m['name']} ×{m['seats']}" for m in group_members(g)]) + f" = {group_total(g)} seats"


def ledger_since(sub):
    """Ledger entries of this watch's current request (recorded after hold_at)."""
    mine = ledger_all(sub["id"])
    try:
        since = datetime.fromisoformat(sub.get("hold_at") or "").timestamp() - 5
        return [e for e in mine if datetime.strptime(f"{e['day']} {e['at']}", "%Y-%m-%d %H:%M:%S").timestamp() >= since]
    except (KeyError, ValueError):
        return mine


def ledger_all(sub_id):
    """Every hold record for this watch: this PC's holds.json, plus what a
    friend's own holder reported from their PC."""
    try:
        ledger = json.loads((Path(__file__).resolve().parent / "holds.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ledger = []
    mine = [e for e in ledger if e.get("sub_id") == sub_id]
    with db() as c:
        for r in c.execute("SELECT data FROM remote_ledger WHERE sub_id=? ORDER BY id", (sub_id,)).fetchall():
            try:
                mine.append(json.loads(r["data"]))
            except ValueError:
                pass
    mine.sort(key=lambda e: (e.get("day", ""), e.get("at", "")))
    return mine


def ledger_entry(sub_id):
    try:
        ledger = json.loads((Path(__file__).resolve().parent / "holds.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mine = [e for e in ledger if e.get("sub_id") == sub_id]
    return mine[-1] if mine else None


def group_locked(sub):
    """No joining or changing seats while a hold is asked for, running or done."""
    status, _ = hold_outcome(sub)
    return status in ("pending", "approved", "triggered", "held", "booked")


def group_view(g, me):
    """What a member or the organiser sees of a group."""
    with db() as c:
        row = c.execute("SELECT * FROM subs WHERE id=?", (g["sub_id"],)).fetchone()
    if not row:
        return None
    sub = dict(row)
    members = group_members(g)
    total = group_total(g, members)
    status, text = hold_outcome(sub)
    e = ledger_entry(sub["id"]) if status in ("held", "booked") else None
    amount = (e or {}).get("payable") or (e or {}).get("total") or 0
    per_seat = round(float(amount) / total, 2) if amount and total else 0
    try:
        pretty = datetime.strptime(sub["date_code"], "%Y%m%d").strftime("%a %d %b")
    except ValueError:
        pretty = sub["date_code"]
    show = watched_show(sub) if sub.get("session") else None
    base = setting("public_url", "").rstrip("/")
    org = me == g["organizer"]
    out = {"code": g["code"], "organizer": g["organizer"], "me_organizer": org,
           "link": f"{base or ''}/tools?join={g['code']}#home",
           "venue": sub["venue_name"], "date": pretty, "what": describe(sub),
           "movie": (show or {}).get("movie") or sub.get("movie") or "",
           "time": (show or {}).get("time") or "",
           "org_seats": g["seats"], "total": total, "locked": group_locked(sub),
           "status": status, "status_text": text, "seats_text": (e or {}).get("seats") or "",
           "amount": amount, "per_seat": per_seat, "upi": g["upi"] if (org or status == "booked") else "",
           "members": [{"name": m["name"], "seats": m["seats"], "paid": bool(m["paid"]),
                        "share": round(per_seat * m["seats"]) if per_seat else 0,
                        "me": m["name"] == me} for m in members]}
    return out


def group_alert(g, sub, status):
    """Tell every member (with a topic) what happened to the group's hold."""
    view = group_view(g, "")
    if not view:
        return
    what = f"{view['movie'] or sub['venue_name']} {view['time']}".strip()
    for m in group_members(g):
        if not m["topic"]:
            continue
        share = round(view["per_seat"] * m["seats"]) if view["per_seat"] else 0
        msg = {
            "held": f"Seats held for your group: {view['seats_text'] or str(view['total']) + ' seats'}.\n"
                    f"{g['organizer']} is completing the booking. You'll get your share once it's booked.",
            "booked": f"Booked! {view['seats_text']}\nYour {m['seats']} seat(s)"
                      + (f": Rs {share}. Pay {g['organizer']} back from the site (UPI button)." if share
                         else f". Settle up with {g['organizer']}."),
            "found": f"Test run: seats found for the group ({view['seats_text']}), nothing reserved.",
        }.get(status, f"The group's auto-hold didn't go through ({status}). {g['organizer']} can try again.")
        push_json(m["topic"], f"Group booking: {what}", f"{sub['venue_name']} · {view['date']}\n{msg}",
                  view["link"] if view["link"].startswith("http") else "",
                  [{"action": "view", "label": "Open group", "url": view["link"]}] if view["link"].startswith("http") else None)


SCREENSHOT_DAYS = 30      # hold / QR / ticket screenshots older than this are deleted
BACKUP_KEEP = 14          # nightly copies of the database and config


def expire_and_retry():
    """Pending requests past their expiry lapse; failed holds with retries left go again."""
    with db() as c:
        subs = [dict(r) for r in c.execute(
            "SELECT * FROM subs WHERE hold_status IN ('pending','triggered')").fetchall()]
    for sub in subs:
        opts = json.loads(sub.get("hold_opts") or "{}")
        if sub["hold_status"] == "pending" and opts.get("expire_min"):
            try:
                age = (datetime.now() - datetime.fromisoformat(sub["hold_at"])).total_seconds()
            except (TypeError, ValueError):
                continue
            if age > opts["expire_min"] * 60:
                with LOCK, db() as c:
                    n = c.execute("UPDATE subs SET hold_status='expired', hold_code='' WHERE id=? AND hold_status='pending'",
                                  (sub["id"],)).rowcount
                if n:
                    push(sub["topic"], "Auto-hold request expired",
                         f"{sub['venue_name']} · {describe(sub)}\nIt wasn't approved in time. You can request it again.")
                    log_activity("expired", sub["hold_by"] or "", f"{sub['venue_name']} {sub['date_code']}", sub["id"])
            continue
        if sub["hold_status"] == "triggered" and opts.get("retries"):
            status, text = hold_outcome(sub)
            if status in ("failed", "noanswer") and opts.get("retried", 0) < opts["retries"]:
                opts["retried"] = opts.get("retried", 0) + 1
                with LOCK, db() as c:
                    n = c.execute("UPDATE subs SET hold_status='approved', hold_opts=? WHERE id=? AND hold_status='triggered'",
                                  (json.dumps(opts), sub["id"])).rowcount
                if n:
                    push(sub["topic"], f"Trying the auto-hold again ({opts['retried']} of {opts['retries']})",
                         f"{sub['venue_name']} · {describe(sub)}\nLast try: {text}")
                    log_activity("retry", sub["hold_by"] or "", f"{sub['venue_name']} {sub['date_code']} · "
                                                                 f"try {opts['retried']} of {opts['retries']}", sub["id"])


def housekeeping_daily():
    here = Path(__file__).resolve().parent
    cut = time.time() - SCREENSHOT_DAYS * 86400
    gone = 0
    for f in (here / "captures" / "holds").glob("*.png"):
        try:
            if f.stat().st_mtime < cut:
                f.unlink()
                gone += 1
        except OSError:
            pass
    if gone:
        print(f"  deleted {gone} screenshot(s) older than {SCREENSHOT_DAYS} days")
    out = here / "backups"
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    try:
        with sqlite3.connect(DB) as src, sqlite3.connect(out / f"bms_server-{stamp}.db") as dst:
            src.backup(dst)
        (out / f"watch_config-{stamp}.json").write_bytes((here / "watch_config.json").read_bytes())
    except (OSError, sqlite3.Error) as e:
        print("  backup failed:", e)
    for pattern in ("bms_server-*.db", "watch_config-*.json"):
        for old in sorted(out.glob(pattern))[:-BACKUP_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass


def group_poller():
    """Watch each group's hold and message the members once per outcome. Also runs
    request expiry / retries every pass and the daily clean-up and backup."""
    last_day = ""
    while True:
        time.sleep(10)
        try:
            expire_and_retry()
            today = datetime.now().strftime("%Y%m%d")
            if today != last_day and datetime.now().hour >= 3:
                last_day = today
                housekeeping_daily()
        except Exception:
            traceback.print_exc()
        try:
            with db() as c:
                groups = [dict(r) for r in c.execute("SELECT * FROM groups").fetchall()]
            for g in groups:
                with db() as c:
                    row = c.execute("SELECT * FROM subs WHERE id=?", (g["sub_id"],)).fetchone()
                if not row:
                    continue
                status, _ = hold_outcome(dict(row))
                key = f"{status}:{row['hold_at']}"
                if status in GROUP_DONE and g["notified"] != key:
                    with LOCK, db() as c:
                        c.execute("UPDATE groups SET notified=? WHERE id=?", (key, g["id"]))
                    if group_members(g):
                        group_alert(g, dict(row), status)
        except Exception:
            traceback.print_exc()


# ---------------------------------------------------------------- matching
def wanted(show, sub):
    if sub.get("session"):
        return str(show.get("session_id") or "") == str(sub["session"])
    scr = (sub.get("screen") or "").strip().upper()
    if scr:
        blob = " ".join(str(show.get(k) or "")
                        for k in ("attributes", "screen", "dimension")).upper()
        # whole-name match: "SCREEN 1" must not match "SCREEN 10", while a format
        # like "DOLBY" still matches "BARCO FLAGSHIP LASER DOLBY ATMOS"
        if not re.search(r"(?<![A-Z0-9])" + re.escape(scr) + r"(?![A-Z0-9])", blob):
            return False
    mov = (sub.get("movie") or "").strip().upper()
    if mov and mov not in str(show.get("movie") or "").upper():
        return False
    return True


def watch_shows(sub):
    """Showtimes a many-show watch covers, from the last poll (for its seat-map picker)."""
    if sub.get("session"):
        return []
    live = LIVE.get(sub["url"]) or {}
    return [{"session": x["session_id"], "time": x["time"], "movie": x.get("movie"),
             "screen": x.get("attributes") or x.get("screen") or "", "cats": x["cats"]}
            for x in live.get("shows") or [] if wanted(x, sub)][:40]


def watch_cats(sub):
    """Category names on the shows a watch covers (for the pick-a-category chips)."""
    live = LIVE.get(sub["url"]) or {}
    names = []
    for s in live.get("shows") or []:
        if wanted(s, sub):
            for c in s["cats"]:
                if c["name"] not in names:
                    names.append(c["name"])
    return names


def describe(sub):
    if sub.get("session"):
        sf = seat_filter_of(sub)
        return "one showtime" + (f" · seat alert: {seat_filter_text(sf)}" if sf else "")
    return " · ".join([sub.get("screen") or "all screens",
                       sub.get("movie") or "all movies"])


# ---------------------------------------------------------------- poller
# ---------------------------------------------------------------- live status
# What the poller last saw on each watched page, so every watch can show
# "checked 8s ago · 04:10 PM · GOLD Rs 390 filling fast". In memory: it refills
# within one poll (~10s) after a restart.
LIVE = {}
CAT_STATE = {"2": "available", "1": "filling fast", "0": "sold out"}


def plain_detail(text):
    """'GOLD went 0 -> 2' -> 'GOLD sold out → available' (BMS availability codes)."""
    words = {"0": "sold out", "1": "filling fast", "2": "available"}
    return re.sub(r"\b(\d) -> (\d)\b",
                  lambda m: f"{words.get(m.group(1), m.group(1))} → {words.get(m.group(2), m.group(2))}",
                  str(text or ""))


def live_show(s):
    """The bits of a parsed show that the status line and the pills need."""
    names, prices = s.get("category_names") or {}, s.get("category_prices") or {}
    cats = []
    for code, status in (s.get("categories") or {}).items():
        try:
            price = int(float(prices.get(code) or 0))
        except (TypeError, ValueError):
            price = 0
        cats.append({"name": names.get(code) or code, "price": price,
                     "state": CAT_STATE.get(str(status), "sold out" if str(status) in CLOSED else "available")})
    # BMS sometimes splits one category into blocks ("GOLD" and "GOLD."): show it
    # once, with the most room any block has
    rank = {"available": 0, "filling fast": 1, "sold out": 2}
    merged = {}
    for c in cats:
        key = (c["name"].rstrip(". ").upper(), c["price"])
        if key not in merged or rank[c["state"]] < rank[merged[key]["state"]]:
            merged[key] = {**c, "name": c["name"].rstrip(". ")}
    cats = list(merged.values())
    return {"session_id": str(s.get("session_id") or ""), "event_code": s.get("event_code") or "",
            "movie": s.get("movie"), "time": s.get("show_time"),
            "screen": s.get("screen"), "attributes": s.get("attributes"), "dimension": s.get("dimension"),
            "avail": s.get("avail"), "categories": s.get("categories") or {}, "cats": cats}


def cats_text(cats, limit=3):
    order = {"available": 0, "filling fast": 1, "sold out": 2}
    parts = [f"{c['name']}{' Rs ' + str(c['price']) if c['price'] else ''} {c['state']}"
             for c in sorted(cats, key=lambda c: (order[c["state"]], -c["price"]))[:limit]]
    return ", ".join(parts)


def live_status(sub):
    """{"at": epoch, "state": open|filling|sold|none|closed|waiting, "text": ...} for one watch."""
    live = LIVE.get(sub["url"])
    if not live:
        return {"at": None, "state": "waiting", "text": "first check in a few seconds"}
    if not live["date_open"]:
        return {"at": live["at"], "state": "closed", "text": "this date isn't open for booking yet"}
    mine = [s for s in live["shows"] if wanted(s, sub)]
    if not mine:
        return {"at": live["at"], "state": "none", "text": "no matching show listed yet"}
    def state(s):
        st = {c["state"] for c in s["cats"]}
        return "open" if "available" in st else "filling" if "filling fast" in st else "sold"
    states = [state(s) for s in mine]
    overall = "open" if "open" in states else "filling" if "filling" in states else "sold"
    if len(mine) == 1:
        s = mine[0]
        seat = SEAT_LIVE.get(sub["id"]) if seat_filter_of(sub) else None
        if seat:
            return {"at": seat["at"], "state": seat["state"], "text": f"{s['time']}: {seat['text']}"}
        return {"at": live["at"], "state": overall, "text": f"{s['time']}: {cats_text(s['cats']) or 'no categories'}"}
    with_seats = sum(st != "sold" for st in states)
    first = next((s for s, st in zip(mine, states) if st != "sold"), None)
    return {"at": live["at"], "state": overall,
            "text": f"{len(mine)} shows, {with_seats} with seats"
                    + (f", next with seats {first['time']}" if first else ", all sold out")}


BACKOFF = {"n": 0, "until": 0.0}      # pause after BookMyShow refuses pages (rate limit)


BUDGET_PER_MIN = 30        # most BookMyShow requests in any minute, from the whole website
BUDGET_GAP = 1.0           # seconds between two requests (never a burst)
LOW_SHARE = 0.5            # background work (the movie-list sweep) only uses half the minute
BUDGET = {"lock": threading.Lock(), "times": [], "last": 0.0, "hour": [], "refused_at": 0.0, "alerted": 0.0}
_LOW = threading.local()


class low_priority:
    """with low_priority(): ... marks background requests (movie-list sweep, film
    details): they wait while the minute is half used, so watches go first."""
    def __enter__(self):
        _LOW.on = True

    def __exit__(self, *a):
        _LOW.on = False


def budget_take(max_wait=20.0):
    """Wait for a slot in the request budget. False = none within max_wait (or we're
    backing off after a refusal): the caller skips the request / uses its cache."""
    low = getattr(_LOW, "on", False)
    if getattr(FRESH, "wait", None):
        max_wait = min(max_wait, FRESH.wait)
    give_up = time.time() + (600 if low else max_wait)   # background work just waits its turn
    cap = BUDGET_PER_MIN * (LOW_SHARE if low else 1)
    while True:
        now = time.time()
        if now < BACKOFF["until"]:
            return False
        with BUDGET["lock"]:
            BUDGET["times"] = [t for t in BUDGET["times"] if now - t < 60]
            wait = max(BUDGET["last"] + BUDGET_GAP - now,
                       (BUDGET["times"][0] + 60 - now) if len(BUDGET["times"]) >= cap else 0)
            if wait <= 0:
                BUDGET["times"].append(now)
                BUDGET["last"] = now
                BUDGET["hour"] = [t for t in BUDGET["hour"] if now - t < 3600] + [now]
                return True
        if now + wait > give_up:
            return False
        time.sleep(min(wait, 1.0))


def budget_refused():
    """BookMyShow refused a request: back off 15s, 30s, ... 15 min, and tell the owner once."""
    BACKOFF["n"] += 1
    BACKOFF["until"] = time.time() + min(900, 15 * 2 ** (BACKOFF["n"] - 1))
    BUDGET["refused_at"] = time.time()
    if time.time() - BUDGET["alerted"] > 1800:
        BUDGET["alerted"] = time.time()
        topic = owner_cfg().get("ntfy_topic")
        if topic:
            try:
                push(topic, "BookMyShow is refusing requests",
                     f"The website got a refusal (rate limit) from BookMyShow. All its checks are paused "
                     f"for {int(BACKOFF['until'] - time.time())}s and back off further if it continues.\n"
                     f"Avoid browsing BookMyShow from this PC for a while. Details on the admin page.")
            except Exception:
                pass


def budget_state():
    now = time.time()
    return {"last_min": len([t for t in BUDGET["times"] if now - t < 60]),
            "last_hour": len([t for t in BUDGET["hour"] if now - t < 3600]),
            "per_min": BUDGET_PER_MIN,
            "refused_at": BUDGET["refused_at"] or None,
            "paused_for": max(0, int(BACKOFF["until"] - now))}


def _guard_fetches():
    """Every BookMyShow request the website makes goes through HttpClient.fetch:
    it takes a slot in the budget first, and a refusal backs everything off."""
    if getattr(HttpClient, "_guarded", False):
        return
    plain = HttpClient.fetch

    def fetch(self, url, as_json=True):
        if not budget_take():
            return None
        out = plain(self, url, as_json)
        if out is None:
            budget_refused()
        else:
            BACKOFF["n"] = 0
        return out
    HttpClient.fetch = fetch
    HttpClient._guarded = True


_guard_fetches()


def poll_once(client, only=None):
    """One check of every watched cinema-day page (or just the urls in `only`)."""
    with db() as c:
        subs = [dict(r) for r in c.execute("SELECT * FROM subs").fetchall()]
    if not subs:
        return
    by_url = {}
    for s in subs:
        if only is None or s["url"] in only:
            by_url.setdefault(s["url"], []).append(s)

    for url, watchers in by_url.items():
        if time.time() < BACKOFF["until"]:
            return                             # BookMyShow asked us to slow down
        try:
            html = client.fetch(url, as_json=False)
            if not html:
                return                         # refused / failed: the fetch guard backs off
            remember_page(url, html)
            want = watchers[0]["date_code"]
            served = page_date(html)
            prev = load_snapshot(url)
            if want and served and served != want:
                save_snapshot(url, {**(prev or {}), "__date_open__": False})
                LIVE[url] = {"at": time.time(), "date_open": False, "shows": []}
                continue                       # date not open for booking yet
            shows = parse_cinema_page(html)
            LIVE[url] = {"at": time.time(), "date_open": True, "shows": [live_show(s) for s in shows]}
            if not shows:
                if served == want and prev and prev.get("__date_open__") is False:
                    for topic in {w["topic"] for w in watchers}:
                        push(topic, "Date available",
                             f"{watchers[0]['venue_name']}\n{want}\nDate is now listed; no shows yet.", url)
                    for w in watchers:
                        log_alert(w["id"], "DATE_OPEN", "date listed on BookMyShow, no shows yet")
                    log_opening("DATE_OPEN", "Date listed", watchers[0]["venue"],
                                watchers[0].get("venue_name"), want, key=f"{watchers[0]['venue']}|{want}|DATE")
                save_snapshot(url, {"__date_open__": served == want})
                continue
            # approved auto-holds: whenever a matching show has seats open and the
            # holder is live, send the hold request (before any alert goes out).
            # Covers shows that were already open and holders that were offline.
            if any(w.get("hold_status") == "approved" for w in watchers):
                for w in watchers:
                    if w.get("hold_status") == "approved" and target_live(w):
                        if seat_filter_of(w) and seat_maps_working():
                            SEAT_DUE.add(w["id"])     # held when its seats are free (seat_check)
                            continue
                        show = next((s for s in shows if wanted(s, w) and open_for_hold(s)), None)
                        if show:
                            publish_hold(w, show)

            first = prev is None
            prev = prev or {}
            events, snap = diff_shows(prev, shows, None, first_run=first)
            snap["__date_open__"] = True
            save_snapshot(url, snap)
            if first or not events:
                continue
            try:
                log_page_openings(watchers, want, events)
            except Exception as e:
                print("openings log error:", e)

            for w in watchers:
                mine = [e for e in events if wanted(e["show"], w)]
                if not mine:
                    continue
                if seat_filter_of(w) and seat_maps_working():
                    SEAT_DUE.add(w["id"])      # the seat check decides; a category change alone isn't enough
                    continue
                s0 = mine[0]["show"]
                link = url
                if s0.get("session_id") and s0.get("event_code"):
                    link = SEAT_PAGE.format(eventCode=s0["event_code"],
                                            venueCode=w["venue"],
                                            sessionId=s0["session_id"],
                                            dateCode=w["date_code"])
                title = f"{s0.get('movie') or 'Show'} - {s0.get('show_time')}"
                lines = [w.get("venue_name") or w["venue"], ""]
                for e in mine[:8]:
                    sh = e["show"]
                    lines.append(f"{sh.get('show_time')} [{sh.get('attributes')}]")
                    lines.append(f"   {plain_detail(e['detail'])}")
                    log_alert(w["id"], e.get("type", "change"),
                              f"{sh.get('show_time')}: {plain_detail(e['detail'])}")
                lines += ["", link]
                watch_alert(w, title, "\n".join(lines), link)
                print(f"  alerted {w['topic'][:18]}: {len(mine)} event(s)")
        except Exception as e:
            print("poll error:", e)
            traceback.print_exc()


# ---------------------------------------------------------------- track a movie everywhere
MWATCH_INTERVAL = 300      # seconds between checks; one request per movie + one per open date


def mwatch_matches(venues, areas, formats):
    """Cinemas (and their matching shows) that fit the chosen areas and formats."""
    areas = [a.upper() for a in areas or []]
    formats = [f.upper() for f in formats or []]
    out = []
    for v in venues or []:
        if areas and not any(a in (v.get("area") or v.get("name") or "").upper() for a in areas):
            continue
        shows = [s for s in v["shows"]
                 if not formats or any(f in f"{s.get('screen', '')} {s.get('format', '')}".upper() for f in formats)]
        if shows:
            out.append({"code": v["code"], "name": v["name"], "area": v.get("area", ""),
                        "shows": [{"time": s.get("time"), "screen": s.get("screen"), "open": s.get("open")}
                                  for s in shows]})
    return out


def mwatch_describe(w):
    areas, formats = json.loads(w["areas"] or "[]"), json.loads(w["formats"] or "[]")
    when = "any date" if not w["date"] else datetime.strptime(w["date"], "%Y%m%d").strftime("%a %d %b")
    return " · ".join([when, ", ".join(areas) or "all areas", ", ".join(formats) or "any format"])


def mwatch_check(w, dates_cache, shows_cache, alert=True):
    """Check one movie watch; alert on newly opened dates / newly matching cinemas.
    Returns its summary for the watch list. alert=False just records a baseline."""
    movie = {"code": w["code"], "slug": w["slug"]}
    state = json.loads(w["state"] or "{}")
    alert = alert and "checked" in state             # the first check is just a baseline
    seen = state.get("seen", {})                     # date -> [cinema codes already reported]
    if w["code"] not in dates_cache:
        dates_cache[w["code"]] = movie_dates(movie, max_age=60) or []
    open_dates = [d["code"] for d in dates_cache[w["code"]] if d["open"]]
    today = datetime.now().strftime("%Y%m%d")
    targets = [w["date"]] if w["date"] else [d for d in open_dates if d >= today][:7]
    areas, formats = json.loads(w["areas"] or "[]"), json.loads(w["formats"] or "[]")
    found, news = {}, []
    for d in targets:
        if d not in open_dates:
            continue
        key = (w["code"], d)
        if key not in shows_cache:
            shows_cache[key] = movie_shows({"code": w["code"], "slug": w["slug"]}, d) or []
        m = mwatch_matches(shows_cache[key], areas, formats)
        found[d] = m
        new = [v for v in m if v["code"] not in seen.get(d, [])]
        if new:
            news.append((d, new, d not in seen))
        seen[d] = sorted(set(seen.get(d, [])) | {v["code"] for v in m})
    state.update(seen=seen, checked=time.time(),
                 summary={d: len(m) for d, m in found.items()},
                 bookable=bool(open_dates))
    with LOCK, db() as c:
        c.execute("UPDATE mwatch SET state=? WHERE id=?", (json.dumps(state), w["id"]))
    if alert:
        for d, new, first in news:
            mwatch_alert(w, d, new, first)
    return state


def mwatch_alert(w, date, venues, first):
    when = datetime.strptime(date, "%Y%m%d").strftime("%a %d %b")
    title = (f"{w['title']}: bookings open {when}" if first
             else f"{w['title']}: {len(venues)} more cinema(s) {when}")
    lines = []
    for v in venues[:10]:
        times = ", ".join(f"{s['time']}{' (' + s['screen'] + ')' if s.get('screen') else ''}"
                          for s in v["shows"][:4])
        lines.append(f"{v['name']}\n   {times}")
    if len(venues) > 10:
        lines.append(f"...and {len(venues) - 10} more")
    link = f"https://in.bookmyshow.com/movies/hyderabad/{w['slug']}/buytickets/{w['code']}/{date}"
    base = setting("public_url", "").rstrip("/")
    actions = []
    if base and w["token"]:
        actions.append({"action": "http", "label": "Stop watching", "method": "POST", "clear": True,
                        "url": f"{base}/api/admin/mwatch-stop", "headers": {"Content-Type": "application/json"},
                        "body": json.dumps({"id": w["id"], "token": w["token"]})})
    push_json(w["topic"], title, "\n".join(lines) + f"\n\n{mwatch_describe(w)}", link, actions,
              priority=2 if quiet_now(w.get("owner")) else 5)
    STATE["alerts"] += 1
    log_opening("BOOKINGS", (f"Bookings open at {len(venues)} cinema{'s' if len(venues) != 1 else ''}" if first
                             else f"{len(venues)} more cinema{'s' if len(venues) != 1 else ''}"),
                "", ", ".join(v["name"] for v in venues[:2]) + (f" +{len(venues) - 2}" if len(venues) > 2 else ""),
                date, w["title"], "", w["code"], key=f"mw|{w['code']}|{date}|{len(venues)}")
    log_alert(-w["id"], "NEW_SHOW", f"{when}: " + ", ".join(v["name"] for v in venues[:6])
              + (f" +{len(venues) - 6} more" if len(venues) > 6 else ""))


def mwatch_poller():
    while True:
        time.sleep(MWATCH_INTERVAL)
        try:
            if setting("paused", "0") == "1":
                continue
            with db() as c:
                watches = [dict(r) for r in c.execute("SELECT * FROM mwatch").fetchall()]
            dates_cache, shows_cache = {}, {}
            today = datetime.now().strftime("%Y%m%d")
            for w in watches:
                if w["date"] and w["date"] < today:
                    with LOCK, db() as c:          # its date has passed
                        c.execute("DELETE FROM mwatch WHERE id=?", (w["id"],))
                    continue
                mwatch_check(w, dates_cache, shows_cache)
        except Exception:
            traceback.print_exc()


# ---------------------------------------------------------------- seat maps & seat alerts
# Seat maps are decrypted inside BookMyShow's own page, so they come from the
# tracker's Chrome (seat_maps.py: one extra tab, views only, never holds).
SEATMAPS = None
SEAT_FAST = 3              # seconds between seat checks of the most urgent seat-alert watch
SEAT_FAST_MAX = 1          # ...one watch at a time (one with an approved auto-hold first)
SEAT_EVERY = 30            # every other seat-alert watch
SEAT_MIN_GAP = 3           # never sooner than this, even when its categories keep changing
SEAT_DUE = set()           # watch ids to check now (a category on their show changed)
SEAT_LIVE = {}             # watch id -> {"at", "state", "text"} for the live line
SEAT_LAST_ALERT = {}


SHOWPAGES = {}             # cinema day-page url -> (at, parsed shows), for the showtime picker


def cinema_show(code, date, session, max_age=60):
    """(venue row, parsed show) for one showtime, from a cached cinema day page."""
    with db() as c:
        v = c.execute("SELECT * FROM venues WHERE code=?", (code,)).fetchone()
    if not v:
        return None, None
    _, html = cinema_page(code, date, max_age=max(max_age, 120))
    shows = parse_cinema_page(html) if html and page_date(html) == date else []
    return dict(v), next((x for x in shows if str(x.get("session_id")) == str(session)), None)


def seat_maps():
    global SEATMAPS
    if SEATMAPS is None:
        from seat_maps import SeatMaps
        try:
            port = int(json.loads(Path("watch_config.json").read_text()).get("cdp_port", 9222))
        except Exception:
            port = 9222
        SEATMAPS = SeatMaps(port)
    return SEATMAPS


def seat_maps_working():
    """True unless seat maps have been failing (Chrome off): then seat-alert
    watches fall back to ordinary category alerts rather than going quiet."""
    m = seat_maps()
    return not m.last_error or time.time() - m.last_ok < 300


def seat_filter_of(sub):
    try:
        sf = json.loads(sub.get("seat_filter") or "null")
    except (TypeError, ValueError):
        return None
    return sf if isinstance(sf, dict) and sf.get("together") else None


def seat_filter_text(sf):
    bits = [f"{sf['together']} together"]
    if sf.get("categories"):
        bits.append(", ".join(sf["categories"]))
    if sf.get("rows"):
        bits.append("rows " + " ".join(sf["rows"]))
    return " · ".join(bits)


def clean_seat_filter(b):
    try:
        n = int(b.get("together") or 2)
    except (TypeError, ValueError):
        return None, "Seats together must be a number."
    if not 1 <= n <= 10:
        return None, "Seats together must be between 1 and 10."
    as_list = lambda v: v if isinstance(v, list) else str(v or "").split(",")
    cats = [str(c).strip().upper()[:30] for c in as_list(b.get("categories")) if str(c).strip()][:8]
    rows = [str(r).strip().upper() for r in as_list(b.get("rows")) if str(r).strip()]
    rows = [r for r in rows if re.fullmatch(r"[A-Z]{1,2}", r)][:20]
    return {"together": n, "categories": cats, "rows": rows}, ""


def watched_show(sub):
    """The live show a single-showtime watch is on (from the last poll), or None."""
    live = LIVE.get(sub["url"])
    if not live or not sub.get("session"):
        return None
    return next((s for s in live["shows"] if s["session_id"] == str(sub["session"])), None)


def seat_url(sub, show):
    return (f"https://in.bookmyshow.com/movies/{(sub.get('region') or 'HYD').lower()}/seat-layout/"
            f"{show['event_code']}/{sub['venue']}/{sub['session']}/{sub['date_code']}")


def seat_check(sub, show, first=False):
    """Load the show's seat map and alert on seats that newly fit the watch's filter."""
    from seat_maps import blocks_for
    sf = seat_filter_of(sub)
    url = seat_url(sub, show)
    hit = seat_maps().cache.get(url)
    if not (hit and time.time() - hit[0] <= 2) and not budget_take(max_wait=5):
        return                                   # budget spent / backing off: try next round
    layout = seat_maps().get(url, max_age=2)
    if not layout:
        SEAT_LIVE[sub["id"]] = {"at": time.time(), "state": "none",
                                "text": f"seat map unavailable ({seat_maps().last_error}); category alerts meanwhile"}
        return
    blocks = blocks_for(layout, sf["together"], sf.get("categories"), sf.get("rows"))
    if sub.get("hold_status") == "approved":
        opts = json.loads(sub.get("hold_opts") or "{}")
        need = max(int(opts.get("qty") or sf["together"]), 1)
        cats, rows = opts.get("categories") or sf.get("categories"), opts.get("rows") or sf.get("rows")
        if blocks_for(layout, need, cats, rows) and publish_hold(sub, {**show, "show_time": show["time"]}):
            print(f"  seat-alert hold #{sub['id']}: {need} together free in its seats, hold sent")
    ids = sorted({i for b in blocks for i in b[3]})
    names = [f"{c} {name}" for c, _, name, _ in blocks]
    SEAT_LIVE[sub["id"]] = {"at": time.time(), "state": "open" if blocks else "sold",
                            "text": (f"{len(blocks)} block(s) fit: " + ", ".join(names[:4])
                                     + (" …" if len(names) > 4 else "")) if blocks
                            else f"no {sf['together']} together in your seats right now"}
    try:
        seen = set(json.loads(sub.get("seat_seen") or "[]"))
    except (TypeError, ValueError):
        seen = set()
    fresh = [i for i in ids if i not in seen]
    now = time.time()
    if fresh and now - SEAT_LAST_ALERT.get(sub["id"], 0) < 90:
        keep = sorted(set(ids) - set(fresh))   # too soon after the last alert: these alert next time
    else:
        keep = ids
        if fresh:
            SEAT_LAST_ALERT[sub["id"]] = now
            new_blocks = [f"{c} {name}" for c, _, name, bid in blocks if any(i in fresh for i in bid)]
            link = seat_url(sub, show)
            body = "\n".join([sub.get("venue_name") or sub["venue"], f"{show['time']} · {seat_filter_text(sf)}", "",
                              ("Free now: " if first else "Just freed up: ") + ", ".join(new_blocks[:10])
                              + (f" (+{len(new_blocks) - 10} more)" if len(new_blocks) > 10 else ""),
                              "", link])
            watch_alert(sub, f"Seats free: {show.get('movie') or 'Show'} {show['time']}", body, link)
            log_alert(sub["id"], "SEATS", f"{show['time']}: " + ", ".join(new_blocks[:6]))
            print(f"  seat alert #{sub['id']}: {', '.join(new_blocks[:3])}")
    with LOCK, db() as c:
        c.execute("UPDATE subs SET seat_seen=? WHERE id=?", (json.dumps(keep), sub["id"]))


def seat_poller():
    """Seat maps of seat-alert watches: the most urgent one every SEAT_FAST (3s),
    the rest every SEAT_EVERY, and any of them as soon as their show's categories
    change. All inside the request budget, which spaces them out when it's busy."""
    last = {}
    while True:
        time.sleep(1)
        try:
            if setting("paused", "0") == "1":
                continue
            with db() as c:
                subs = [dict(r) for r in c.execute(
                    "SELECT * FROM subs WHERE IFNULL(seat_filter,'') NOT IN ('','null')").fetchall()]
            subs.sort(key=lambda x: (x.get("hold_status") != "approved", x["id"]))
            fast_ids = {x["id"] for x in subs[:SEAT_FAST_MAX]}
            for sub in subs:
                show = watched_show(sub)
                if not show:
                    continue
                if not open_for_hold(show):
                    SEAT_LIVE[sub["id"]] = {"at": time.time(), "state": "sold", "text": "sold out / not open"}
                    continue
                since = time.time() - last.get(sub["id"], 0)
                every = SEAT_FAST if sub["id"] in fast_ids else SEAT_EVERY
                if since < SEAT_MIN_GAP or (sub["id"] not in SEAT_DUE and since < every):
                    continue
                SEAT_DUE.discard(sub["id"])
                first = not sub.get("seat_seen")
                last[sub["id"]] = time.time()
                seat_check(sub, show, first=first)
        except Exception:
            traceback.print_exc()


FAST_SECONDS = 3            # the page an approved auto-hold is waiting on is checked this often
FAST_MAX = 1                # ...one page at a time (the next ones every FAST_NEXT)
FAST_NEXT = 6
FAR_SECONDS = 120           # dates 2+ days away that are already open
DONE_SECONDS = 600          # today's pages whose shows have all started
WARM_FILE = Path(__file__).resolve().parent / "warm_venues.json"
WARM_QTY = Path(__file__).resolve().parent / "warm_qty.json"     # seats wanted per warm venue


def waiting_holds():
    """cinema-day url -> (venue code, seats wanted), for watches whose auto-hold is approved and waiting."""
    with db() as c:
        rows = c.execute("SELECT url, venue, hold_opts FROM subs WHERE hold_status='approved'").fetchall()
    out = {}
    for r in rows:
        try:
            qty = int(json.loads(r["hold_opts"] or "{}").get("qty") or 2)
        except (ValueError, TypeError):
            qty = 2
        out[r["url"]] = (r["venue"], qty)
    return out


def write_warm(urls):
    """Tell the holder which cinemas to keep a warm seat tab at (it re-reads every minute)."""
    want, qtys = {}, {}
    for url, (venue, qty) in urls.items():
        want.setdefault(venue, url)
        qtys.setdefault(venue, qty)
    try:
        if json.loads(WARM_QTY.read_text(encoding="utf-8")) != qtys:
            WARM_QTY.write_text(json.dumps(qtys), encoding="utf-8")
    except (OSError, ValueError):
        WARM_QTY.write_text(json.dumps(qtys), encoding="utf-8")
    try:
        old = json.loads(WARM_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = None
    if old != want:
        WARM_FILE.write_text(json.dumps(want), encoding="utf-8")


def show_minutes(t):
    m = re.match(r"(\d{1,2}):(\d{2})\s*([AP]M)", str(t or ""), re.I)
    return (int(m[1]) % 12 + (12 if m[3].upper() == "PM" else 0)) * 60 + int(m[2]) if m else None


def page_interval(url, date, fast_rank, base):
    """How often this watched page is worth checking."""
    if fast_rank is not None:                   # an approved auto-hold is waiting on it
        return FAST_SECONDS if fast_rank < FAST_MAX else FAST_NEXT
    live = LIVE.get(url)
    if not live or not live.get("date_open"):
        return base                             # not released yet: catch the release
    today = datetime.now().strftime("%Y%m%d")
    try:
        days = (datetime.strptime(date, "%Y%m%d") - datetime.strptime(today, "%Y%m%d")).days
    except ValueError:
        days = 0
    if days >= 2:
        return FAR_SECONDS
    if date == today and live.get("shows"):
        now_min = datetime.now().hour * 60 + datetime.now().minute
        starts = [show_minutes(x.get("time")) for x in live["shows"]]
        if all(m is not None and m < now_min for m in starts):
            return DONE_SECONDS                 # every show has started: little left to catch
    return base


MAX_PAGES = 60          # distinct cinema-days watched at once (the owner's don't count)
MAX_FILMS = 25          # distinct films with a "track everywhere" watch
MAX_PER_IP = 30         # active watches made from one address
PER_PASS = 2            # pages checked per loop turn, so the 3s page never waits behind a crowd
POLL_LAG = {"late": 0, "worst": 0}


def owner_pages():
    topic = owner_cfg().get("ntfy_topic") or "-"
    with db() as c:
        return {r["url"] for r in c.execute("SELECT DISTINCT url FROM subs WHERE topic=?", (topic,)).fetchall()}


def watched_pages():
    with db() as c:
        return {r["url"] for r in c.execute("SELECT DISTINCT url FROM subs").fetchall()}


def poller():
    """Each watched page on its own schedule (page_interval): the page an approved
    auto-hold waits on every 3s, releases and today/tomorrow every 30s, later open
    dates every 2 min, finished days every 10 min. When BookMyShow's budget can't keep
    up, pages go in a fair order: auto-holds first, then the owner's, then whichever
    is most overdue, so a crowd slows everyone a little instead of starving some."""
    client = HttpClient({})
    last_purge, polled = 0, {}
    live = holder_state()["live"]
    while True:
        try:
            now = time.time()
            live = tell_askers_if_back(live)
            if now - last_purge > 3600:
                purge_stale()
                last_purge = now
            fast = waiting_holds()
            write_warm(fast)
            if setting("paused", "0") != "1":
                base = int(setting("interval", str(POLL_SECONDS)) or POLL_SECONDS)
                mine_topic = owner_cfg().get("ntfy_topic") or "-"
                with db() as c:
                    rows = c.execute("SELECT url, MAX(date_code) d, MAX(topic=?) mine FROM subs GROUP BY url",
                                     (mine_topic,)).fetchall()
                order = list(fast)
                due, late, worst = [], 0, 0
                for r in rows:
                    u = r["url"]
                    every = page_interval(u, r["d"], order.index(u) if u in fast else None, base)
                    waited = now - polled.get(u, 0)
                    if waited >= every:
                        tier = 0 if u in fast else 1 if r["mine"] else 2
                        due.append((tier, -waited / every, u))
                        if u in polled and waited > 2 * every:
                            late += 1
                            worst = max(worst, waited - every)
                POLL_LAG.update(late=late, worst=int(worst))
                if due:
                    pick = [u for _, _, u in sorted(due)[:PER_PASS]]
                    for u in pick:
                        polled[u] = now
                    poll_once(client, only=set(pick))
                    STATE["passes"] += 1
        except Exception:
            traceback.print_exc()
        time.sleep(0.5)


# ---------------------------------------------------------------- web
LANDING_PAGE = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Seat Watch</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@700;800&family=Figtree:wght@400;500;600;700&display=swap">
<style>
:root{--ink:#1d1a21;--soft:#5d5866;--paper:#f3f2ef;--card:#ffffff;--line:#dedbe2;
  --velvet:#a3123a;--velvet-dark:#7c0d2c;--free:#1f8a5b;--sold:#cfcbd4;--held:#e0a526;
  --display:"Big Shoulders Display","Arial Narrow",Impact,sans-serif;--body:Figtree,ui-sans-serif,system-ui,"Segoe UI",sans-serif}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 var(--body)}
.page{max-width:1440px;margin:0 auto;padding:0 clamp(16px,4vw,56px)}
a{color:inherit}

/* top bar */
.bar{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:22px 0;border-bottom:2px solid var(--ink)}
.word{font:800 30px/1 var(--display);letter-spacing:.04em;text-transform:uppercase}
.word b{color:var(--velvet)}
.bar small{font-size:13px;color:var(--soft);text-align:right}

/* hero */
.hero{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(320px,.65fr);gap:clamp(24px,4vw,64px);align-items:end;padding:clamp(36px,6vw,80px) 0 40px}
.kicker{font-weight:700;font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:var(--velvet)}
h1{font:800 clamp(64px,10vw,150px)/.86 var(--display);letter-spacing:.005em;text-transform:uppercase;margin:14px 0 22px}
h1 span{color:var(--velvet)}
.lead{font-size:clamp(17px,1.5vw,20px);color:var(--soft);max-width:60ch;margin:0}
.lead b{color:var(--ink);font-weight:600}

/* the name form, as a ticket stub */
.stub{position:relative;background:var(--card);border-radius:14px;padding:26px 26px 24px;box-shadow:0 1px 0 var(--line),0 18px 40px rgba(29,26,33,.10)}
.stub .perf{position:relative;border-top:2px dashed var(--line);margin:18px -26px 20px}
.stub .perf:before,.stub .perf:after{content:"";position:absolute;top:-12px;width:22px;height:22px;border-radius:50%;background:var(--paper)}
.stub .perf:before{left:-11px}.stub .perf:after{right:-11px}
.admit{display:flex;justify-content:space-between;align-items:baseline;font:800 26px/1 var(--display);letter-spacing:.06em;text-transform:uppercase}
.admit small{font:600 12px var(--body);letter-spacing:.12em;color:var(--soft)}
.stub p{margin:8px 0 0;color:var(--soft);font-size:14px}
label{display:block;font-size:13px;font-weight:700;margin-bottom:8px}
input{width:100%;padding:14px 15px;border:1.5px solid var(--line);border-radius:10px;font:inherit;background:#fff;color:var(--ink)}
input:focus{outline:none;border-color:var(--velvet);box-shadow:0 0 0 3px rgba(163,18,58,.15)}
button{width:100%;margin-top:12px;border:0;border-radius:10px;padding:15px 18px;background:var(--velvet);color:#fff;
  font:800 19px/1 var(--display);letter-spacing:.08em;text-transform:uppercase;cursor:pointer}
button:hover{background:var(--velvet-dark)}
button:focus-visible{outline:3px solid var(--ink);outline-offset:2px}
#msg{min-height:20px;margin-top:8px;font-size:13px;color:var(--soft)}

/* the hall: a real-looking seat map across the page */
.hall{background:var(--card);border-radius:14px;padding:22px clamp(14px,3vw,36px) 18px;box-shadow:0 1px 0 var(--line)}
.hall-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.hall-head b{font:800 22px/1 var(--display);letter-spacing:.05em;text-transform:uppercase}
.legend{display:flex;gap:16px;font-size:13px;color:var(--soft);flex-wrap:wrap}
.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-1px;margin-right:6px}
.seats{display:grid;gap:5px;overflow:hidden}
.srow{display:grid;grid-template-columns:22px 1fr 22px;align-items:center;gap:8px}
.srow em{font:700 12px var(--body);font-style:normal;color:var(--soft);text-align:center}
.sline{display:grid;grid-template-columns:repeat(var(--n),minmax(0,1fr));gap:4px}
.s{aspect-ratio:1.15;border-radius:4px 4px 2px 2px;background:var(--sold);max-height:22px}
.s.free{background:#fff;box-shadow:inset 0 0 0 1.5px var(--free)}
.s.new{background:var(--free)}
.s.held{background:var(--held)}
.s.gap{visibility:hidden}
.screen{margin:16px auto 0;width:60%;height:6px;border-radius:99px;background:linear-gradient(90deg,transparent,var(--sold) 20%,var(--sold) 80%,transparent)}
.screen-txt{text-align:center;font:700 11px var(--body);letter-spacing:.3em;color:var(--soft);margin-top:7px}

/* how it works: a real sequence, so numbered */
.steps{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:clamp(16px,2.5vw,32px);padding:48px 0 30px}
.step{border-top:2px solid var(--ink);padding-top:14px}
.step .n{font:800 46px/1 var(--display);color:var(--velvet)}
.step h3{font:800 26px/1.05 var(--display);letter-spacing:.03em;text-transform:uppercase;margin:6px 0 8px}
.step p{margin:0;color:var(--soft);max-width:44ch}
.facts{display:flex;flex-wrap:wrap;gap:10px 28px;padding:18px 0 40px;border-bottom:1px solid var(--line);font-size:14px;color:var(--soft)}
.facts b{color:var(--ink)}
footer{padding:22px 0 40px;color:var(--soft);font-size:13px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}

@media(max-width:860px){.hero{grid-template-columns:1fr;align-items:start}.steps{grid-template-columns:1fr}.bar small{display:none}}
@media(max-width:520px){.sline{gap:2px}.seats{gap:3px}.srow{grid-template-columns:16px 1fr 16px;gap:4px}}
</style></head><body>
<div class="page">
  <header class="bar">
    <div class="word">Seat<b>&#8202;Watch</b></div>
    <small>Hyderabad cinemas on BookMyShow · private, shared by invite</small>
  </header>

  <section class="hero">
    <div>
      <div class="kicker">Seat alerts and auto-hold</div>
      <h1>Seats open.<br><span>You hear first.</span></h1>
      <p class="lead">Pick a cinema, a date, a screen or a single show. Seat Watch checks BookMyShow around the clock and
        sends <b>your phone</b> an alert the moment seats open. Ask for an auto-hold and it can <b>hold the seats in about
        three seconds</b> while you pay from your own UPI app.</p>
    </div>
    <div class="stub">
      <div class="admit">Admit one <small>Your pass</small></div>
      <p>Tell the owner who you are. Your name is kept with your watches so only you see them.</p>
      <div class="perf" aria-hidden="true"></div>
      <form id="access"><label for="name">Your name</label>
        <input id="name" maxlength="40" autocomplete="name" placeholder="e.g. Ravi K" required>
        <button type="submit">Open Seat Watch</button><div id="msg" role="status"></div></form>
    </div>
  </section>

  <section class="hall" aria-label="How a seat map looks to the tracker">
    <div class="hall-head"><b>Screen 1 · 11:20 PM</b>
      <div class="legend"><span><i style="box-shadow:inset 0 0 0 1.5px var(--free);background:#fff"></i>free</span>
        <span><i style="background:var(--free)"></i>just freed up: you get an alert</span>
        <span><i style="background:var(--held)"></i>held for you</span><span><i style="background:var(--sold)"></i>sold</span></div></div>
    <div class="seats" id="seats"></div>
    <div class="screen" aria-hidden="true"></div><div class="screen-txt">SCREEN THIS WAY</div>
  </section>

  <section class="steps">
    <div class="step"><div class="n">1</div><h3>Pick what to watch</h3>
      <p>A whole cinema on a date that isn't listed yet, one screen like a Dolby or IMAX hall, one showtime, or even
        rows on the seat map.</p></div>
    <div class="step"><div class="n">2</div><h3>Get the alert</h3>
      <p>Alerts arrive through the free ntfy app the moment a category opens, a new show appears, or seats in your
        rows free up.</p></div>
    <div class="step"><div class="n">3</div><h3>Let it hold the seats</h3>
      <p>Approve an auto-hold and the best seats together are held to the payment page. You pay with your own UPI
        app, or book on your own account with your own holder.</p></div>
  </section>

  <div class="facts"><span><b>~100</b> Hyderabad cinemas</span><span>Checked every <b>30 s</b>, every <b>3 s</b> while an auto-hold waits</span>
    <span>Middle and back rows chosen first</span><span>Nothing is booked without your OK</span></div>

  <footer><span>Seat Watch is a private tool, not affiliated with BookMyShow.</span><span>Access is shared by the owner.</span></footer>
</div>
<script>
// a hall that looks like BookMyShow's: mostly sold, a few free, a few that just freed up
(function(){
  const rows='ABCDEFGHIJ'.split(''),n=24,box=document.getElementById('seats');
  let seed=7;const rnd=()=>(seed=(seed*9301+49297)%233280)/233280;
  box.innerHTML=rows.map((r,i)=>{
    let cells='';
    for(let c=0;c<n;c++){
      const aisle=c===5||c===18;let k='s';
      if(aisle)k+=' gap';
      else{const x=rnd();
        if(i===3&&c>=9&&c<=11)k+=' held';
        else if((i===6&&c>=12&&c<=14)||(i===1&&(c===20||c===21)))k+=' new';
        else if(x<(i>6?.32:.14))k+=' free';}
      cells+=`<span class="${k}"></span>`;
    }
    return `<div class="srow"><em>${r}</em><div class="sline" style="--n:${n}">${cells}</div><em>${r}</em></div>`;
  }).join('');
})();
document.getElementById('access').addEventListener('submit',async function(e){e.preventDefault();
  var name=document.getElementById('name').value.trim(),msg=document.getElementById('msg');
  if(name.length<2){msg.textContent='Please enter your name.';return}
  msg.textContent='Opening…';
  var r=await fetch('/api/access',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name})});
  var d=await r.json();if(d.ok){location.href='/tools'+location.search+'#home'}else{msg.textContent=d.error||'Could not open access.'}});
</script></body></html>"""


PAGE = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js" defer></script>
<title>Seat Watch</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@700;800&family=Figtree:wght@400;500;600;700&display=swap"><style>
:root{--house:#f3f2ef;--paper:#f3f2ef;--seat:#fff;--edge:#dedbe2;--marquee:#a3123a;--marquee-dark:#7c0d2c;
      --open:#1f8a5b;--dim:#5d5866;--ink:#1d1a21;
      --display:"Big Shoulders Display","Arial Narrow",Impact,sans-serif;--body:Figtree,ui-sans-serif,system-ui,"Segoe UI",sans-serif;
      --card-shadow:0 1px 0 var(--edge),0 10px 26px rgba(29,26,33,.06)}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:#fff;color:var(--ink);
 font:16px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace}
header{padding:24px 0;border-bottom:1px solid var(--edge);background:rgba(255,255,255,.86);
 backdrop-filter:blur(16px);box-shadow:0 8px 30px rgba(17,24,39,.06)}
.wrap{max-width:1440px;margin:0 auto;padding:0 clamp(16px,3.5vw,48px)}
h1{margin:0;font:800 34px/1 "Big Shoulders Display","Arial Narrow",Impact,sans-serif;letter-spacing:.04em;text-transform:uppercase;color:var(--ink)}
.sub{margin:9px 0 0;color:var(--dim);font-size:15px}
h2{font-size:16px;letter-spacing:0;color:var(--ink);margin:30px 0 10px}
label{display:block;font-size:13px;font-weight:600;color:var(--ink);margin:14px 0 6px}
select,input{width:100%;padding:13px;background:var(--seat);color:var(--ink);
 border:1px solid var(--edge);border-radius:9px;font-size:16px}
select:focus,input:focus{outline:2px solid var(--marquee);outline-offset:1px}
button{padding:13px 20px;border:0;border-radius:9px;font-size:15px;font-weight:700;
 background:var(--marquee);color:#fff;margin-top:16px}
button.g{background:transparent;color:var(--dim);border:1px solid var(--edge);
 font-weight:600;padding:9px 13px;font-size:13px;margin:0}
button.pick{background:transparent;color:var(--open);border:1px solid var(--open);
 font-weight:700;padding:8px 14px;font-size:13px;margin:0}
.row{display:flex;gap:9px;flex-wrap:wrap;align-items:center}
.show{background:rgba(255,255,255,.88);border:1px solid var(--edge);border-left:4px solid #3478f6;border-radius:14px;
 box-shadow:0 10px 28px rgba(17,24,39,.06);backdrop-filter:blur(14px);padding:12px 14px;margin-bottom:8px;display:flex;justify-content:space-between;
 align-items:center;gap:12px}
.show .t{font-size:17px}
.show .m{color:var(--dim);font-size:13px;margin-top:2px}
.free{color:var(--open)}.gone{color:var(--dim)}
.stub{position:relative;background:rgba(255,255,255,.88);border:1px solid var(--edge);
 border-left:4px solid #8255d9;border-radius:14px;box-shadow:0 10px 28px rgba(17,24,39,.06);backdrop-filter:blur(14px);padding:14px 82px 14px 15px;margin-bottom:9px}
.stub:before{content:"";position:absolute;top:0;bottom:0;right:68px;width:1px;
 background:var(--edge)}
.stub .v{font-size:10px;letter-spacing:.24em;text-transform:uppercase;color:var(--marquee)}
.stub .d{font-size:17px;margin-top:3px}
.stub .f{color:var(--dim);font-size:13px;margin-top:2px}
.x{position:absolute;right:12px;top:50%;transform:translateY(-50%)}
.hint{font-size:13px;color:var(--dim);margin-top:6px}
.ok{color:var(--open)}
.empty{border:1px dashed var(--edge);border-radius:14px;padding:22px;color:var(--dim);
 text-align:center;font-size:14px}
footer{color:var(--dim);font-size:12px;padding:26px 0 50px}
.home-link{color:inherit;text-decoration:none}.home-link b{color:var(--marquee)}.home-link:hover{color:var(--marquee)}
.topic-row{display:flex;gap:8px;flex-wrap:wrap}.topic-row input{flex:1;min-width:220px}.topic-row button{margin:0}
.setup{margin-top:10px;padding:12px 16px;border:1px solid var(--edge);border-radius:10px;background:#fafbfc;font-size:14px}
.setup ol{margin:0;padding-left:20px}.setup li{margin:6px 0}.setup button{margin:0}
.topic-qr{margin-top:8px}.topic-qr img,.topic-qr canvas{width:140px;height:140px}
.prefs{margin-top:22px;padding:14px 16px;border:1px solid var(--edge);border-radius:12px;background:#fff}
.prefs summary{cursor:pointer;font-weight:700}.prefs .fields{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.prefs label{margin:6px 0 4px}.prefs input,.prefs select{padding:9px;font-size:15px}
.star{display:inline-grid;place-items:center;width:26px;height:26px;border-radius:50%;color:#98a2b3;font-size:17px;line-height:1;cursor:pointer}
.star.on{color:#e0a100}.star:hover{background:#f2f4f7}
.venue-tile{position:relative}.venue-tile .star{position:absolute;top:6px;right:6px}
.chip{display:inline-flex;align-items:center;gap:4px;margin:0 6px 6px 0;padding:3px 10px;border:1px solid var(--edge);border-radius:99px;font-size:13px;color:var(--ink)}
@media(max-width:520px){.prefs .fields{grid-template-columns:1fr}}
.view{display:none}.view.on{display:block}
.choices{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:6px}
.choice{display:flex;flex-direction:column;gap:6px;padding:22px;border:1px solid var(--edge);border-radius:14px;background:#fff;
 color:var(--ink);text-decoration:none;box-shadow:0 8px 26px rgba(17,24,39,.05);transition:border-color .15s,transform .15s}
.choice:hover{border-color:var(--marquee);transform:translateY(-2px)}
.choice svg{width:34px;height:34px;fill:none;stroke:var(--marquee);stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
.choice b{font-size:19px}.choice span{color:var(--dim);font-size:14px}
.venue-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px;margin-top:14px}
.venue-tile{display:flex;flex-direction:column;align-items:center;gap:8px;margin:0;padding:16px 10px;background:#fff;color:var(--ink);
 border:1px solid var(--edge);border-radius:12px;font-size:13px;font-weight:600;text-align:center;cursor:pointer}
.venue-tile:hover{border-color:var(--marquee)}.venue-tile small{color:var(--dim);font-weight:400}
.vt-icon{display:grid;place-items:center;width:52px;height:52px;border-radius:14px;color:#fff;font-size:17px;font-weight:800;letter-spacing:.02em}
.venue-track{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:14px 0 4px}.venue-track button{margin:0}
.screen-track{display:flex;gap:8px;flex:1;min-width:260px}.screen-track input{padding:10px}
.mv-head .fmt{margin-left:8px;color:var(--dim);font-size:12px;font-weight:600;letter-spacing:.02em}
#vd-screens-btn.on{background:var(--marquee);color:#fff;border-color:var(--marquee)}
.seg{display:inline-flex;border:1px solid var(--edge);border-radius:9px;overflow:hidden}
.seg button{margin:0;padding:7px 12px;border:0;border-radius:0;background:#fff;color:var(--dim);font-size:13px;font-weight:600}
.seg button.on{background:var(--marquee);color:#fff}
.mv-filters{display:flex;flex-direction:column;gap:6px;margin:8px 0}.mv-filters:empty{display:none}
.frow{display:flex;gap:6px;flex-wrap:wrap;align-items:center}.frow>b{font-size:12px;color:var(--dim);margin-right:4px;min-width:52px}
.fchip{margin:0;padding:4px 10px;border:1px solid var(--edge);border-radius:99px;background:#fff;color:var(--ink);font-size:13px;font-weight:600}
.fchip.on{background:var(--marquee);border-color:var(--marquee);color:#fff}.fchip span{opacity:.7;font-weight:400}
.mw-panel{margin-top:14px}.mw-panel .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.mw-panel button{margin:0}
#mtabs{margin-bottom:6px}.soon-tag{display:inline-block;margin-top:4px;padding:1px 8px;border-radius:99px;background:#fff4dc;color:#a96b00;font-size:11px;font-weight:700}
.vd-hold{margin:12px 0 0}.vd-hold:empty{display:none}.vd-hold .fields{margin-top:10px}
.check{display:flex;gap:8px;align-items:flex-start;margin:0;font-weight:400;cursor:pointer}
.check input{width:auto;margin-top:4px;accent-color:var(--marquee)}
.mv-group{padding:16px;margin:12px 0;background:#fff;border:1px solid var(--edge);border-radius:12px}
.mv-group h3{margin:0;font-size:16px}.mv-head{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.catalog{display:grid;grid-template-columns:170px minmax(0,1fr);gap:26px;margin-top:18px;align-items:start}
.cats{position:sticky;top:12px;display:flex;flex-direction:column;gap:4px}
.cats h3{margin:0 0 6px;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
.cats h3+.cat{margin-top:0}.cats .cat+h3{margin-top:14px}
.cat{display:flex;justify-content:space-between;gap:8px;width:100%;margin:0;padding:8px 10px;background:transparent;color:var(--ink);
 border:0;border-radius:8px;font-size:14px;font-weight:600;text-align:left;cursor:pointer}
.cat span{color:var(--dim);font-weight:400}.cat:hover{background:#f2f4f7}
.cat.active{background:var(--marquee);color:#fff}.cat.active span{color:#fde2e6}
.movie-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:22px}
.movie-card{display:block;width:100%;padding:0;margin:0;text-align:left;background:none;color:var(--ink);border:0;cursor:pointer}
.movie-card img,.poster-fallback{display:block;width:100%;aspect-ratio:2/3;object-fit:cover;border-radius:11px;background:#e8eaf0}
.poster-fallback{display:grid;place-items:center;padding:16px;font-size:20px;font-weight:700;text-align:center}
.movie-card strong{display:block;margin-top:10px;font-size:16px;overflow-wrap:anywhere}
.movie-card small{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
 overflow-wrap:anywhere;color:var(--dim);font-size:13px;line-height:1.35}
.movie-card.active img,.movie-card.active .poster-fallback{outline:3px solid var(--marquee);outline-offset:3px}
.movie-add{display:flex;gap:8px;margin-top:20px}.movie-add input{flex:1}.movie-add button{margin:0}
.schedule{margin-top:32px}.date-strip{display:flex;gap:8px;overflow:auto;padding:12px 2px 18px;scroll-behavior:smooth;scrollbar-width:thin;flex:1}
.date-slider{display:flex;align-items:center;gap:6px}
.date-slider .arrow{flex:0 0 auto;margin:0;padding:6px 11px;background:#fff;color:var(--ink);border:1px solid var(--edge);border-radius:9px;font-size:18px;line-height:1}
.date-pill{position:relative}.date-pill i{display:block;margin:3px auto 0;width:6px;height:6px;border-radius:50%;background:transparent}
.date-pill.has i{background:var(--open)}.date-pill.active.has i{background:#fff}
.date-pill.nolist{color:#98a2b3;border-style:dashed}.date-pill.noshow{color:#98a2b3}
.date-legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--dim);margin:2px 0 6px}
.date-legend b{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--open);margin-right:5px;vertical-align:middle}
.date-pill{flex:0 0 70px;margin:0;padding:8px 4px;background:#fff;color:var(--ink);border:1px solid var(--edge);border-radius:9px;text-align:center}
.date-pill.active{background:var(--marquee);border-color:var(--marquee);color:#fff}.date-pill span{display:block;font-size:12px}.date-pill b{display:block;font-size:20px;line-height:1.2}
.venue-card{padding:20px;margin:12px 0;background:#fff;border:1px solid var(--edge);border-radius:12px;box-shadow:0 4px 16px rgba(17,24,39,.04)}
.venue-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.venue-head h3{margin:0;font-size:17px}
.time-grid{display:flex;flex-wrap:wrap;gap:10px;margin-top:16px}.time-pill{min-width:112px;margin:0;padding:9px 12px;background:#fff;color:var(--open);border:1px solid #b7dec7;border-radius:5px;line-height:1.3}
.time-pill small{display:block;color:var(--dim);font-size:11px;font-weight:400}
.format-section+.format-section{margin-top:18px;padding-top:14px;border-top:1px dashed var(--edge)}
.format-title{margin:16px 0 0;font-size:12px;font-weight:700;color:var(--dim)}
.format-section .time-grid{margin-top:7px}.time-pill.sold{color:var(--dim);border-color:var(--edge)}.time-pill.fast{color:#a96b00;border-color:#e7af34}
.watch-options{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}.watch-options button{margin:0}
.gap-panel{margin-top:30px;padding:20px;background:#f8f9fb;border:1px solid var(--edge);border-radius:12px}.gap-panel h3{margin:0 0 6px}.gap-panel .fields{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.live{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:12px 0 0;font-size:14px;color:var(--dim)}
.live button{margin:0}.dot{width:9px;height:9px;border-radius:50%;background:#b0b7c3;flex:0 0 auto}
.dot.on{background:var(--open);box-shadow:0 0 0 4px #d9f2e3}
.hold-line{margin-top:8px;font-size:13px}.hold-line button{margin:0 0 0 6px}
.wl{display:flex;align-items:center;gap:6px;margin-top:4px;font-size:13px;color:var(--dim)}
.hist-btn{margin:6px 0 0;font-size:12px;padding:4px 10px}div.hist-btn{padding:0}
.hist{margin:6px 0 0;padding:8px 10px 8px 28px;background:#fafbfc;border:1px solid var(--edge);border-radius:8px;
 font-size:13px;max-height:220px;overflow:auto}.hist li{margin:3px 0}.hist .mono{color:var(--dim);font-size:12px}
.hist .hi{display:inline-block;width:16px;text-align:center}
.wl .dot{width:8px;height:8px}.dot.fast{background:#e7a500}.dot.sold{background:var(--marquee)}
.time-pill small+small{margin-top:1px}
.cd{display:inline-block;margin-left:6px;padding:1px 8px;border-radius:99px;background:#eef4ff;color:#3478f6;font-weight:700;font-size:12px}
.cd.late{background:#fde8ea;color:var(--marquee)}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px;font-weight:700;background:#eef0f4;color:var(--dim)}
.badge.pending{background:#fff4dc;color:#a96b00}.badge.approved,.badge.triggered,.badge.held,.badge.found,.badge.booked{background:#e3f4ea;color:var(--open)}.badge.released{background:#eef0f4;color:var(--dim)}.badge.rejected,.badge.failed{background:#fde8ea;color:var(--marquee)}
.hold-form{margin-top:10px;padding:12px;border:1px solid var(--edge);border-radius:10px;background:#fafbfc}
.smap{margin-top:10px;padding:12px;border:1px solid var(--edge);border-radius:10px;background:#fafbfc}
.smap .svgbox{overflow-x:auto;margin:10px 0;background:#fff;border:1px solid var(--edge);border-radius:8px;padding:8px}
.smap svg{display:block;width:100%;min-width:460px;height:auto}
.smap svg [data-row]{cursor:pointer}
.smap .frow{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0}
.smap .frow b{font-size:13px;margin-right:4px}
.smap select{width:auto;padding:7px 9px;font-size:14px}
.smap .fits{font-size:14px;margin:6px 0}.smap .fits b{color:var(--open)}
.smap .key{display:inline-flex;align-items:center;gap:4px;margin-right:12px;font-size:12px;color:var(--dim)}
.smap .key i{display:inline-block;width:11px;height:11px;border-radius:3px}
.catchips{margin-top:6px;display:flex;flex-wrap:wrap;gap:6px}
.lang-tag{color:var(--marquee)!important;font-weight:700}
.health{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
label.inline{display:inline-flex;align-items:center;gap:6px;margin:0;font-size:13px;font-weight:600}
label.inline select{width:auto;padding:6px 8px;font-size:13px}
.recent{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:14px 0 4px;font-size:14px}.recent:empty{display:none}
.recent a.fchip{text-decoration:none}
.hp{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border:1px solid var(--edge);border-radius:99px;font-size:13px;font-weight:600;background:#fff}
.hp small{font-weight:400;color:var(--dim)}
.armed{margin-top:10px;padding:10px 12px;border-radius:10px;background:#fde8ea;color:#9b1c2e;border:1px solid #f5b5bf;font-size:14px}
.chip-st{display:inline-block;margin-left:6px;padding:1px 8px;border-radius:99px;font-size:10px;letter-spacing:.08em;font-weight:700;background:#eef0f4;color:var(--dim);vertical-align:1px}
.chip-st.ok{background:#e3f4ea;color:var(--open)}.chip-st.warn{background:#fff4dc;color:#a96b00}.chip-st.bad{background:#fde8ea;color:var(--marquee)}
.tl{display:flex;flex-wrap:wrap;gap:4px;list-style:none;margin:8px 0 2px;padding:0;font-size:12px}
.tl li{padding:2px 8px;border-radius:99px;background:#f3f4f6;color:#98a2b3}
.tl li.done{background:#e3f4ea;color:var(--open)}.tl li.now{background:var(--open);color:#fff;font-weight:700}
.tl li.end{background:#fde8ea;color:var(--marquee);font-weight:700}
.price{font-size:13px;color:var(--ink);margin-top:4px}.price .held{color:#3478f6;font-weight:600}
.seatprefs{grid-column:1/-1}.seatprefs summary{cursor:pointer;font-size:13px;font-weight:600;margin:4px 0}
.seatprefs .fields{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:6px}
@media(max-width:520px){.seatprefs .fields{grid-template-columns:1fr}}
.showpanel{margin:10px 0 4px;scroll-margin-top:12px}
.showpanel .svgbox{padding:6px;margin:8px 0}
.showpanel .smap svg{min-width:0;width:100%;max-height:44vh}
.showpanel .frow{margin:4px 0}
.pick-actions{position:sticky;bottom:0;z-index:2;margin:8px -12px -12px;padding:10px 12px 12px;background:#fff;
 border-top:1px solid var(--edge);border-radius:0 0 10px 10px;box-shadow:0 -8px 18px rgba(17,24,39,.06);display:grid;gap:6px}
.pick-actions .row button{margin:0}.pick-actions #pick-msg:empty{display:none}.pick-hint{margin:6px 0 0}
@media(max-width:520px){.pick-actions{gap:4px;padding:8px 10px 10px}.pick-actions .row{gap:6px}
 .pick-actions .row button{padding:8px 10px;font-size:13px}.pick-hold .hint{display:none}
 .showpanel .smap svg{max-height:38vh}}
.pick-hold{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:0;font-weight:400}.pick-hold input{width:auto}
body.picking #venue-shows>article:not(.pick-focus),body.picking #vd-shows>article:not(.pick-focus),
body.picking #mv-filters,body.picking #mw-panel,body.picking #mv-hold,body.picking #vd-hold,body.picking .gap-panel{display:none}
.pick-back{margin:0 0 8px}.showpanel .smap{background:#fff;border-color:#f0b4bd;box-shadow:0 8px 24px rgba(17,24,39,.06)}
.showpanel .row button{margin-top:4px}
.time-pill.picked{outline:2px solid var(--marquee);outline-offset:1px}
.grp{margin-top:10px;padding:12px;border:1px solid var(--edge);border-radius:10px;background:#fafbfc}
.grp .fields{display:grid;grid-template-columns:1fr 1fr;gap:10px}.grp label{margin:0 0 4px}
.grp input,.grp select{padding:9px;font-size:15px}.grp button{margin-top:10px}
.grp .link{display:flex;gap:8px;align-items:center;margin:8px 0}.grp .link input{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
.grp .link button{margin:0;white-space:nowrap}
.grp table{width:100%;border-collapse:collapse;font-size:14px;margin-top:8px}
.grp td{padding:6px 4px;border-top:1px solid var(--edge)}.grp td:last-child{text-align:right}
.grp td button{margin:0}
.join-card{border:2px solid var(--marquee);border-radius:14px;padding:14px 16px;margin:18px 0;background:#fff}
.join-card h3{margin:0 0 4px}.join-card .row{margin-top:8px}.join-card select,.join-card input{width:auto;padding:9px}
.join-card button{margin-top:0}
.paybox{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:8px}.paybox .qr{background:#fff;padding:6px;border:1px solid var(--edge);border-radius:8px}
@media(max-width:520px){.grp .fields{grid-template-columns:1fr}}
.stub .smap{margin-right:-70px;position:relative;z-index:1}.stub:has(.smap) .x{top:14px;transform:none}
@media(max-width:520px){.stub .smap{margin-left:-8px;padding:10px 8px}.smap svg{min-width:400px}}
.hold-form .fields{display:grid;grid-template-columns:1fr 1fr;gap:10px}.hold-form label{margin:0 0 4px}
.hold-form input,.hold-form select{padding:9px;font-size:15px}.hold-form button{margin-top:10px}
@media(max-width:520px){.hold-form .fields{grid-template-columns:1fr}}
@media(max-width:800px){.movie-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.gap-panel .fields{grid-template-columns:1fr}
 .catalog{grid-template-columns:1fr;gap:12px}.cats{position:static;flex-direction:row;overflow-x:auto;padding-bottom:4px}
 .cats h3,.cats .cat+h3{flex:0 0 auto;align-self:center;margin:0 2px 0 8px}.cat{flex:0 0 auto;width:auto;border:1px solid var(--edge);border-radius:99px;padding:6px 12px}}
@media(max-width:600px){.choices{grid-template-columns:1fr}.venue-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:520px){.movie-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.venue-head{align-items:flex-start;flex-direction:column}.movie-add{flex-direction:column}}
/* ---- theme: the start page's look (paper, ink, cinema-seat red, marquee type) ---- */
.me{margin-top:8px;font-size:14px;color:var(--dim)}.me a{color:var(--marquee)}
.name-ask{position:fixed;inset:0;z-index:50;display:grid;place-items:center;padding:16px;background:rgba(29,26,33,.45)}
.name-ask[hidden]{display:none}
.na-card{width:min(420px,100%);background:#fff;border-radius:14px;padding:22px 22px 18px;box-shadow:0 24px 60px rgba(29,26,33,.3)}
.na-head{font:800 28px/1 var(--display);letter-spacing:.06em;text-transform:uppercase;padding-bottom:12px;border-bottom:2px dashed var(--edge)}
.na-card p{color:var(--dim);font-size:14px;margin:12px 0 4px}.na-card .row{margin-top:12px}.na-card .row button{margin:0}
/* first visit: three steps, ticked off as they're done */
.start{background:#fff;border-radius:14px;box-shadow:var(--card-shadow);padding:16px 18px;margin:18px 0 6px;border-left:5px solid var(--marquee)}
.start[hidden]{display:none}
.home-top{display:none}body.on-home .home-top{display:block}
.credit{font-size:12px;color:var(--dim);margin:4px 0 24px}
.stale-note{position:fixed;z-index:45;left:12px;right:12px;top:12px;margin:0 auto;width:fit-content;max-width:calc(100vw - 24px);
  background:#fff7e0;color:var(--ink);border:1.5px solid #e0a526;border-radius:12px;padding:10px 14px;font-size:14px;
  box-shadow:var(--card-shadow);opacity:0;transform:translateY(-8px);transition:.2s;pointer-events:none}
.stale-note.on{opacity:1;transform:none}
/* "Popular now": posters people here track most */
.popular{margin:18px 0 6px}.popular[hidden]{display:none}
.pop-row{display:flex;gap:12px;overflow-x:auto;scroll-snap-type:x proximity;padding:10px 2px 12px}
.pop-film{flex:none;width:128px;text-transform:none;letter-spacing:normal;scroll-snap-align:start;background:none;border:0;padding:0;margin:0;text-align:left;cursor:pointer;color:var(--ink);font:inherit}
.pop-film img,.pop-film .poster-fallback{width:128px;aspect-ratio:2/3;object-fit:cover;border-radius:10px;font-size:17px;box-shadow:var(--card-shadow)}
.pop-film:hover img,.pop-film:hover .poster-fallback{outline:2px solid var(--marquee);outline-offset:2px}
.pop-film b{display:block;font-weight:600;font-size:14px;margin-top:6px;line-height:1.25;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.pop-film small{color:var(--dim);font-size:12.5px}.pop-film small.hot{color:var(--marquee);font-weight:600}
.pop-venues{display:flex;gap:8px;flex-wrap:wrap;margin-top:4px}
.pop-venues a{text-decoration:none;color:var(--ink);background:#fff;border-radius:99px;padding:7px 12px;font-size:14px;box-shadow:var(--card-shadow)}
.pop-venues a small{color:var(--marquee);font-weight:600;margin-left:4px}
/* "Your watches": a bar that stays in reach while scrolling */
.mybar{position:fixed;z-index:40;left:12px;right:12px;bottom:14px;width:fit-content;margin:0 auto!important;max-width:calc(100vw - 24px);
  display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:center;border:0;border-radius:16px;row-gap:4px;
  padding:11px 18px;background:var(--ink);color:#fff;font:600 14px/1.3 var(--body);text-transform:none;letter-spacing:normal;box-shadow:0 12px 32px rgba(29,26,33,.35);cursor:pointer}
.mybar[hidden],body.picking .mybar{display:none}
.mybar .sep{opacity:.4}.mybar>span{white-space:nowrap}.mybar .go{color:#ffd0dc}
.mybar .alert{background:var(--marquee);border-radius:99px;padding:2px 10px;animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{50%{opacity:.6}}
@media(prefers-reduced-motion:reduce){.mybar .alert{animation:none}}
body.has-bar{padding-bottom:76px}
body.list-seen .mybar{opacity:0;pointer-events:none}.mybar{transition:opacity .2s}
/* release calendar */
.soon-cal[hidden],.catalog[hidden]{display:none}
.cal-tools{display:flex;gap:12px;align-items:baseline;justify-content:space-between;flex-wrap:wrap;margin:6px 0 4px}
.cal-tools .link,.link{background:none;border:0;padding:4px;margin:0;color:var(--marquee);text-decoration:underline;cursor:pointer;font:inherit;font-size:14px;text-transform:none;letter-spacing:normal}
.cal-group h3{font:800 22px/1 var(--display);letter-spacing:.05em;text-transform:uppercase;margin:22px 0 10px;padding-bottom:8px;border-bottom:2px solid var(--ink)}
.cal-row{display:grid;grid-template-columns:64px minmax(0,1fr) auto;gap:14px;align-items:center;background:#fff;border-radius:12px;
  box-shadow:var(--card-shadow);padding:10px 14px 10px 10px;margin-bottom:10px}
.cal-poster{background:none;border:0;padding:0;margin:0;cursor:pointer}
.cal-poster img,.cal-poster .poster-fallback{width:64px;aspect-ratio:2/3;object-fit:cover;border-radius:8px;font-size:10px;padding:4px;letter-spacing:.02em}
.cal-title{background:none;border:0;padding:0;margin:0;cursor:pointer;text-align:left;font:600 17px/1.25 var(--body);color:var(--ink);text-transform:none;letter-spacing:normal}
.cal-title:hover{color:var(--marquee)}
.cal-rel{font-size:14px;color:var(--dim);margin:2px 0 4px}.cal-rel b{color:var(--ink)}
.cal-open{display:flex;gap:6px;flex-wrap:wrap;align-items:center;font-size:13px;color:#1f8a5b;font-weight:600}
.cal-open .fchip{margin:0;padding:3px 9px;font-size:13px}
.cal-on{color:#1f8a5b;font-weight:600;font-size:14px;white-space:nowrap}
.cal-act button{margin:0;white-space:nowrap}
@media(max-width:620px){.cal-row{grid-template-columns:56px minmax(0,1fr)}.cal-act{grid-column:2}.cal-poster img,.cal-poster .poster-fallback{width:56px}}
/* phones: a tab bar at the bottom */
.tabbar{display:none}
@media(max-width:760px){
  .tabbar{display:grid;grid-template-columns:repeat(4,1fr);position:fixed;z-index:42;left:0;right:0;bottom:0;background:#fff;
    border-top:1px solid var(--edge);padding:6px 4px calc(6px + env(safe-area-inset-bottom));box-shadow:0 -8px 24px rgba(29,26,33,.08)}
  .tabbar a{position:relative;display:flex;flex-direction:column;align-items:center;gap:3px;text-decoration:none;color:var(--dim);
    font:600 11.5px/1.1 var(--body);padding:4px 0;border-radius:10px}
  .tabbar svg{width:23px;height:23px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
  .tabbar a.on{color:var(--marquee)}
  body.list-seen .tabbar a.on{color:var(--dim)}body.list-seen .tabbar [data-tab=watches]{color:var(--marquee)}
  .tb-n{position:absolute;top:0;left:calc(50% + 6px);min-width:18px;height:18px;padding:0 5px;border-radius:99px;background:var(--ink);
    color:#fff;font:700 11px/18px var(--body);text-align:center}
  .tb-n.alert{background:var(--marquee);animation:pulse 1.6s ease-in-out infinite}
  .tb-n[hidden]{display:none}
  body{padding-bottom:calc(72px + env(safe-area-inset-bottom))}
  body.has-bar{padding-bottom:calc(72px + env(safe-area-inset-bottom))}
  body.bar-urgent{padding-bottom:calc(132px + env(safe-area-inset-bottom))}
  .mybar{bottom:calc(74px + env(safe-area-inset-bottom))}
  .mybar:not(.urgent){display:none}           /* the tab's badge carries the count; only "pay now" floats */
  body.picking .tabbar{display:none}
}
.start-head{display:flex;align-items:baseline;gap:12px}
.start-head b{font:800 24px/1 var(--display);letter-spacing:.05em;text-transform:uppercase}
.start-head span{color:var(--dim);font-size:14px}
.start-head .link{margin-left:auto;background:none;border:0;color:var(--dim);text-decoration:underline;padding:4px;cursor:pointer;font:inherit;font-size:14px}
.start-steps{list-style:none;margin:12px 0 0;padding:0;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.start-steps li{display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap;padding:12px;border:1.5px dashed var(--edge);border-radius:10px}
.start-steps li>div{flex:1;min-width:150px}
.start-steps p{margin:2px 0 0;color:var(--dim);font-size:13.5px}
.start-steps .sn{flex:none;width:28px;height:28px;border-radius:50%;display:grid;place-items:center;font:800 16px/1 var(--display);background:var(--ink);color:#fff}
.start-steps li.done{border-style:solid;background:#f6faf7}
.start-steps li.done .sn{background:#1f8a5b}
.start-steps li.done p{display:none}
.start-steps li.done b{text-decoration:line-through;text-decoration-color:rgba(29,26,33,.35)}
.start-steps button{margin:4px 0 0 38px}
/* one search box for movies and cinemas */
.finder{margin:18px 0 4px}
.find-label{font:800 20px/1 var(--display);letter-spacing:.05em;text-transform:uppercase;display:block;margin-bottom:8px}
.find-box{position:relative}
.find-box svg{position:absolute;left:14px;top:50%;transform:translateY(-50%);width:20px;height:20px;fill:none;stroke:var(--dim);stroke-width:2;pointer-events:none}
.find-box input{width:100%;padding:15px 16px 15px 44px;font-size:17px;border-radius:12px;background:#fff;box-shadow:var(--card-shadow)}
.find-list{position:absolute;left:0;right:0;top:calc(100% + 6px);z-index:30;background:#fff;border-radius:12px;
  box-shadow:0 18px 44px rgba(29,26,33,.18);padding:6px;max-height:min(60vh,440px);overflow:auto}
.find-list[hidden]{display:none}
.find-item{display:flex;gap:12px;align-items:center;width:100%;text-align:left;background:none;border:0;border-radius:8px;
  padding:9px 10px;margin:0;color:var(--ink);font:inherit;cursor:pointer}
.find-item.on,.find-item:hover{background:#f3f2ef}
.find-item b{display:block;font-weight:600}.find-item small{color:var(--dim);font-size:13px}
.fi-icon{flex:none;width:58px;text-align:center;font:700 11px/1 var(--body);letter-spacing:.08em;text-transform:uppercase;padding:6px 0;border-radius:6px;background:var(--ink);color:#fff}
.fi-icon.v{background:#fff;color:var(--ink);box-shadow:inset 0 0 0 1.5px var(--ink)}
.find-none{padding:12px;color:var(--dim);font-size:14px}
/* "Just opened": a strip of ticket stubs */
.opened{margin:22px 0 6px}
.opened-head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}.opened-head h2{margin:0}
.opened-strip{display:flex;gap:12px;overflow-x:auto;scroll-snap-type:x proximity;padding:10px 2px 12px}
.op{flex:none;width:230px;scroll-snap-align:start;display:flex;flex-direction:column;gap:3px;text-decoration:none;color:var(--ink);
  background:#fff;border-radius:12px;box-shadow:var(--card-shadow);padding:12px 14px;border-top:4px solid #1f8a5b;position:relative}
.op:hover{box-shadow:0 0 0 2px var(--marquee),var(--card-shadow)}
.op.NEW_SHOW,.op.BOOKINGS,.op.DATE_OPEN{border-top-color:var(--marquee)}
.op-what{font:800 19px/1.05 var(--display);letter-spacing:.04em;text-transform:uppercase;color:#1f8a5b}
.op.NEW_SHOW .op-what,.op.BOOKINGS .op-what,.op.DATE_OPEN .op-what{color:var(--marquee)}
.op b{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.op span,.op small{font-size:13px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.op .op-ago{margin-top:4px;padding-top:6px;border-top:1.5px dashed var(--edge);font-size:12px}
.op-empty{color:var(--dim);font-size:14px;padding:14px 16px;background:#fff;border-radius:12px;box-shadow:var(--card-shadow);max-width:640px}
@media(max-width:820px){.start-steps{grid-template-columns:1fr}}
body{background:var(--paper);font-family:var(--body)}
.wrap{max-width:1440px;padding:0 clamp(16px,3.5vw,48px)}
header{background:transparent;backdrop-filter:none;box-shadow:none;border-bottom:2px solid var(--ink);padding:20px 0 18px}
h1{font:800 40px/1 var(--display)}
h2{font:800 24px/1.1 var(--display);letter-spacing:.04em;text-transform:uppercase;margin:34px 0 12px}
h3{font-family:var(--body)}
button{font-family:var(--display);font-weight:800;font-size:17px;letter-spacing:.06em;text-transform:uppercase;border-radius:10px}
button:hover{background:var(--marquee-dark)}
button.g,button.pick,.seg button,.fchip,.cat,.date-pill,.time-pill,.movie-card,.venue-tile,.star,.date-slider .arrow,
.choice,button.hist-btn{font-family:var(--body);letter-spacing:0;text-transform:none}
button.g{color:var(--ink);background:#fff;border-color:var(--edge)}button.g:hover{background:#fff;border-color:var(--ink)}
button.pick:hover,.seg button:hover,.fchip:hover,.date-pill:hover,.time-pill:hover,.cat:hover,.movie-card:hover,.venue-tile:hover,
.date-slider .arrow:hover,.star:hover{background:inherit}
.fchip.on:hover,.cat.active:hover,.date-pill.active:hover,.seg button.on:hover{background:var(--marquee)}
.cat:hover{background:#e9e7ec}.time-pill:hover{background:#fff}.venue-tile:hover,.date-pill:hover{background:#fff}
select,input{border:1.5px solid var(--edge);border-radius:10px;font-family:var(--body)}
select:focus,input:focus{outline:none;border-color:var(--marquee);box-shadow:0 0 0 3px rgba(163,18,58,.15)}
.choice,.venue-card,.mv-group,.prefs,.venue-tile,.gap-panel,.join-card{background:#fff;border:0;border-radius:14px;box-shadow:var(--card-shadow)}
.choice b{font:800 28px/1 var(--display);letter-spacing:.03em;text-transform:uppercase}
.choice:hover{transform:none;box-shadow:0 0 0 2px var(--marquee),var(--card-shadow)}
.venue-tile:hover{box-shadow:0 0 0 2px var(--marquee),var(--card-shadow)}
.prefs summary{font:800 20px/1.2 var(--display);letter-spacing:.04em;text-transform:uppercase}
.prefs summary .hint{font-family:var(--body);text-transform:none;letter-spacing:0;font-weight:400}
.venue-head h3,.mv-group h3{font:800 22px/1.1 var(--display);letter-spacing:.03em;text-transform:uppercase}
.gap-panel h3{font:800 22px/1.1 var(--display);letter-spacing:.03em;text-transform:uppercase}
/* each watch is a ticket: stub on the right behind a perforation */
.stub{background:#fff;border:0;border-radius:14px;box-shadow:var(--card-shadow);backdrop-filter:none}
.stub:before{width:0;background:none;border-left:2px dashed var(--edge)}
.stub .v{font:800 15px/1.2 var(--display);letter-spacing:.08em}
.stub .d{font-weight:600}
.show{border-left:0;box-shadow:var(--card-shadow);backdrop-filter:none}
.hold-form,.smap,.grp,.setup{background:#faf9f7;border-color:var(--edge)}
.hp{background:#fff}
.empty{background:transparent;border-color:#cfcbd4}
/* no posters from BookMyShow: each film gets a marquee-style title card */
.poster-fallback{background:var(--ink);color:var(--paper);font:800 24px/1 var(--display);letter-spacing:.04em;text-transform:uppercase;
 box-shadow:inset 0 0 0 6px var(--ink),inset 0 0 0 7px rgba(243,242,239,.25)}
.movie-card strong{font-weight:700}
.join-card{border:0;box-shadow:0 0 0 2px var(--marquee),var(--card-shadow)}
footer{border-top:1px solid var(--edge);margin-top:20px}
/* ================= landing: the story above the tools ================= */
body.off-home .landing{display:none}
.landing{position:relative;background:var(--paper);overflow-x:clip;font-family:var(--body)}
.landing a{color:inherit}
.ln-wrap{max-width:1440px;margin:0 auto;padding:0 clamp(16px,3.5vw,48px)}
.ln-sec{padding:clamp(64px,9vw,120px) 0}
.ln-h2{font:800 clamp(46px,7vw,104px)/.88 var(--display);text-transform:uppercase;letter-spacing:.005em;margin:0 0 16px;color:var(--ink)}
.ln-h2 span{color:var(--marquee)}
.ln-sub{font-size:clamp(17px,1.4vw,20px);line-height:1.55;color:var(--dim);max-width:62ch;margin:0 0 40px}
.ln-sub b{color:var(--ink);font-weight:600}

/* nav */
.ln-nav{position:sticky;top:0;z-index:30;background:rgba(243,242,239,.82);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  border-bottom:1px solid transparent;transition:border-color .3s,background .3s}
.ln-nav.solid{border-bottom-color:var(--edge);background:rgba(243,242,239,.94)}
.ln-nav .ln-wrap{display:flex;align-items:center;justify-content:space-between;gap:16px;height:64px}
.ln-logo{font:800 28px/1 var(--display);letter-spacing:.04em;text-transform:uppercase;text-decoration:none;color:var(--ink)!important}
.ln-logo b{color:var(--marquee)}
.ln-links{display:flex;align-items:center;gap:clamp(10px,2vw,26px);font-size:15px;font-weight:600}
.ln-links a{text-decoration:none;color:var(--dim)!important}.ln-links a:hover{color:var(--ink)!important}
.ln-links .ln-cta-sm{background:var(--ink);color:#fff!important;border-radius:99px;padding:9px 16px}
.ln-links .ln-cta-sm:hover{background:var(--marquee)}

/* hero */
.ln-hero{padding:clamp(26px,5vw,70px) 0 clamp(34px,5vw,64px)}
.ln-hero-grid{display:grid;grid-template-columns:minmax(0,1.08fr) minmax(320px,.92fr);gap:clamp(28px,5vw,80px);align-items:center}
.ln-kicker{display:flex;align-items:center;gap:10px;font:700 13px/1.2 var(--body);letter-spacing:.16em;text-transform:uppercase;color:var(--marquee);margin:0}
.ln-kicker i{width:9px;height:9px;border-radius:50%;background:var(--marquee);box-shadow:0 0 0 0 rgba(163,18,58,.5);animation:ln-live 1.8s infinite}
.ln-title{font:800 clamp(66px,min(8.8vw,14.5vh),150px)/.84 var(--display);text-transform:uppercase;letter-spacing:.005em;margin:18px 0 26px;color:var(--ink)}
.ln-title>span{display:block;overflow:hidden;padding-bottom:.04em}
.ln-title em{display:block;position:relative;font-style:normal;transform:translateY(108%);animation:ln-rise .9s cubic-bezier(.2,.8,.2,1) forwards}
.ln-title>span:nth-child(2) em{animation-delay:.12s}.ln-title>span:nth-child(3) em{animation-delay:.24s}
.ln-title .red{color:var(--marquee)}
.ln-title .strike em{width:fit-content;animation:ln-rise .9s cubic-bezier(.2,.8,.2,1) forwards,ln-fade-word .5s 1.75s forwards}
.ln-title .strike em::after{content:"";position:absolute;left:-3%;right:-3%;top:50%;height:.085em;background:var(--marquee);border-radius:99px;
  transform:scaleX(0);transform-origin:left;animation:ln-strike .55s 1.25s cubic-bezier(.7,0,.2,1) forwards}
.ln-lead{font-size:clamp(17px,1.45vw,21px);line-height:1.55;color:var(--dim);max-width:56ch;margin:0;opacity:0;animation:ln-up .8s .5s forwards}
.ln-lead b{color:var(--ink);font-weight:600}
.ln-ctas{display:flex;flex-wrap:wrap;gap:12px;margin:30px 0 14px;opacity:0;animation:ln-up .8s .7s forwards}
.ln-btn{display:inline-flex;align-items:center;gap:10px;background:var(--marquee);color:#fff!important;text-decoration:none;
  font:800 20px/1 var(--display);letter-spacing:.08em;text-transform:uppercase;padding:17px 26px;border-radius:12px;
  box-shadow:0 12px 26px rgba(163,18,58,.28);transition:transform .18s,box-shadow .18s,background .18s}
.ln-btn:hover{transform:translateY(-2px);box-shadow:0 16px 32px rgba(163,18,58,.34);background:var(--marquee-dark)}
.ln-btn svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2.6;stroke-linecap:round;stroke-linejoin:round}
.ln-btn.ghost{background:transparent;color:var(--ink)!important;box-shadow:inset 0 0 0 2px var(--ink)}
.ln-btn.ghost:hover{background:var(--ink);color:#fff!important}
.ln-fine{font-size:14px;color:var(--dim);margin:0;opacity:0;animation:ln-up .8s .85s forwards}
.ln-lead .short,.btn-short{display:none}

/* hero visual: a seat map inside a marquee sign */
.ln-visual{position:relative;opacity:0;animation:ln-pop-in 1s .35s cubic-bezier(.2,.9,.3,1.1) forwards}
.hc-frame{position:relative;background:var(--ink);border-radius:26px;padding:22px;box-shadow:0 34px 70px rgba(29,26,33,.28);
  transform:rotate(-1.6deg);animation:ln-float 7s ease-in-out infinite}
.bulbs{position:absolute;inset:0;pointer-events:none}
.bulbs i{position:absolute;width:7px;height:7px;margin:-3.5px;border-radius:50%;background:#ffd27a;box-shadow:0 0 8px 2px rgba(255,200,90,.65);
  animation:ln-bulb 1.1s ease-in-out infinite alternate}
.bulbs i:nth-child(even){animation-delay:.55s}
.hc{position:relative;background:#fff;border-radius:14px;padding:16px 14px 14px;overflow:hidden}
.hc-top{display:flex;justify-content:space-between;align-items:baseline;font-size:13px;color:var(--dim);margin-bottom:12px}
.hc-top b{font:800 18px/1 var(--display);letter-spacing:.06em;text-transform:uppercase;color:var(--ink)}
.hc-seats{display:grid;gap:5px}
.hc-row{display:grid;grid-template-columns:12px repeat(16,minmax(0,1fr)) 12px;gap:4px;align-items:center}
.hc-row em{font:700 10px/1 var(--body);font-style:normal;color:#a39fab;text-align:center}
.hc .s{aspect-ratio:1/.92;border-radius:4px 4px 2px 2px;background:#dcd8e1;transition:background .3s,box-shadow .3s}
.hc .s.gap{visibility:hidden}
.hc .s.free{background:#1f8a5b;animation:ln-seat-pop .45s cubic-bezier(.2,.9,.3,1.4)}
.hc .s.held{background:#e0a526;box-shadow:0 0 0 2px rgba(224,165,38,.35);animation:ln-seat-pop .45s}
.hc-screen{margin:14px auto 0;width:72%;height:5px;border-radius:99px;background:linear-gradient(90deg,transparent,#bdb8c4 18%,#bdb8c4 82%,transparent)}
.hc-screen-t{text-align:center;font:700 10px/1 var(--body);letter-spacing:.32em;color:#a39fab;margin-top:6px}
.hc-stamp{position:absolute;left:50%;top:50%;z-index:2;font:800 clamp(34px,4vw,50px)/1 var(--display);letter-spacing:.08em;text-transform:uppercase;
  color:var(--marquee);border:4px solid var(--marquee);border-radius:10px;padding:6px 18px 4px;background:rgba(255,255,255,.9);
  opacity:0;transform:translate(-50%,-50%) rotate(-11deg) scale(1.8);transition:opacity .25s,transform .35s cubic-bezier(.3,1.6,.5,1);pointer-events:none}
.hc-stamp.on{opacity:1;transform:translate(-50%,-50%) rotate(-11deg) scale(1)}
.hc-toast{position:absolute;z-index:3;left:10px;right:10px;top:10px;display:flex;gap:12px;align-items:center;background:var(--ink);color:#fff;
  border-radius:14px;padding:11px 13px;box-shadow:0 14px 30px rgba(29,26,33,.35);transform:translateY(-140%);transition:transform .5s cubic-bezier(.2,.9,.3,1.25)}
.hc-toast.on{transform:none}
.hc-toast .bell{flex:none;width:34px;height:34px;border-radius:10px;background:var(--marquee);display:grid;place-items:center}
.hc-toast .bell svg{width:18px;height:18px;fill:none;stroke:#fff;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.hc-toast.on .bell svg{animation:ln-ring .9s .3s}
.hc-toast b{display:block;font-size:14px}.hc-toast span{font-size:12.5px;color:#cfcad6}
.hc-held{position:absolute;z-index:3;left:50%;bottom:12px;display:flex;align-items:center;gap:8px;white-space:nowrap;background:#e0a526;color:var(--ink);
  font-weight:700;font-size:14px;border-radius:99px;padding:9px 16px;box-shadow:0 10px 24px rgba(224,165,38,.4);
  transform:translate(-50%,170%);transition:transform .45s cubic-bezier(.2,.9,.3,1.25)}
.hc-held.on{transform:translate(-50%,0)}
.hc-legend{display:flex;justify-content:center;gap:18px;flex-wrap:wrap;margin-top:22px;font-size:13px;color:var(--dim)}
.hc-legend i{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-1px;margin-right:6px}
.ln-scroll{display:flex;flex-direction:column;align-items:center;gap:6px;width:fit-content;margin:clamp(30px,5vw,56px) auto 0;
  font-size:13px;font-weight:600;color:var(--dim)!important;text-decoration:none;letter-spacing:.06em;text-transform:uppercase}
.ln-scroll svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:2.4;animation:ln-bob 1.6s ease-in-out infinite}

/* live ticker */
.ln-ticker{display:flex;align-items:stretch;background:var(--ink);color:#fff;overflow:hidden;white-space:nowrap}
.ln-ticker[hidden]{display:none}
.ln-ticker>b{flex:none;position:relative;z-index:1;display:flex;align-items:center;gap:8px;background:var(--marquee);padding:13px 18px;
  font:800 16px/1 var(--display);letter-spacing:.12em;text-transform:uppercase}
.ln-ticker>b i{width:8px;height:8px;border-radius:50%;background:#fff;animation:ln-blink 1.2s infinite}
.tk-mask{overflow:hidden;flex:1;-webkit-mask:linear-gradient(90deg,transparent,#000 4%,#000 96%,transparent);mask:linear-gradient(90deg,transparent,#000 4%,#000 96%,transparent)}
.tk-track{display:inline-flex;gap:46px;padding:13px 0 13px 30px;animation:ln-tick 50s linear infinite}
.ln-ticker:hover .tk-track{animation-play-state:paused}
.tk-track a{text-decoration:none;font-size:14.5px;color:#e9e6ee!important}
.tk-track a i{font-style:normal;font-weight:700;color:#7fe0ae;margin-right:6px}
.tk-track a:hover{color:#fff!important;text-decoration:underline}

/* why seats come back */
.why-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:clamp(14px,2vw,26px)}
.why{background:#fff;border-radius:18px;padding:22px 22px 26px;box-shadow:var(--card-shadow);transition:transform .25s,box-shadow .25s}
.why:hover{transform:translateY(-4px);box-shadow:0 1px 0 var(--edge),0 22px 40px rgba(29,26,33,.1)}
.why h3{font:800 30px/1 var(--display);text-transform:uppercase;letter-spacing:.03em;margin:20px 0 8px;color:var(--ink)}
.why p{margin:0;color:var(--dim);line-height:1.55}
.why-art{position:relative;height:150px;border-radius:14px;background:var(--paper);display:grid;place-items:center;overflow:hidden}
.mini{display:grid;grid-template-columns:repeat(8,22px);gap:6px}
.mini i{height:20px;border-radius:5px 5px 3px 3px;background:#dcd8e1}
.art-lock .mini i.b{animation:ln-unblock 5s infinite}
.art-lock .lock{position:absolute;width:46px;height:46px;animation:ln-lock 5s infinite}
.art-lock .lock path.shackle{animation:ln-shackle 5s infinite}
.art-timer .mini i.h{animation:ln-unpaid 5s infinite}
.art-timer .ring{position:absolute;right:16px;top:14px;width:44px;height:44px}
.art-timer .ring circle.run{stroke-dasharray:113;animation:ln-ring-run 5s linear infinite}
.art-timer .ring text{font:700 11px var(--body);fill:var(--ink)}
.art-cancel .mini i.c1{animation:ln-cancel 4s infinite}.art-cancel .mini i.c2{animation:ln-cancel 4s 1.6s infinite}
.art-cancel .mini i.c3{animation:ln-cancel 4s 2.9s infinite}

/* stats */
.ln-stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;background:var(--edge);border-radius:18px;overflow:hidden;margin-top:clamp(40px,6vw,72px)}
.ln-stat{background:#fff;padding:24px 22px}
.ln-stat b{display:block;font:800 clamp(44px,5vw,72px)/.9 var(--display);color:var(--ink)}
.ln-stat>span{display:block;margin-top:8px;color:var(--dim);font-size:15px;line-height:1.4}

/* the race: releases vs refreshing vs Seat Watch */
.race{background:#fff;border-radius:22px;padding:clamp(22px,3.5vw,44px);box-shadow:var(--card-shadow)}
.lanes{position:relative;margin:34px 0 8px;display:grid;gap:14px}
.lane{display:grid;grid-template-columns:clamp(110px,14vw,170px) minmax(0,1fr);align-items:center;gap:14px}
.lane>span{font-weight:700;font-size:14px;color:var(--ink)}.lane>span small{display:block;font-weight:500;color:var(--dim);font-size:12.5px}
.track{position:relative;height:38px;border-radius:10px;background:var(--paper)}
.track .pt{position:absolute;top:50%;left:calc(var(--p) * 1%);width:16px;height:16px;margin:-8px 0 0 -8px;border-radius:50%}
.lane-rel .pt{background:#1f8a5b;transform:scale(0);animation:ln-blip 8s calc(var(--p) * .08s) infinite}
.lane-you .pt{background:#bdb8c4;animation:ln-look 8s calc(var(--p) * .08s) infinite}
.lane-you .pt::after{content:"";position:absolute;inset:-6px;border-radius:50%;border:2px solid #bdb8c4;opacity:0;animation:ln-look-ring 8s calc(var(--p) * .08s) infinite}
.lane-sw .track{background:repeating-linear-gradient(90deg,#e6e2ea 0 2px,transparent 2px 9px),var(--paper)}
.lane-sw .pt{background:var(--marquee);transform:scale(0);animation:ln-caught 8s calc(var(--p) * .08s) infinite}
.now-line{position:absolute;top:-10px;bottom:-10px;left:calc(clamp(110px,14vw,170px) + 14px);right:0;pointer-events:none}
.now-line i{position:absolute;top:0;bottom:0;width:2px;background:var(--ink);opacity:.5;animation:ln-sweep 8s linear infinite}
.race-note{display:flex;flex-wrap:wrap;gap:8px 26px;margin-top:22px;font-size:14px;color:var(--dim)}
.race-note i{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:7px;vertical-align:-1px}

/* how it works: a sticky stage that changes as the steps scroll by */
.ln-how{background:var(--ink);color:#fff}
.ln-how .ln-h2{color:#fff}.ln-how .ln-sub{color:#bdb8c4}
.story{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:clamp(28px,6vw,96px)}
.story-steps{order:1}
.story-stage{order:2;position:sticky;top:calc(50vh - 230px);height:460px;align-self:start}
.step{min-height:78vh;display:flex;flex-direction:column;justify-content:center;opacity:.28;transition:opacity .45s}
.step.on{opacity:1}
.step .n{font:800 22px/1 var(--display);letter-spacing:.14em;color:var(--marquee);text-transform:uppercase}
.step h3{font:800 clamp(38px,4.6vw,64px)/.92 var(--display);text-transform:uppercase;margin:12px 0 14px;color:#fff}
.step p{font-size:clamp(16px,1.3vw,19px);line-height:1.6;color:#cfcad6;margin:0;max-width:46ch}
.step p b{color:#fff}
.scene{position:absolute;inset:0;display:grid;place-items:center;opacity:0;transform:translateY(18px) scale(.97);
  transition:opacity .55s,transform .55s cubic-bezier(.2,.8,.2,1);pointer-events:none}
.story-stage[data-step="1"] .sc1,.story-stage[data-step="2"] .sc2,.story-stage[data-step="3"] .sc3,.story-stage[data-step="4"] .sc4{opacity:1;transform:none}
.card-w{background:#fff;color:var(--ink);border-radius:18px;padding:18px;box-shadow:0 30px 60px rgba(0,0,0,.4);width:min(380px,100%)}
.sc-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;font-size:13px;color:var(--dim)}
.sc-head b{font:800 20px/1 var(--display);letter-spacing:.06em;text-transform:uppercase;color:var(--marquee)}
.refresh{width:26px;height:26px;fill:none;stroke:var(--ink);stroke-width:2.2;stroke-linecap:round;animation:ln-spin 2.6s cubic-bezier(.6,0,.4,1) infinite}
.grid10{display:grid;grid-template-columns:repeat(10,minmax(0,1fr));gap:5px}
.grid10 i{aspect-ratio:1/.92;border-radius:4px 4px 2px 2px;background:#dcd8e1}
.grid10 i.flick{animation:ln-flick 2.6s infinite}
.sc-cap{margin-top:14px;font-size:13.5px;color:var(--dim)}
.radar{position:relative;width:min(380px,90%);aspect-ratio:1;display:grid;place-items:center}
.radar i{position:absolute;inset:30%;border-radius:50%;border:2px solid rgba(255,255,255,.5);animation:ln-ping 3s cubic-bezier(.2,.6,.3,1) infinite}
.radar i:nth-child(2){animation-delay:1s}.radar i:nth-child(3){animation-delay:2s}
.radar .core{position:relative;z-index:1;width:42%;aspect-ratio:1;border-radius:50%;background:var(--marquee);display:grid;place-items:center;text-align:center;
  box-shadow:0 0 0 10px rgba(163,18,58,.25),0 20px 50px rgba(163,18,58,.45)}
.radar .core b{display:block;font:800 clamp(44px,5vw,64px)/.9 var(--display)}.radar .core span{font-size:12px;letter-spacing:.12em;text-transform:uppercase}
.radar .tag{position:absolute;background:#fff;color:var(--ink);border-radius:99px;padding:6px 12px;font-size:12.5px;font-weight:600;box-shadow:0 10px 24px rgba(0,0,0,.35)}
.radar .t1{left:0;top:16%}.radar .t2{right:0;top:40%}.radar .t3{left:8%;bottom:12%}
.phone{position:relative;width:240px;height:440px;border-radius:38px;background:#2b2731;border:8px solid #0f0d12;box-shadow:0 30px 60px rgba(0,0,0,.5);overflow:hidden}
.phone .clock{text-align:center;margin-top:46px;font:800 64px/1 var(--display);color:#fff}.phone .date{text-align:center;color:#bdb8c4;font-size:13px;margin-top:6px}
.phone .note{position:absolute;left:10px;right:10px;top:150px;background:rgba(255,255,255,.96);color:var(--ink);border-radius:16px;padding:12px;
  display:flex;gap:10px;box-shadow:0 12px 26px rgba(0,0,0,.35);animation:ln-notify 4.5s infinite}
.phone .note .bell{flex:none;width:30px;height:30px;border-radius:9px;background:var(--marquee);display:grid;place-items:center}
.phone .note .bell svg{width:16px;height:16px;fill:none;stroke:#fff;stroke-width:2.2}
.phone .note b{display:block;font-size:13px}.phone .note span{display:block;font-size:12px;color:var(--dim);line-height:1.35}
.story-stage[data-step="3"] .phone{animation:ln-buzz 4.5s infinite}
.hold-card .grid10 i.h{background:#e0a526;animation:ln-held-glow 1.6s ease-in-out infinite}
.hold-row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:16px}
.watch{font:800 40px/1 var(--display);color:var(--ink)}.watch small{font:600 12px var(--body);color:var(--dim);display:block;letter-spacing:.06em;text-transform:uppercase}
.pay{background:var(--ink);color:#fff;border-radius:12px;padding:12px 16px;font-weight:700;font-size:14px;animation:ln-nudge 1.6s ease-in-out infinite}

/* features */
.feat-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:clamp(14px,2vw,24px)}
.feat{background:#fff;border-radius:18px;padding:24px;box-shadow:var(--card-shadow);transition:transform .25s,box-shadow .25s}
.feat:hover{transform:translateY(-4px);box-shadow:0 1px 0 var(--edge),0 22px 40px rgba(29,26,33,.1)}
.feat svg{width:40px;height:40px;padding:8px;border-radius:12px;background:var(--paper);fill:none;stroke:var(--marquee);stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.feat h3{font:800 25px/1.05 var(--display);text-transform:uppercase;letter-spacing:.03em;margin:16px 0 6px;color:var(--ink)}
.feat p{margin:0;color:var(--dim);line-height:1.55}

/* final call */
.ln-final{position:relative;overflow:hidden;background:var(--marquee);color:#fff;text-align:center;padding:clamp(80px,11vw,150px) 0}
.ln-final .beam{position:absolute;top:-40%;width:70vmax;height:70vmax;left:50%;margin-left:-35vmax;border-radius:50%;
  background:conic-gradient(from 0deg,transparent 0 40deg,rgba(255,255,255,.16) 52deg,transparent 64deg 180deg,rgba(255,255,255,.12) 194deg,transparent 206deg);
  animation:ln-beam 14s linear infinite;pointer-events:none}
.ln-final .beam.b2{animation-duration:22s;animation-direction:reverse;opacity:.7}
.ln-final .ln-h2{position:relative;color:#fff;font-size:clamp(52px,8.5vw,132px)}
.ln-final .ln-h2 span{color:#ffd27a}
.ln-final p{position:relative;color:#f6d9e1;font-size:clamp(16px,1.4vw,19px);margin:16px auto 30px;max-width:52ch}
.ln-final .ln-btn{position:relative;background:#fff;color:var(--marquee)!important;box-shadow:0 16px 36px rgba(0,0,0,.25)}
.ln-final .ln-btn:hover{background:var(--ink);color:#fff!important}
.ln-final .ln-scroll{position:relative;color:#f6d9e1!important}

/* reveal on scroll (only when JS runs; without it everything shows) */
.js-reveal .reveal{opacity:0;transform:translateY(28px);transition:opacity .8s cubic-bezier(.2,.8,.2,1),transform .8s cubic-bezier(.2,.8,.2,1)}
.js-reveal .reveal.in{opacity:1;transform:none}
.js-reveal .reveal.d1{transition-delay:.1s}.js-reveal .reveal.d2{transition-delay:.2s}.js-reveal .reveal.d3{transition-delay:.3s}
.js-reveal .reveal.d4{transition-delay:.4s}.js-reveal .reveal.d5{transition-delay:.5s}

@keyframes ln-rise{to{transform:none}}
@keyframes ln-up{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}
@keyframes ln-pop-in{from{opacity:0;transform:translateY(30px) scale(.94)}to{opacity:1;transform:none}}
@keyframes ln-strike{to{transform:scaleX(1)}}
@keyframes ln-fade-word{to{color:#b4afbb}}
@keyframes ln-live{0%{box-shadow:0 0 0 0 rgba(163,18,58,.5)}80%,100%{box-shadow:0 0 0 12px rgba(163,18,58,0)}}
@keyframes ln-float{0%,100%{transform:rotate(-1.6deg) translateY(0)}50%{transform:rotate(-.6deg) translateY(-10px)}}
@keyframes ln-bulb{from{opacity:.35;box-shadow:none}to{opacity:1}}
@keyframes ln-seat-pop{0%{transform:scale(.4)}60%{transform:scale(1.25)}100%{transform:none}}
@keyframes ln-ring{0%,100%{transform:rotate(0)}20%{transform:rotate(18deg)}40%{transform:rotate(-16deg)}60%{transform:rotate(10deg)}80%{transform:rotate(-6deg)}}
@keyframes ln-bob{0%,100%{transform:translateY(0)}50%{transform:translateY(6px)}}
@keyframes ln-blink{50%{opacity:.25}}
@keyframes ln-tick{to{transform:translateX(-50%)}}
@keyframes ln-unblock{0%,42%{background:#bdb8c4}55%,88%{background:#1f8a5b}100%{background:#bdb8c4}}
@keyframes ln-lock{0%,40%{opacity:1;transform:none}52%,92%{opacity:0;transform:translateY(-14px) scale(.8)}100%{opacity:1;transform:none}}
@keyframes ln-shackle{0%,30%{transform:none}40%,100%{transform:translateY(-5px)}}
@keyframes ln-unpaid{0%,66%{background:#e0a526}72%,92%{background:#1f8a5b}100%{background:#e0a526}}
@keyframes ln-ring-run{0%{stroke-dashoffset:0}66%,100%{stroke-dashoffset:113}}
@keyframes ln-cancel{0%,8%{background:#dcd8e1;transform:none}14%{background:#1f8a5b;transform:scale(1.25)}20%,55%{background:#1f8a5b;transform:none}62%,100%{background:#dcd8e1}}
@keyframes ln-blip{0%{transform:scale(0)}2%{transform:scale(1.25)}4%,9%{transform:scale(1)}12%,100%{transform:scale(0)}}
@keyframes ln-look{0%,100%{background:#bdb8c4}1%,5%{background:var(--ink)}}
@keyframes ln-look-ring{0%{opacity:.9;transform:scale(.6)}6%{opacity:0;transform:scale(1.6)}100%{opacity:0}}
@keyframes ln-caught{0%{transform:scale(0)}2%{transform:scale(1.35)}5%,14%{transform:scale(1)}18%,100%{transform:scale(0)}}
@keyframes ln-sweep{from{left:0}to{left:100%}}
@keyframes ln-spin{0%{transform:rotate(0)}40%,100%{transform:rotate(360deg)}}
@keyframes ln-flick{0%,52%{background:#dcd8e1}56%,72%{background:#1f8a5b}76%,100%{background:#dcd8e1}}
@keyframes ln-ping{0%{transform:scale(1);opacity:.9}100%{transform:scale(3.2);opacity:0}}
@keyframes ln-notify{0%,8%{transform:translateY(-220px);opacity:0}16%,88%{transform:none;opacity:1}96%,100%{transform:translateY(-220px);opacity:0}}
@keyframes ln-buzz{0%,14%{transform:none}15%{transform:translateX(-4px) rotate(-1deg)}16%{transform:translateX(4px) rotate(1deg)}17%{transform:translateX(-3px)}18%{transform:translateX(3px)}19%,100%{transform:none}}
@keyframes ln-held-glow{50%{box-shadow:0 0 0 3px rgba(224,165,38,.35)}}
@keyframes ln-nudge{0%,100%{transform:none}50%{transform:translateX(4px)}}
@keyframes ln-beam{to{transform:rotate(360deg)}}

@media(max-width:980px){
  .ln-hero-grid{grid-template-columns:1fr}
  .ln-visual{max-width:520px;width:100%;margin:0 auto}
  .why-grid,.feat-grid{grid-template-columns:1fr 1fr}
  .ln-stats{grid-template-columns:1fr 1fr}
  .story{grid-template-columns:1fr}
  .story-stage{order:0;top:64px;height:300px;z-index:2;background:var(--ink);margin:0 calc(-1 * clamp(16px,3.5vw,48px));
    box-shadow:0 18px 24px -12px var(--ink)}
  .story-steps{order:1}
  .step{min-height:78vh;justify-content:flex-end;padding-bottom:14vh}
  .scene{transform:scale(.6)}
  .story-stage[data-step="1"] .sc1,.story-stage[data-step="2"] .sc2,.story-stage[data-step="3"] .sc3,.story-stage[data-step="4"] .sc4{transform:scale(.66)}
}
/* phones: the whole hero fits between the top bar (64px) and the tab bar (72px) */
@media(max-width:760px){
  .ln-hero{padding:0}
  .ln-hero>.ln-wrap{display:flex;flex-direction:column;justify-content:center;padding-top:12px;padding-bottom:16px;
    min-height:calc(100vh - 136px);min-height:calc(100svh - 136px - env(safe-area-inset-bottom))}
  .ln-hero-grid{display:flex;flex-direction:column;align-items:stretch;gap:clamp(10px,2.2svh,20px)}
  .ln-hero-grid>div:first-child{display:contents}
  .ln-kicker{order:1;font-size:11.5px;letter-spacing:.12em}
  .ln-title{order:2;margin:0;font-size:clamp(40px,min(13.4vw,7.4svh),64px)}
  .ln-title>span:nth-child(2),.ln-title>span:nth-child(3){display:inline-block;vertical-align:top}
  .ln-visual{order:3;width:100%;margin:0 auto}
  .ln-lead{order:4;font-size:15.5px;line-height:1.45}
  .ln-lead .long,.btn-long{display:none}.ln-lead .short,.btn-short{display:inline}
  .ln-ctas{order:5;margin:0;flex-wrap:nowrap;gap:10px}
  .ln-ctas .ln-btn{flex:1;justify-content:center;padding:15px 10px;font-size:17px;white-space:nowrap}
  .ln-fine,.hc-legend,.ln-hero .ln-scroll{display:none}
  .hc-frame{padding:12px;border-radius:20px}
  .hc{padding:11px 10px 10px}
  .hc-row:nth-child(-n+2){display:none}
  .hc-top{margin-bottom:8px}.hc-top b{font-size:15px}
  .hc-screen{margin-top:9px}
  .hc-stamp{font-size:30px;border-width:3px}
  .hc-toast{padding:8px 10px;gap:9px}.hc-toast .bell{width:28px;height:28px}
  .hc-toast b{font-size:13px}.hc-toast span{font-size:11.5px}
  .hc-held{font-size:12.5px;padding:7px 12px;bottom:8px}
}
/* short phones: fewer seat rows (the action is in E-H) and no kicker line */
@media(max-width:760px) and (max-height:760px){
  .ln-kicker{display:none}
  .hc-row:nth-child(-n+3){display:none}
}
@media(max-width:760px) and (max-height:660px){
  .hc-row:nth-child(10){display:none}
  .hc-top{margin-bottom:6px}
}
@media(max-width:640px){
  .ln-links a:not(.ln-cta-sm){display:none}
  .why-grid,.feat-grid{grid-template-columns:1fr}
  .lane{grid-template-columns:1fr;gap:6px}
  .now-line{left:0}
  .ln-stat{padding:18px 16px}
  .hc-frame{padding:16px}
  .hc-row{gap:3px}.hc-seats{gap:4px}
}
@media(prefers-reduced-motion:reduce){
  .landing *,.landing *::before,.landing *::after{animation:none!important;transition:none!important}
  .ln-title em,.ln-lead,.ln-ctas,.ln-fine,.ln-visual{transform:none!important;opacity:1!important}
  .js-reveal .reveal{opacity:1;transform:none}
  .ln-title .strike em{color:#b4afbb}.ln-title .strike em::after{transform:none}
}

/* ================= tools, in the landing's style ================= */
.app-bar{position:sticky;top:0;z-index:31;background:rgba(243,242,239,.9);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  border-bottom:1px solid var(--edge)}
.app-bar .wrap{display:flex;align-items:center;gap:clamp(10px,2vw,22px);height:64px}
.app-nav{display:flex;gap:4px}
.app-nav a{padding:8px 15px;border-radius:99px;text-decoration:none;color:var(--dim);font-weight:600;font-size:15px;transition:background .2s,color .2s}
.app-nav a:hover{color:var(--ink);background:rgba(29,26,33,.06)}
.app-nav a.on{background:var(--ink);color:#fff}
body.list-seen .app-nav a.on{background:none;color:var(--dim)}
body.list-seen .app-nav [data-nav=watches]{background:var(--ink);color:#fff}
.app-bar .me{margin:0 0 0 auto;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
header.t-head{background:none;border:0;box-shadow:none;padding:clamp(28px,4.5vw,60px) 0 6px}
.t-title{font:800 clamp(54px,7.2vw,112px)/.86 var(--display);text-transform:uppercase;letter-spacing:.005em;margin:14px 0 16px;color:var(--ink)}
.t-title span{color:var(--marquee)}
.t-title.anim{animation:t-rise .7s cubic-bezier(.2,.8,.2,1)}
header.t-head .sub{font-size:clamp(16px,1.35vw,19px);line-height:1.55;max-width:60ch;margin:0;color:var(--dim)}
.t-status{display:flex;flex-wrap:wrap;align-items:center;gap:10px 18px;margin-top:20px}
.t-status .health,.t-status .live{margin:0}
.armed{border-radius:14px;border:0;border-left:5px solid var(--marquee);background:#fbe6eb;padding:12px 16px;margin-top:16px}

/* headings: big marquee type with a red word */
.wrap h2{font:800 clamp(32px,3.5vw,52px)/.95 var(--display);letter-spacing:.01em;text-transform:uppercase;margin:56px 0 18px;color:var(--ink)}
.wrap h2 span{color:var(--marquee)}
.opened-head h2{margin:0}
.opened-head{margin-bottom:4px}

/* buttons: the landing's red button and ghost button */
button{border-radius:12px;transition:transform .15s,box-shadow .15s,background .15s,color .15s}
button:not([class]){letter-spacing:.08em;box-shadow:0 10px 22px rgba(163,18,58,.22)}
button:not([class]):hover{transform:translateY(-1px);box-shadow:0 14px 28px rgba(163,18,58,.3)}
.seg button,button:disabled{box-shadow:none!important;transform:none!important}
button.g{border:0;box-shadow:inset 0 0 0 1.5px var(--ink);background:transparent;color:var(--ink);font-weight:600}
button.g:hover{background:var(--ink);color:#fff;border:0}
select,input{border-radius:12px}

/* cards: rounder, and they lift */
.choice,.venue-card,.mv-group,.prefs,.venue-tile,.gap-panel,.join-card,.stub,.start,.op,.cal-row,.alerts-card,.empty{border-radius:18px}
.choice,.venue-tile,.op,.cal-row{transition:transform .25s,box-shadow .25s}
.choice:hover,.venue-tile:hover,.cal-row:hover{transform:translateY(-4px);box-shadow:0 0 0 2px var(--marquee),0 22px 40px rgba(29,26,33,.1)}
.op:hover{transform:translateY(-3px)}
.choice{padding:26px}
.choice svg{width:48px;height:48px;padding:10px;border-radius:14px;background:var(--paper)}
.choice b{font-size:34px}
.alerts-card{background:#fff;box-shadow:var(--card-shadow);padding:clamp(20px,2.6vw,32px);margin-top:44px}
.alerts-card h2{margin-top:0}
.alerts-card .hint:last-child{margin-bottom:0}
.prefs{padding:18px 22px}

/* your watches: the dark band, like the landing's "how it works" */
.band-dark{background:var(--ink);color:#fff;padding:clamp(44px,6vw,84px) 0 clamp(48px,6vw,90px);margin-top:64px}
.band-dark h2{color:#fff;margin-top:0}
.band-dark .hint{color:#bdb8c4}
.band-dark .band-sub{font-size:clamp(15px,1.2vw,17px);max-width:62ch;margin:-4px 0 22px}
.band-dark .empty{color:#cfcad6;background:rgba(255,255,255,.05);border:1.5px dashed rgba(255,255,255,.2)}
.band-dark .stub{box-shadow:0 18px 40px rgba(0,0,0,.35)}
.band-dark button.g{box-shadow:inset 0 0 0 1.5px rgba(255,255,255,.7);color:#fff}
.band-dark .stub button.g{box-shadow:inset 0 0 0 1.5px var(--ink);color:var(--ink)}
.band-dark .stub button.g:hover{color:#fff}
.site-foot{background:#141117;color:#bdb8c4;padding:44px 0 48px}
.site-foot{margin-top:0;border-top:0}
body.has-bar{padding-bottom:0}body.has-bar .site-foot{padding-bottom:124px}
.sf-top{display:flex;align-items:baseline;gap:18px;flex-wrap:wrap;margin-bottom:12px}
.site-foot .ln-logo{color:#fff!important}
.sf-tag{font:800 20px/1 var(--display);letter-spacing:.06em;text-transform:uppercase;color:#6f6977}
.sf-stat{font-size:13px;color:#8f8a97}
.site-foot .credit{color:#8f8a97;margin:8px 0 0;max-width:80ch}
.stale-note{top:76px}

/* a little motion when switching sections */
.view.on{animation:t-in .5s cubic-bezier(.2,.8,.2,1)}
@keyframes t-rise{from{opacity:0;transform:translateY(24px)}to{opacity:1;transform:none}}
@keyframes t-in{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}

@media(max-width:760px){
  .app-nav{display:none}
  .app-bar .wrap{height:56px}
  .app-bar .ln-logo{font-size:24px}
  .t-title{font-size:clamp(46px,13vw,64px)}
  .wrap h2{font-size:clamp(30px,8.6vw,40px);margin-top:44px}
  .choice b{font-size:28px}
  .band-dark{margin-top:48px}
  .site-foot{padding-bottom:calc(36px + 72px + env(safe-area-inset-bottom))}
  body,body.has-bar,body.bar-urgent{padding-bottom:0}
  body.bar-urgent .site-foot{padding-bottom:calc(36px + 132px + env(safe-area-inset-bottom))}
}
@media(prefers-reduced-motion:reduce){.view.on,.t-title.anim{animation:none}button,.choice,.venue-tile,.op,.cal-row{transition:none}}
</style></head><body>
<script>if(location.hash&&!/^#home/.test(location.hash))document.body.classList.add('off-home');</script>
<div class="landing" id="landing">
  <nav class="ln-nav" aria-label="Seat Watch">
    <div class="ln-wrap">
      <a class="ln-logo" href="#ln-top" data-top>Seat <b>Watch</b></a>
      <div class="ln-links"><a href="#ln-why">Why it works</a><a href="#ln-how">How it works</a>
        <a class="ln-cta-sm" href="#tools" data-go-tools>Open the tools</a></div>
    </div>
  </nav>

  <section class="ln-hero" id="ln-top">
    <div class="ln-wrap">
      <div class="ln-hero-grid">
        <div>
          <p class="ln-kicker"><i></i>Hyderabad · BookMyShow seat alerts</p>
          <h1 class="ln-title" aria-label="Housefull isn't the end">
            <span class="strike" aria-hidden="true"><em>Housefull</em></span>
            <span aria-hidden="true"><em>isn't the</em></span>
            <span aria-hidden="true"><em class="red">end.</em></span>
          </h1>
          <p class="ln-lead"><span class="long">After a show sells out, <b>the best seats come back</b>: blocks the cinema held back, bookings
            nobody paid for, last-minute cancellations. They're gone in minutes. Seat Watch keeps watching BookMyShow,
            <b>buzzes your phone the moment they open</b>, and can <b>hold them for you</b> while you pay.</span><span
            class="short">The best seats come back after a show sells out. Seat Watch <b>buzzes your phone the moment
            they do</b> and can <b>hold them while you pay</b>.</span></p>
          <div class="ln-ctas">
            <a class="ln-btn" href="#tools" data-go-tools>Find my show
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg></a>
            <a class="ln-btn ghost" href="#ln-why"><span class="btn-long">Why seats come back</span><span class="btn-short">How it works</span></a>
          </div>
          <p class="ln-fine">No refreshing. Nothing is booked without your OK.</p>
        </div>
        <div class="ln-visual" aria-hidden="true">
          <div class="hc-frame">
            <div class="bulbs" id="ln-bulbs"></div>
            <div class="hc">
              <div class="hc-top"><b>Screen 1</b><span>Saturday · 7:30 PM</span></div>
              <div class="hc-toast" id="hc-toast"><span class="bell"><svg viewBox="0 0 24 24"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 8 3 8H3s3-1 3-8"/><path d="M10 20a2 2 0 0 0 4 0"/></svg></span>
                <div><b>Seats just opened</b><span>GOLD · F7–F9 · middle of the hall</span></div></div>
              <div class="hc-seats" id="hc-seats"></div>
              <div class="hc-screen"></div><div class="hc-screen-t">SCREEN</div>
              <div class="hc-stamp" id="hc-stamp">Housefull</div>
              <div class="hc-held" id="hc-held">Held in <span id="hc-sec">0.0</span> s · pay with your UPI app</div>
            </div>
          </div>
          <div class="hc-legend"><span><i style="background:#dcd8e1"></i>sold</span><span><i style="background:#1f8a5b"></i>just released</span>
            <span><i style="background:#e0a526"></i>held for you</span></div>
        </div>
      </div>
      <a class="ln-scroll" href="#ln-why">Scroll<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></a>
    </div>
  </section>

  <div class="ln-ticker" id="ln-ticker" hidden><b><i></i>Just opened</b><div class="tk-mask"><div class="tk-track" id="tk-track"></div></div></div>

  <section class="ln-sec" id="ln-why">
    <div class="ln-wrap">
      <h2 class="ln-h2 reveal">Sold out<br><span>isn't final.</span></h2>
      <p class="ln-sub reveal d1">Seats go back on sale all the time, usually a few at once and with no warning.
        Here's where they come from:</p>
      <div class="why-grid">
        <article class="why reveal d1">
          <div class="why-art art-lock" aria-hidden="true">
            <div class="mini"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i>
              <i></i><i></i><i class="b"></i><i class="b"></i><i class="b"></i><i class="b"></i><i></i><i></i>
              <i></i><i></i><i class="b"></i><i class="b"></i><i class="b"></i><i class="b"></i><i></i><i></i></div>
            <svg class="lock" viewBox="0 0 24 24"><path class="shackle" d="M8 11V8a4 4 0 0 1 8 0v3" fill="none" stroke="#1d1a21" stroke-width="2.2"/>
              <rect x="5" y="11" width="14" height="10" rx="2.5" fill="#1d1a21"/></svg>
          </div>
          <h3>Held-back blocks</h3>
          <p>Cinemas often keep some of their best rows aside and put them on sale closer to the show.</p>
        </article>
        <article class="why reveal d2">
          <div class="why-art art-timer" aria-hidden="true">
            <div class="mini"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i>
              <i></i><i></i><i></i><i class="h"></i><i class="h"></i><i class="h"></i><i></i><i></i>
              <i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
            <svg class="ring" viewBox="0 0 44 44"><circle cx="22" cy="22" r="18" fill="none" stroke="#dcd8e1" stroke-width="4"/>
              <circle class="run" cx="22" cy="22" r="18" fill="none" stroke="#e0a526" stroke-width="4" transform="rotate(-90 22 22)"/>
              <text x="22" y="26" text-anchor="middle">pay</text></svg>
          </div>
          <h3>Unpaid bookings</h3>
          <p>Someone picks seats, then never pays. A few minutes later those seats are back on sale.</p>
        </article>
        <article class="why reveal d3">
          <div class="why-art art-cancel" aria-hidden="true">
            <div class="mini"><i></i><i></i><i></i><i class="c2"></i><i></i><i></i><i></i><i></i>
              <i></i><i class="c1"></i><i></i><i></i><i></i><i></i><i class="c3"></i><i></i>
              <i></i><i></i><i></i><i></i><i class="c1"></i><i></i><i></i><i></i></div>
          </div>
          <h3>Cancellations</h3>
          <p>Plans change. Seats free up one or two at a time, right up to showtime.</p>
        </article>
      </div>

      <div class="ln-stats">
        <div class="ln-stat reveal"><b>24/7</b><span>watching, so you can sleep</span></div>
        <div class="ln-stat reveal d1"><b><span data-to="3">0</span> s</b><span>between checks while an auto-hold waits</span></div>
        <div class="ln-stat reveal d2"><b><span data-to="90" id="ln-cinemas">0</span>+</b><span>Hyderabad cinemas to choose from</span></div>
        <div class="ln-stat reveal d3"><b>~<span data-to="4">0</span> s</b><span>to hold seats up to the payment page</span></div>
      </div>
    </div>
  </section>

  <section class="ln-sec" style="padding-top:0">
    <div class="ln-wrap">
      <div class="race reveal">
        <h2 class="ln-h2" style="font-size:clamp(40px,5.4vw,80px)">The catch: <span>they vanish.</span></h2>
        <p class="ln-sub" style="margin-bottom:0">Released seats show up at random moments and rarely last long. Check by hand and
          you'll keep looking at the wrong moment. <b>Seat Watch never stops looking.</b></p>
        <div class="lanes" aria-hidden="true">
          <div class="now-line"><i></i></div>
          <div class="lane lane-rel"><span>Seats released<small>random, brief</small></span>
            <div class="track"><i class="pt" style="--p:19"></i><i class="pt" style="--p:44"></i><i class="pt" style="--p:69"></i><i class="pt" style="--p:84"></i></div></div>
          <div class="lane lane-you"><span>You, refreshing<small>every now and then</small></span>
            <div class="track"><i class="pt" style="--p:8"></i><i class="pt" style="--p:33"></i><i class="pt" style="--p:58"></i><i class="pt" style="--p:94"></i></div></div>
          <div class="lane lane-sw"><span>Seat Watch<small>every few seconds</small></span>
            <div class="track"><i class="pt" style="--p:19"></i><i class="pt" style="--p:44"></i><i class="pt" style="--p:69"></i><i class="pt" style="--p:84"></i></div></div>
        </div>
        <div class="race-note"><span><i style="background:#1f8a5b"></i>seats on sale</span><span><i style="background:#1d1a21"></i>you look: nothing there</span>
          <span><i style="background:var(--marquee)"></i>Seat Watch catches it and tells you</span></div>
      </div>
    </div>
  </section>

  <section class="ln-sec ln-how" id="ln-how">
    <div class="ln-wrap">
      <h2 class="ln-h2 reveal">How you get <span>them</span></h2>
      <p class="ln-sub reveal d1">Set it up once. Seat Watch does the waiting.</p>
      <div class="story">
        <div class="story-stage" id="ln-stage" data-step="1" aria-hidden="true">
          <div class="scene sc1"><div class="card-w">
            <div class="sc-head"><b>Housefull</b><svg class="refresh" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4v5h-5"/></svg></div>
            <div class="grid10" id="sc1-grid"></div>
            <div class="sc-cap">Seats were back for a few seconds… and gone before the page reloaded.</div></div></div>
          <div class="scene sc2"><div class="radar"><i></i><i></i><i></i>
            <div class="core"><div><b id="sc2-n">3</b><span>next check</span></div></div>
            <span class="tag t1">GOLD · still full</span><span class="tag t2">Rows F–H · watching</span><span class="tag t3">IMAX · 9:45 PM</span></div></div>
          <div class="scene sc3"><div class="phone"><div class="clock">6:12</div><div class="date">Saturday</div>
            <div class="note"><span class="bell"><svg viewBox="0 0 24 24"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 8 3 8H3s3-1 3-8"/><path d="M10 20a2 2 0 0 0 4 0"/></svg></span>
              <div><b>Seats free: GOLD F6–F8</b><span>Screen 1 · 7:30 PM. Tap to open the seats.</span></div></div></div></div>
          <div class="scene sc4"><div class="card-w hold-card">
            <div class="sc-head"><b style="color:#b07d10">Held for you</b><span>Screen 1 · 7:30 PM</span></div>
            <div class="grid10" id="sc4-grid"></div>
            <div class="hold-row"><div class="watch"><span id="sc4-sec">0.0</span> s<small>seats to payment page</small></div>
              <div class="pay">Pay with UPI ›</div></div></div></div>
        </div>
        <div class="story-steps">
          <div class="step on" data-step="1"><span class="n">01 · The problem</span><h3>Refreshing doesn't work</h3>
            <p>Released seats appear and vanish between your refreshes. To catch them yourself you'd have to look every
              few seconds, all day, and still be fast enough to book.</p></div>
          <div class="step" data-step="2"><span class="n">02 · We watch</span><h3>We look so you don't have to</h3>
            <p>Pick a show, a premium screen, a whole cinema or date (even before it's listed), or <b>exact rows on the
              seat map</b>. Seat Watch checks BookMyShow around the clock, <b>every 3 seconds</b> for shows you want held.</p></div>
          <div class="step" data-step="3"><span class="n">03 · You hear first</span><h3>Your phone buzzes first</h3>
            <p>The moment a category opens or seats in your rows free up, you get an alert through the free ntfy app,
              with a link straight to the seats. Quiet hours keep nights silent.</p></div>
          <div class="step" data-step="4"><span class="n">04 · We grab them</span><h3>We hold them while you pay</h3>
            <p>Ask for auto-hold and the <b>best seats together</b> (middle and back rows first) are held right up to the
              payment page in a few seconds. You pay from your own UPI app. <b>Nothing is booked without your OK.</b></p></div>
        </div>
      </div>
    </div>
  </section>

  <section class="ln-sec">
    <div class="ln-wrap">
      <h2 class="ln-h2 reveal">Made for the <span>big nights</span></h2>
      <p class="ln-sub reveal d1">First-day shows, IMAX and Dolby screens, the seats everyone fights for.</p>
      <div class="feat-grid">
        <div class="feat reveal"><svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>
          <h3>Before it's even listed</h3><p>Track a date or a film before bookings open. You're told the moment they do.</p></div>
        <div class="feat reveal d1"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="5" height="5" rx="1"/><rect x="10" y="4" width="5" height="5" rx="1"/><rect x="17" y="4" width="4" height="5" rx="1"/><rect x="3" y="12" width="5" height="5" rx="1"/><rect x="10" y="12" width="5" height="5" rx="1" fill="currentColor"/><path d="M5 21h14"/></svg>
          <h3>Only your rows</h3><p>Pick rows or categories on the real seat map. Alerts come only when those seats free up.</p></div>
        <div class="feat reveal d2"><svg viewBox="0 0 24 24"><path d="M2 7h20v10H2z"/><path d="M6 7v10M18 7v10"/><circle cx="12" cy="12" r="2.5"/></svg>
          <h3>Premium screens</h3><p>IMAX, Dolby, 4DX, recliners: track just the screen you want at a cinema.</p></div>
        <div class="feat reveal"><svg viewBox="0 0 24 24"><circle cx="8" cy="8" r="3"/><circle cx="16" cy="8" r="3"/><path d="M2 20c0-3.3 2.7-6 6-6s6 2.7 6 6M14 14.2c.6-.1 1.3-.2 2-.2 3.3 0 6 2.7 6 6"/></svg>
          <h3>Seats together</h3><p>Group booking: friends join one hold, seats come together, everyone pays back by UPI.</p></div>
        <div class="feat reveal d1"><svg viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
          <h3>Quiet hours</h3><p>Alerts arrive silently at night. Seats held for you still ring.</p></div>
        <div class="feat reveal d2"><svg viewBox="0 0 24 24"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>
          <h3>Your own account</h3><p>Run your own holder and the tickets land in your own BookMyShow account.</p></div>
      </div>
    </div>
  </section>

  <section class="ln-final">
    <div class="beam"></div><div class="beam b2"></div>
    <div class="ln-wrap">
      <h2 class="ln-h2 reveal">Stop refreshing.<br><span>Start watching.</span></h2>
      <p class="reveal d1">Pick a movie or a cinema below. Setting up your first watch takes about a minute.</p>
      <a class="ln-btn reveal d2" href="#tools" data-go-tools>Find my show
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M6 13l6 6 6-6"/></svg></a>
    </div>
  </section>
</div>
<div id="tools"></div>
<script>
(function(){
  const L=document.getElementById('landing');if(!L)return;
  const g=id=>document.getElementById(id);
  const calm=window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)').matches;
  L.classList.add('js-reveal');

  // marquee bulbs around the hero sign
  const bulbs=g('ln-bulbs');
  function placeBulbs(){
    const w=bulbs.clientWidth,h=bulbs.clientHeight;if(!w)return;
    const gap=22,inset=9,pts=[];
    for(let x=inset+12;x<=w-inset-12;x+=gap)pts.push([x,inset],[x,h-inset]);
    for(let y=inset+12+gap;y<=h-inset-12-gap/2;y+=gap)pts.push([inset,y],[w-inset,y]);
    bulbs.innerHTML=pts.map(([x,y])=>`<i style="left:${x}px;top:${y}px"></i>`).join('');
  }

  // hero: housefull -> the best seats come back -> phone alert -> held for you
  const ROWS='ABCDEFGHIJ'.split(''),N=16,box=g('hc-seats');
  box.innerHTML=ROWS.map(r=>`<div class="hc-row"><em>${r}</em>${Array.from({length:N},(_,i)=>
    `<i class="s${i===3||i===12?' gap':''}" data-k="${r}${i}"></i>`).join('')}<em>${r}</em></div>`).join('');
  const S=k=>box.querySelector(`[data-k="${k}"]`);
  const REL=['F7','F8','F9','G6','E8','G7','H5','G8','H10','G9'],HELD=['F7','F8','F9'],GONE=['G6','H5','H10','G9'];
  const stamp=g('hc-stamp'),toast=g('hc-toast'),held=g('hc-held'),sec=g('hc-sec');
  let timers=[];
  const at=(ms,fn)=>timers.push(setTimeout(fn,ms));
  function count(el,to,ms,dec){const t0=performance.now();const step=()=>{const k=Math.min(1,(performance.now()-t0)/ms);
    el.textContent=(to*k).toFixed(dec);if(k<1)requestAnimationFrame(step);};step();setTimeout(()=>el.textContent=to.toFixed(dec),ms+50);}
  function finalState(){REL.forEach(k=>S(k).className='s'+(HELD.includes(k)?' held':GONE.includes(k)?'':' free'));
    toast.classList.add('on');held.classList.add('on');sec.textContent='3.4';}
  function cycle(){
    timers.forEach(clearTimeout);timers=[];
    box.querySelectorAll('.s:not(.gap)').forEach(s=>s.className='s');
    [stamp,toast,held].forEach(e=>e.classList.remove('on'));sec.textContent='0.0';
    at(500,()=>stamp.classList.add('on'));
    at(2300,()=>stamp.classList.remove('on'));
    REL.forEach((k,i)=>at(2600+i*130,()=>S(k).classList.add('free')));
    at(4000,()=>toast.classList.add('on'));
    at(5200,()=>{HELD.forEach(k=>{S(k).classList.remove('free');S(k).classList.add('held');});GONE.forEach(k=>S(k).classList.remove('free'));});
    at(5350,()=>{held.classList.add('on');count(sec,3.4,800,1);});
    at(6800,()=>toast.classList.remove('on'));
    at(9800,cycle);
  }
  if(calm)finalState();else cycle();

  // story scenes: seat grids
  const grid=(el,flick,hold)=>{el.innerHTML=Array.from({length:50},(_,i)=>`<i class="${flick.includes(i)?'flick':''}${hold.includes(i)?'h':''}"></i>`).join('');};
  grid(g('sc1-grid'),[23,24,25,34],[]);grid(g('sc4-grid'),[],[24,25,26]);
  let n=3;setInterval(()=>{n=n<=1?3:n-1;const e=g('sc2-n');if(e)e.textContent=n;},1000);

  // scrolling: nav shadow, reveals, the sticky story, count-ups
  const nav=L.querySelector('.ln-nav'),reveals=[...L.querySelectorAll('.reveal')];
  const steps=[...L.querySelectorAll('.step')],stage=g('ln-stage');
  function onStep(k){if(k==='4')count(g('sc4-sec'),3.4,900,1);}
  function check(){
    if(document.body.classList.contains('off-home'))return;
    nav.classList.toggle('solid',scrollY>8);
    const h=innerHeight;
    for(const el of reveals){
      if(el.classList.contains('in'))continue;
      if(el.getBoundingClientRect().top<h*.9){el.classList.add('in');
        el.querySelectorAll('[data-to]').forEach(c=>count(c,+c.dataset.to,calm?0:1100,0));}
    }
    let best=null,bd=1e9;const mid=h/2;
    if(innerWidth<=980){          // stage on top: the step whose heading has come up into view
      for(const s of steps)if(s.querySelector('h3').getBoundingClientRect().top<h*.8)best=s;
      best=best||steps[0];
    }else for(const s of steps){const r=s.getBoundingClientRect();const d=Math.abs(r.top+r.height/2-mid);if(d<bd){bd=d;best=s;}}
    if(best&&stage.dataset.step!==best.dataset.step){stage.dataset.step=best.dataset.step;
      steps.forEach(s=>s.classList.toggle('on',s===best));onStep(best.dataset.step);}
  }
  let wait=0;
  addEventListener('scroll',()=>{if(wait)return;wait=setTimeout(()=>{wait=0;check();},50);},{passive:true});
  addEventListener('resize',()=>{placeBulbs();check();});
  placeBulbs();check();setTimeout(()=>{placeBulbs();check();},400);
  setInterval(check,500);          // backup for browsers that skip scroll events (and for jumps by link)

  // in-page links scroll smoothly (and don't change the address, which drives the tools' views)
  L.addEventListener('click',e=>{
    const a=e.target.closest('a[href^="#ln-"],a[data-go-tools]');if(!a)return;
    e.preventDefault();
    const smooth={behavior:calm?'auto':'smooth',block:'start'};
    if(a.hasAttribute('data-top')){window.scrollTo({top:0,behavior:smooth.behavior});return;}
    if(a.hasAttribute('data-go-tools')){
      if(location.hash&&!/^#home/.test(location.hash))location.hash='#home';
      g('tools').scrollIntoView(smooth);
      if(window.matchMedia&&matchMedia('(hover: hover)').matches)
        setTimeout(()=>{const f=g('find');if(f&&f.offsetParent)f.focus({preventScroll:true});},800);
      return;
    }
    const t=document.querySelector(a.getAttribute('href'));if(t)t.scrollIntoView(smooth);
  });

  // real numbers and real openings (no BookMyShow requests: both come from what the site already has)
  fetch('/api/venues').then(r=>r.json()).then(d=>{const c=(d.venues||[]).length;if(c){const e=g('ln-cinemas');
    e.dataset.to=Math.floor(c/10)*10;if(e.closest('.reveal.in'))e.textContent=e.dataset.to;}}).catch(()=>{});
  const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  fetch('/api/openings').then(r=>r.json()).then(d=>{
    const l=(d.openings||[]).slice(0,14);if(!l.length)return;
    const item=o=>`<a href="${o.venue?'#venues/'+encodeURIComponent(o.venue)+'/'+esc(o.date):'#movies'}"${!o.venue&&o.movie_code?` data-recent-movie="${esc(o.movie_code)}"`:''}>`+
      `<i>${esc(o.what)}</i>${esc(o.movie||'')}${o.show_time?' · '+esc(o.show_time):''}${o.venue_name?' · '+esc(o.venue_name):''}</a>`;
    const html=l.map(item).join('');g('tk-track').innerHTML=html+html;
    g('tk-track').style.animationDuration=Math.max(30,l.length*7)+'s';g('ln-ticker').hidden=false;
  }).catch(()=>{});
})();
</script>
<div class="app-bar" id="app-bar"><div class="wrap">
  <a class="ln-logo" href="/" title="Back to the start">Seat <b>Watch</b></a>
  <nav class="app-nav" aria-label="Sections">
    <a href="#home" data-nav="home">Home</a><a href="#movies" data-nav="movies">Movies</a><a href="#venues" data-nav="venues">Cinemas</a>
    <a href="#" data-nav="watches" onclick="$('list').scrollIntoView({behavior:'smooth',block:'start'});return false">My watches</a>
  </nav>
  <div id="me" class="me"></div>
</div></div>
<header class="t-head"><div class="wrap">
  <p class="ln-kicker"><i></i><span id="t-kicker">Seat Watch tools</span></p>
  <h1 class="t-title" id="t-title">Find your <span>show.</span></h1>
  <p class="sub" id="sub">Get told the moment seats open at the cinemas you care about.</p>
  <div class="t-status">
    <div id="health" class="health" aria-label="System status"></div>
    <div id="live" class="live" role="status"><span class="dot"></span> Checking auto-hold…</div>
  </div>
  <div id="armed" class="armed" role="alert" hidden><b>Real bookings are ON.</b> Approved auto-holds reserve real seats
    on the owner's BookMyShow account and go to the payment page.</div>
</div></header>
<div class="wrap">

  <div id="home-top" class="home-top">
    <div id="start" class="start" hidden></div>
    <div class="finder">
      <label for="find" class="find-label">Find a movie or cinema</label>
      <div class="find-box">
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <input id="find" type="search" placeholder="Movie name, cinema or area" autocomplete="off"
          role="combobox" aria-expanded="false" aria-controls="find-list" aria-autocomplete="list">
        <div id="find-list" class="find-list" role="listbox" hidden></div>
      </div>
    </div>
    <div class="opened">
      <div class="opened-head"><h2>Just <span>opened</span></h2>
        <span class="hint">seats and shows that opened at cinemas people here track</span></div>
      <div id="opened" class="opened-strip"><div class="op-empty">Loading…</div></div>
    </div>
    <div id="popular" class="popular" hidden></div>
  </div>

  <section class="alerts-card">
  <h2>Where alerts <span>go</span></h2>
  <div class="topic-row">
    <input id="topic" class="mono" placeholder="your notification ID" aria-label="Your notification ID">
    <button class="g" onclick="makeTopic()">Create my notification ID</button>
    <button class="g" onclick="toggleSetup()">How to get alerts</button>
  </div>
  <div id="setup" class="setup" style="display:none">
    <ol>
      <li>Install the free <b>ntfy</b> app:
        <a href="https://play.google.com/store/apps/details?id=io.heckel.ntfy" target="_blank" rel="noopener">Android</a> ·
        <a href="https://apps.apple.com/app/ntfy/id1625396347" target="_blank" rel="noopener">iPhone</a></li>
      <li>In the app, subscribe to your notification ID: <span id="sub-links" class="hint">create or enter one first</span></li>
      <li><button class="g" onclick="testAlert()">Send me a test alert</button> <span id="test-msg" class="hint"></span></li>
    </ol>
    <div id="topic-qr" class="topic-qr"></div>
  </div>
  <p class="hint">Your notification ID is private: anyone who knows it can see your alerts, so keep it unguessable.</p>
  </section>

  <section id="v-home" class="view">
    <h2>What do you want to <span>track?</span></h2>
    <div class="choices">
      <a class="choice" href="#movies">
        <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M17 9h4M3 15h4M17 15h4"/></svg>
        <b>A movie</b><span>Pick a movie, see every cinema and showtime playing it, and track a show, a screen or a date.</span>
      </a>
      <a class="choice" href="#venues">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 21V9l9-6 9 6v12"/><path d="M9 21v-6h6v6"/><path d="M3 21h18"/></svg>
        <b>A cinema</b><span>Pick a cinema, see everything it's showing now and on future dates, and track a show, a screen or the whole venue.</span>
      </a>
    </div>
    <div id="recent" class="recent"></div>
    <details id="myholder-box" class="prefs" ontoggle="if(this.open)loadMyHolder()">
      <summary>Use my own BookMyShow account <span class="hint">auto-hold on your account, tickets come to you</span></summary>
      <div id="myholder"><span class="hint">Loading…</span></div>
    </details>
    <details id="prefs-box" class="prefs">
      <summary>My preferences <span class="hint">saved to your name, used to fill in every form</span></summary>
      <div class="fields">
        <div><label for="pf-qty">Seats I usually book</label><select id="pf-qty"></select></div>
        <div><label for="pf-max">Max total incl. fees (&#8377;)</label><input id="pf-max" type="number" min="0" placeholder="no cap"></div>
        <div><label for="pf-cats">Preferred categories</label><input id="pf-cats" placeholder="e.g. GOLD, RECLINER"></div>
        <div><label for="pf-rows">Preferred rows</label><input id="pf-rows" placeholder="e.g. F, G, H"></div>
        <div><label for="pf-formats">Favourite formats</label><input id="pf-formats" list="fmt-list" placeholder="formats you like, comma separated"><datalist id="fmt-list"></datalist></div>
        <div><label for="pf-langs">Languages</label><input id="pf-langs" placeholder="e.g. Telugu, Hindi"></div>
        <div><label>Quiet hours <span class="hint">(alerts arrive silently; holds and payments still ring)</span></label>
          <div class="row"><input id="pf-qfrom" type="time" style="width:auto"> to <input id="pf-qto" type="time" style="width:auto"></div></div>
        <div><label for="pf-areas">My areas</label><input id="pf-areas" list="area-list" placeholder="e.g. Gachibowli, Kukatpally">
          <datalist id="area-list"></datalist></div>
      </div>
      <label>Starred cinemas <span class="hint">(star them on the cinema list; they're shown first everywhere)</span></label>
      <div id="pf-venues" class="hint">none yet</div>
      <button onclick="savePrefs()">Save preferences</button> <span id="pf-msg" class="hint"></span>
    </details>
  </section>

  <section id="v-movies" class="view">
  <h2>Choose a <span>movie</span></h2>
  <div class="seg" id="mtabs"><button class="on" data-mtab="now">Now showing</button><button data-mtab="soon">Coming soon</button></div>
  <p class="hint" id="mtab-hint">Now showing in Hyderabad. Select a movie to see its cinemas and times.</p>
  <div id="soon-cal" class="soon-cal" hidden></div>
  <div class="catalog" id="catalog">
    <nav id="cats" class="cats" aria-label="Categories"></nav>
    <div id="movie-grid" class="movie-grid" aria-label="Movies"><div class="empty">Loading movies…</div></div>
  </div>
  <label for="movie-link">Movie not listed?</label>
  <div class="movie-add"><input id="movie-link" placeholder="Paste its BookMyShow movie link"><button class="g" onclick="addMovie()">Add movie</button></div>
  <p id="movie-msg" class="hint" role="status"></p>

  <section id="schedule" class="schedule" style="display:none">
    <h2 id="movie-title">Shows</h2>
    <div class="date-slider">
      <button class="arrow" onclick="slideDates(-1)" aria-label="Earlier dates">&#8249;</button>
      <div id="date-strip" class="date-strip" aria-label="Choose a date"></div>
      <button class="arrow" onclick="slideDates(1)" aria-label="Later dates">&#8250;</button>
    </div>
    <div class="date-legend"><span><b></b>shows on BookMyShow</span><span>grey: no shows yet</span>
      <span>dashed: not listed yet, you can still track it</span></div>
    <div id="mv-filters" class="mv-filters"></div>
    <div id="mw-panel" class="gap-panel mw-panel">
      <h3 id="mw-title">Track this movie in my areas</h3>
      <p class="hint">Get told when bookings open or a new cinema or show appears that matches the areas and
        formats picked above (none picked = anywhere, any format). Works for films not bookable yet, too.</p>
      <div class="row"><label for="mw-date" style="margin:0">Date</label>
        <select id="mw-date" style="width:auto"></select>
        <button onclick="trackEverywhere()">Track it</button> <span id="mw-msg" class="hint"></span></div>
    </div>
    <div id="mv-hold" class="hold-form vd-hold"></div>
    <p id="loadmsg" class="hint" role="status"></p>
    <div id="venue-shows"></div>
    <div class="gap-panel">
      <h3>No show listed at your cinema or screen?</h3>
      <p class="hint">Track this movie for the selected date. We will notify you when a matching show appears.</p>
      <div class="fields">
        <div><label for="venue">Cinema</label><input id="vsearch" placeholder="Filter cinemas" oninput="filterVenues()"><select id="venue" aria-label="Cinema"></select></div>
        <div><label for="screen">Screen or format</label><select id="screen"><option value="">Choose a date with shows first</option></select></div>
      </div>
      <div class="watch-options"><button onclick="watchVenue()">Track movie at this cinema</button><button class="g" onclick="watchSelectedScreen()">Track movie on this screen</button></div>
    </div>
  </section>

  </section>

  <section id="v-venues" class="view">
    <div id="venue-list">
      <h2>Choose a <span>cinema</span></h2>
      <div class="topic-row"><input id="vgrid-search" placeholder="Search cinemas" oninput="renderVenueGrid()">
        <select id="vgrid-area" onchange="renderVenueGrid()" style="width:auto;min-width:180px" aria-label="Area"></select></div>
      <div id="venue-grid" class="venue-grid"><div class="empty">Loading cinemas…</div></div>
    </div>
    <div id="venue-detail" style="display:none">
      <button class="g" onclick="closeVenue()">&#8249; All cinemas</button>
      <h2 id="vd-title">Cinema</h2>
      <div class="date-slider">
        <button class="arrow" onclick="slideVDates(-1)" aria-label="Earlier dates">&#8249;</button>
        <div id="vd-dates" class="date-strip" aria-label="Choose a date"></div>
        <button class="arrow" onclick="slideVDates(1)" aria-label="Later dates">&#8250;</button>
      </div>
      <div class="date-legend"><span><b></b>shows on BookMyShow</span><span>grey: listed, not bookable yet</span>
        <span>dashed: not listed yet, you can still track it</span></div>
      <div class="venue-track">
        <button id="vd-all">Track the whole cinema on this date</button>
        <button class="g" id="vd-screens-btn" style="display:none">Available screens</button>
        <span id="vd-sort"></span>
      </div>
      <div id="vd-hold" class="hold-form vd-hold"></div>
      <p id="vd-msg" class="hint" role="status"></p>
      <div id="vd-shows"></div>
    </div>
  </section>

  <div id="invite-ask" class="name-ask" hidden>
    <form id="iv-form" class="na-card" role="dialog" aria-modal="true" aria-labelledby="iv-title">
      <div class="na-head" id="iv-title">Invite only</div>
      <p>This Seat Watch is shared by invite. Enter the code you were given; you won't be asked again on this device.</p>
      <label for="iv-code">Invite code</label>
      <input id="iv-code" maxlength="40" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="e.g. K7P2QX">
      <div class="row"><button type="submit">Continue</button><button type="button" class="g" id="iv-cancel">Not now</button></div>
      <span id="iv-msg" class="hint" role="status"></span>
    </form>
  </div>
  <div id="name-ask" class="name-ask" hidden>
    <form id="na-form" class="na-card" role="dialog" aria-modal="true" aria-labelledby="na-title">
      <div class="na-head" id="na-title">Admit one</div>
      <p>What's your name? Your watches, requests and preferences are kept under it, so only you see them.
        You won't be asked again on this device.</p>
      <label for="na-name">Your name</label>
      <input id="na-name" maxlength="40" autocomplete="name" placeholder="e.g. Ravi K">
      <div class="row"><button type="submit">Continue</button><button type="button" class="g" id="na-cancel">Not now</button></div>
      <span id="na-msg" class="hint" role="status"></span>
    </form>
  </div>
  <div id="join-card"></div>
  <div id="groups-box"></div>

</div>
<section class="band-dark" id="watches-band"><div class="wrap">
  <h2>Your <span>watches</span></h2>
  <p class="hint band-sub">The website checks these around the clock and sends your alerts, even
    when auto-hold is off. Auto-hold is only needed to reserve seats for you automatically.</p>
  <div id="list"><div class="empty">Enter your notification ID to see your watches.</div></div>
  <p class="hint">Cinema not listed? Ask the owner to add it.</p>
</div></section>
<div class="wrap">
  <nav id="tabbar" class="tabbar" aria-label="Sections">
    <a href="#home" data-tab="home"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 11 12 4l9 7"/><path d="M5 10v10h14V10"/></svg>Home</a>
    <a href="#movies" data-tab="movies"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M17 9h4M3 15h4M17 15h4"/></svg>Movies</a>
    <a href="#venues" data-tab="venues"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 21V9l9-6 9 6v12"/><path d="M9 21v-6h6v6"/></svg>Cinemas</a>
    <a href="#" data-tab="watches" onclick="$('list').scrollIntoView({behavior:'smooth',block:'start'});return false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 8 3 8H3s3-1 3-8"/><path d="M10 20a2 2 0 0 0 4 0"/></svg>My watches<span id="tb-n" class="tb-n" hidden></span></a>
  </nav>
  <button id="mybar" class="mybar" type="button" hidden onclick="$('list').scrollIntoView({behavior:'smooth',block:'start'})"></button>

</div>
<footer class="site-foot"><div class="wrap">
  <div class="sf-top"><a class="ln-logo" href="/">Seat <b>Watch</b></a><span class="sf-tag">Housefull isn't the end.</span></div>
  <div id="stat" class="sf-stat"></div>
  <p class="credit">Movie posters and release dates come from TMDB and Wikipedia. This product uses the TMDB API but is
    not endorsed or certified by TMDB. Seat Watch is a private tool, not affiliated with BookMyShow.</p>
</div></footer>
<script>
const $=i=>document.getElementById(i);
let MOVIES=[], MOVIE=null, DATE=null, VENUES=[], loadSeq=0;
const safe=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const code=d=>d.getFullYear()+String(d.getMonth()+1).padStart(2,'0')+String(d.getDate()).padStart(2,'0');
function filterVenues(){
  const selected=$('venue').value, q=$('vsearch').value.toLowerCase();
  const found=VENUES.filter(v=>v.name.toLowerCase().includes(q));
  found.sort((a,b)=>starred(b.code)-starred(a.code));
  $('venue').innerHTML=found.map(v=>`<option value="${safe(v.code)}">${starred(v.code)?'★ ':''}${safe(v.name)}</option>`).join('');
  if(found.some(v=>v.code===selected)) $('venue').value=selected;
}
let CAT='All', LANG='All', MDATES=[];
const genres=m=>(m.genre||'').split('/').map(g=>g.trim()).filter(Boolean);
const langs=m=>m.languages||[];
function counted(list){
  const n={};list.forEach(x=>n[x]=(n[x]||0)+1);
  return Object.entries(n).sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0]));
}
function renderCats(){
  // counts follow the other filter, so each list shows what you'd actually get
  const byLang=LANG==='All'?MOVIES:MOVIES.filter(m=>langs(m).includes(LANG));
  const byCat=CAT==='All'?MOVIES:MOVIES.filter(m=>genres(m).includes(CAT));
  const btn=(kind,cur,v,n)=>`<button class="cat ${cur===v?'active':''}" data-${kind}="${safe(v)}">${safe(v)}<span>${n}</span></button>`;
  $('cats').innerHTML='<h3>Categories</h3>'+
    [['All',byLang.length],...counted(byLang.flatMap(genres))].map(([g,n])=>btn('cat',CAT,g,n)).join('')+
    '<h3>Languages</h3>'+
    [['All',byCat.length],...counted(byCat.flatMap(langs))].map(([l,n])=>btn('lang',LANG,l,n)).join('');
}
let MTAB='now';
// ---- "Coming soon" as a release calendar: expected release (TMDB / Wikipedia),
// booking dates once BookMyShow lists them, and one tap to be told when they open
let UPC=null, UPC_AT=0, CAL_OFF=false, MW_CODES=new Set();
async function loadUpcoming(){
  if(UPC&&Date.now()-UPC_AT<300000)return renderCalendar();
  $('soon-cal').innerHTML='<div class="empty">Loading the calendar…</div>';
  try{UPC=(await (await fetch('/api/upcoming')).json()).films||[];UPC_AT=Date.now();}catch(e){UPC=[];}
  renderCalendar();
}
const dayOf=s=>{const [y,m,d]=s.split('-').map(Number);return new Date(y,m-1,d);};
const fmtDay=(d,o)=>d.toLocaleDateString('en-IN',o||{weekday:'short',day:'numeric',month:'short'});
function calGroup(f){
  if(!f.release)return [9e15,'Release date not announced'];
  const d=dayOf(f.release),t=new Date();t.setHours(0,0,0,0);
  const days=Math.round((d-t)/864e5);
  if(days<7)return [0,'Out this week'];
  if(days<14)return [1,'Next week'];
  return [d.getFullYear()*100+d.getMonth()+2,fmtDay(d,{month:'long',year:'numeric'})];
}
function renderCalendar(){
  const box=$('soon-cal');if(!UPC)return;
  if(!UPC.length){box.innerHTML='<div class="empty">No upcoming films listed right now.</div>';return;}
  const groups={};
  UPC.forEach(f=>{const [k,label]=calGroup(f);(groups[k]=groups[k]||{label,films:[]}).films.push(f);});
  const keys=Object.keys(groups).map(Number).sort((a,b)=>a-b);
  box.innerHTML=`<div class="cal-tools"><span class="hint">Release dates are expected dates from TMDB or Wikipedia; booking dates come from BookMyShow.</span>
      <button class="link" onclick="CAL_OFF=true;renderMovies()">Show as posters</button></div>`+
    keys.map(k=>{const g=groups[k];
      g.films.sort((a,b)=>(a.release||'9').localeCompare(b.release||'9')||a.title.localeCompare(b.title));
      return `<section class="cal-group"><h3>${safe(g.label)}</h3>${g.films.map(calRow).join('')}</section>`;}).join('');
}
function calRow(f){
  const open=(f.dates||[]).filter(d=>d.open);
  const rel=f.release?`Releases <b>${fmtDay(dayOf(f.release))}</b>`+
    (dayOf(f.release)<new Date(new Date().setHours(0,0,0,0))?' (already out elsewhere)':''):'Release date not announced yet';
  const book=open.length?`<div class="cal-open"><span>Bookings open:</span>${open.slice(0,6).map(d=>
      `<button class="fchip" data-movie="${safe(f.code)}">${fmtDay(new Date(+d.code.slice(0,4),+d.code.slice(4,6)-1,+d.code.slice(6,8)))}</button>`).join('')}
      ${open.length>6?`<span class="hint">+${open.length-6} more</span>`:''}</div>`
    :`<div class="hint">${f.dates===null?'Checking BookMyShow for booking dates…':'Bookings not open yet'}</div>`;
  const on=MW_CODES.has(f.code);
  return `<div class="cal-row"><button class="cal-poster" data-movie="${safe(f.code)}" aria-label="Open ${safe(f.title)}">${posterHtml(f)}</button>
    <div class="cal-main"><button class="cal-title" data-movie="${safe(f.code)}">${safe(f.title)}</button>
      <div class="cal-rel">${rel}</div>${book}</div>
    <div class="cal-act">${on?'<span class="cal-on">&#10003; You\'ll be told</span>'
      :`<button onclick="tellWhenOpen('${safe(f.code)}',this)">${open.length?'Tell me about new dates':'Tell me when bookings open'}</button>`}</div></div>`;
}
async function tellWhenOpen(code,btn){
  const t=$('topic').value.trim();
  if(!t){startAlerts();alert('First set up where your alerts go (top of the page), then tap again.');return;}
  btn.disabled=true;btn.textContent='Saving…';
  const r=await (await fetch('/api/mwatch',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({topic:t,movie:code,date:'',areas:[],formats:[]})})).json().catch(()=>({error:'Could not reach the site.'}));
  if(r.error&&!/already tracking/i.test(r.error)){btn.disabled=false;btn.textContent='Tell me when bookings open';alert(r.error);return;}
  MW_CODES.add(code);renderCalendar();list();
}

function renderMovies(){
  const cal=MTAB==='soon'&&!CAL_OFF;
  $('soon-cal').hidden=!cal;$('catalog').hidden=cal;
  if(cal){loadUpcoming();return;}
  const shown=MOVIES.filter(m=>(MTAB==='soon'?!!m.upcoming:!m.upcoming)&&
    (CAT==='All'||genres(m).includes(CAT))&&(LANG==='All'||langs(m).includes(LANG)));
  $('movie-grid').innerHTML=shown.length?shown.map(m=>`
    <button class="movie-card ${MOVIE?.code===m.code?'active':''}" data-movie="${safe(m.code)}" aria-label="Select ${safe(m.title)}">
      ${posterHtml(m)}
      <strong>${safe(m.title)}</strong><small>${safe(genres(m).join(' / '))}</small>
      ${MOVIES.filter(x=>x.title===m.title).length>1&&langs(m).length?`<small class="lang-tag">${safe(langs(m).join(' / '))}</small>`:''}
      ${m.upcoming?'<span class="soon-tag">Coming soon</span>':''}
    </button>`).join(''):(MOVIES.length?`<div class="empty">No ${MTAB==='soon'?'upcoming ':''}movies match these filters.</div>`:
      '<div class="empty">Movies are unavailable right now. Try again shortly.</div>');
}
function stripHtml(dates,selected){
  // today through the last day BookMyShow lists (at least two weeks), so days
  // before the listing can still be tracked; BMS's own marks say which have shows
  const known=Object.fromEntries(dates.map(d=>[d.code,d.open]));
  const last=dates.length?dates[dates.length-1].code:'';
  const days=[];
  for(let i=0;i<60;i++){
    const d=new Date();d.setDate(d.getDate()+i);const value=code(d);
    if(i>=14&&value>last)break;
    const cls=value in known?(known[value]?'has':'noshow'):'nolist';
    const tip=cls==='has'?'shows on BookMyShow':cls==='noshow'?'listed, no shows yet':'not listed yet; you can still track it';
    days.push(`<button class="date-pill ${cls} ${selected===value?'active':''}" data-date="${value}" title="${tip}" aria-label="${safe(d.toDateString())}, ${tip}">
      <span>${safe(d.toLocaleDateString(undefined,{weekday:'short'}))}</span><b>${d.getDate()}</b><span>${safe(d.toLocaleDateString(undefined,{month:'short'}))}</span><i></i></button>`);
  }
  return days.join('');
}
function showStrip(el,dates,selected){
  el.innerHTML=stripHtml(dates,selected);
  const active=el.querySelector('.active');
  // keep one earlier day fully in view, so it's clear you can slide back
  if(active)el.scrollLeft=Math.max(0,active.offsetLeft-el.offsetLeft-active.offsetWidth-16);
}
function renderDates(){showStrip($('date-strip'),MDATES,DATE);}
function slideDates(dir){$('date-strip').scrollBy({left:dir*Math.max(160,$('date-strip').clientWidth*0.8)});}

// ---------------------------------------------------------------- preferences
let PREFS={};
const pref=(k,d)=>PREFS[k]??d;
const starred=code=>(PREFS.venues||[]).includes(code);
// ---- who's here: browsing needs no name; the first personal action asks for one
let ME='';
const _fetch=window.fetch.bind(window);
window.fetch=async(url,opts)=>{
  let r=await _fetch(url,opts);
  for(let i=0;i<2&&r.status===401;i++){         // invite code first if needed, then a name; each asked once
    const why=(await r.clone().json().catch(()=>({}))).error;
    if(!(why==='need_invite'?await askInvite():why==='need_name'?await askName():false))break;
    r=await _fetch(url,opts);
  }
  if(r.ok&&String(url).startsWith('/api/'))
    r.clone().json().then(d=>{if(d&&d.stale_min)showStale(d.stale_min);}).catch(()=>{});
  return r;
};
let STALE_T=0;
function showStale(min){
  let t=document.getElementById('stale-note');
  if(!t){t=document.createElement('div');t.id='stale-note';t.className='stale-note';t.setAttribute('role','status');document.body.appendChild(t);}
  t.textContent=`BookMyShow is busy, so this is what we saw ${min} min ago. It refreshes by itself.`;
  t.classList.add('on');clearTimeout(STALE_T);STALE_T=setTimeout(()=>t.classList.remove('on'),8000);
}
let INVITE_ASK=null;
function askInvite(){
  if(INVITE_ASK)return INVITE_ASK;
  INVITE_ASK=new Promise(done=>{
    const box=$('invite-ask');box.hidden=false;$('iv-code').value='';$('iv-msg').textContent='';
    setTimeout(()=>$('iv-code').focus(),50);
    const finish=ok=>{box.hidden=true;$('iv-form').onsubmit=null;$('iv-cancel').onclick=null;INVITE_ASK=null;done(ok);};
    $('iv-cancel').onclick=()=>finish(false);
    $('iv-form').onsubmit=async e=>{
      e.preventDefault();
      const r=await (await _fetch('/api/invite',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({code:$('iv-code').value})})).json().catch(()=>({error:'Could not reach the site.'}));
      if(!r.ok){$('iv-msg').textContent=r.error||'That code isn\'t right.';return;}
      finish(true);
    };
  });
  return INVITE_ASK;
}
let NAME_ASK=null;
function askName(){
  if(NAME_ASK)return NAME_ASK;
  NAME_ASK=new Promise(done=>{
    const box=$('name-ask');box.hidden=false;$('na-name').value='';$('na-msg').textContent='';
    setTimeout(()=>$('na-name').focus(),50);
    const finish=ok=>{box.hidden=true;$('na-form').onsubmit=null;$('na-cancel').onclick=null;NAME_ASK=null;done(ok);};
    $('na-cancel').onclick=()=>finish(false);
    $('na-form').onsubmit=async e=>{
      e.preventDefault();
      const name=$('na-name').value.trim();
      if(name.length<2){$('na-msg').textContent='Please enter your name.';return;}
      const r=await (await _fetch('/api/access',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})})).json();
      if(!r.ok){$('na-msg').textContent=r.error||'Could not save your name.';return;}
      setMe(r.name||name);finish(true);
      loadPrefs();list();loadGroups();
    };
  });
  return NAME_ASK;
}
function setMe(name){
  ME=name;setTimeout(renderStart);
  $('me').innerHTML=ME?`Hi <b>${safe(ME)}</b> · <a href="#" onclick="askName();return false">not you?</a>`
    :`<a href="#" onclick="askName();return false">Add your name</a> to keep your watches`;
}
async function loadPrefs(){
  if(!ME){PREFS={};fillPrefsForm();return;}
  try{PREFS=(await (await fetch('/api/prefs')).json()).prefs||{};}catch(e){PREFS={};}
  fillPrefsForm();
}
function fillPrefsForm(){
  $('pf-qty').innerHTML=[1,2,3,4,5,6,7,8,9,10].map(n=>`<option ${n===pref('qty',2)?'selected':''}>${n}</option>`).join('');
  $('pf-max').value=pref('max_total',0)||'';
  $('pf-cats').value=pref('categories',[]).join(', ');
  $('pf-rows').value=pref('rows',[]).join(', ');
  $('pf-formats').value=pref('formats',[]).join(', ');
  $('pf-langs').value=pref('languages',[]).join(', ');
  $('pf-areas').value=pref('areas',[]).join(', ');
  $('pf-qfrom').value=pref('quiet_from','');$('pf-qto').value=pref('quiet_to','');
  const vs=pref('venues',[]);
  $('pf-venues').innerHTML=vs.length?vs.map(c=>{const v=VENUES.find(x=>x.code===c);
    return `<span class="chip">&#9733; ${safe(v?v.name:c)} <span class="star on" data-star="${safe(c)}" title="Unstar">&times;</span></span>`}).join('')
    :'none yet';
}
async function savePrefs(quiet){
  const body=quiet?PREFS:{qty:$('pf-qty').value,max_total:$('pf-max').value,categories:$('pf-cats').value,
    rows:$('pf-rows').value,formats:$('pf-formats').value,languages:$('pf-langs').value,areas:$('pf-areas').value,venues:pref('venues',[]),
    quiet_from:$('pf-qfrom').value,quiet_to:$('pf-qto').value};
  const r=await (await fetch('/api/prefs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(r.prefs)PREFS=r.prefs;
  if(!quiet){$('pf-msg').textContent=r.ok?'Saved.':'Could not save.';fillPrefsForm();
    HOLD_BOXES.forEach(([id])=>{if($(id))$(id).dataset.ready='';});renderVHold();}
}
async function toggleStar(code){
  PREFS.venues=starred(code)?PREFS.venues.filter(c=>c!==code):[...(PREFS.venues||[]),code];
  await savePrefs(true);fillPrefsForm();
  if($('v-venues').classList.contains('on')){renderVenueGrid();if(VENUE)renderVdTitle();}
}
document.addEventListener('click',e=>{const s=e.target.closest('[data-star]');if(!s)return;
  e.preventDefault();e.stopPropagation();toggleStar(s.dataset.star);},true);

// ---------------------------------------------------------------- ntfy setup
function makeTopic(){
  if($('topic').value.trim()&&!confirm('Replace your notification ID with a new one? Your existing watches stay on the old one.'))return;
  const a=new Uint8Array(6);crypto.getRandomValues(a);
  const rand=[...a].map(x=>'abcdefghjkmnpqrstuvwxyz23456789'[x%31]).join('');
  $('topic').value='seatwatch-'+rand;localStorage.setItem('t',$('topic').value);
  $('setup').style.display='block';showSubscribe();list();
}
function toggleSetup(){const s=$('setup');s.style.display=s.style.display==='none'?'block':'none';showSubscribe();}
function showSubscribe(){
  const t=$('topic').value.trim();
  if(!t){$('sub-links').textContent='create or enter one first';$('topic-qr').innerHTML='';return;}
  const web='https://ntfy.sh/'+encodeURIComponent(t);
  $('sub-links').innerHTML=`<a href="ntfy://ntfy.sh/${encodeURIComponent(t)}">Open in the ntfy app</a> ·
    <a href="${web}" target="_blank" rel="noopener">open in browser</a> · or scan with your phone:`;
  $('topic-qr').innerHTML='';
  if(window.QRCode)new QRCode($('topic-qr'),{text:web,width:140,height:140});
}
async function testAlert(){
  const t=$('topic').value.trim();if(!t){$('test-msg').textContent='Create or enter a notification ID first.';return;}
  $('test-msg').textContent='Sending…';
  const r=await (await fetch('/api/test-alert',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({topic:t})})).json();
  $('test-msg').textContent=r.ok?'Sent. Check your phone.':(r.error||'Could not send.');
  if(r.ok){lsSet('alert-tested','1');renderStart();}
}

// ---------------------------------------------------------------- front page
// first visit: name, alerts, a first watch; each ticks off as it's done
let HAS_WATCH=false;
function lsGet(k){try{return localStorage.getItem(k);}catch(e){return null;}}
function lsSet(k,v){try{localStorage.setItem(k,v);}catch(e){}}
function renderStart(){
  const box=$('start');if(!box)return;
  const topic=!!$('topic').value.trim(), tracked=HAS_WATCH||lsGet('tracked')==='1';
  const steps=[
    {done:!!ME,t:'Add your name',d:'Your watches and requests are kept under it, so only you see them.',b:'Add my name',go:'askName()'},
    {done:topic&&(lsGet('alert-tested')==='1'||tracked),t:'Get alerts on your phone',
     d:topic?'Subscribe to your ID in the free ntfy app, then send yourself a test alert.'
       :'Make a private notification ID and subscribe to it in the free ntfy app.',
     b:topic?'Send a test alert':'Set up alerts',go:'startAlerts()'},
    {done:tracked,t:'Track your first show',d:'Pick a movie or a cinema, then a show, a screen or a whole date.',b:'Pick a movie',go:"location.hash='#movies'"}];
  const n=steps.filter(x=>x.done).length;
  if(lsGet('start-hide')==='1'||n===steps.length){box.hidden=true;return;}
  box.hidden=false;
  box.innerHTML=`<div class="start-head"><b>Get started</b><span>${n} of 3 done</span>
      <button class="link" onclick="lsSet('start-hide','1');renderStart()">Hide</button></div>
    <ol class="start-steps">${steps.map((x,i)=>`<li class="${x.done?'done':''}"><span class="sn">${x.done?'&#10003;':i+1}</span>
      <div><b>${x.t}</b><p>${x.d}</p></div>${x.done?'':`<button class="g" onclick="${x.go}">${x.b}</button>`}</li>`).join('')}</ol>`;
}
function startAlerts(){
  if(!$('topic').value.trim())makeTopic();
  else{$('setup').style.display='block';showSubscribe();testAlert();}
  $('topic').scrollIntoView({behavior:'smooth',block:'center'});
}

// one search box: movies and cinemas together, typed in any order ("amb gachi")
let FIND=[], FIND_AT=-1;
const fold=x=>String(x||'').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/[^a-z0-9]+/g,' ').trim();
function findMatches(q){
  q=fold(q);if(!q)return [];
  const words=q.split(' ');
  const score=text=>{const t=fold(text);if(!words.every(w=>t.includes(w)))return -1;
    return t.startsWith(q)?3:(' '+t).includes(' '+words[0])?2:1;};
  const out=[],seen=new Set();
  MOVIES.forEach(m=>{if(seen.has(m.title))return;const sc=score(m.title+' '+langs(m).join(' '));if(sc<0)return;seen.add(m.title);
    out.push({kind:'m',code:m.code,soon:!!m.upcoming,name:m.title,s:sc+(m.upcoming?0:.5),
      sub:(m.upcoming?'Coming soon':'Now showing')+(langs(m).length?' · '+langs(m).join(', '):'')});});
  VENUES.forEach(v=>{const sc=score(v.name+' '+vArea(v));if(sc<0)return;
    out.push({kind:'v',code:v.code,name:v.name,s:sc+(starred(v.code)?.5:0),sub:'Cinema'+(vArea(v)?' · '+vArea(v):'')});});
  return out.sort((a,b)=>b.s-a.s||a.name.localeCompare(b.name)).slice(0,8);
}
function renderFind(){
  const q=$('find').value.trim(),box=$('find-list');
  FIND=findMatches(q);FIND_AT=FIND.length?Math.min(Math.max(FIND_AT,0),FIND.length-1):-1;
  if(!q){box.hidden=true;$('find').setAttribute('aria-expanded','false');return;}
  box.innerHTML=FIND.length?FIND.map((x,i)=>`<button type="button" role="option" id="fi-${i}" class="find-item${i===FIND_AT?' on':''}"
      aria-selected="${i===FIND_AT}" data-find="${i}"><span class="fi-icon ${x.kind}">${x.kind==='m'?'Movie':'Cinema'}</span>
      <span><b>${safe(x.name)}</b><small>${safe(x.sub)}</small></span></button>`).join('')
    :`<div class="find-none">Nothing matches "${safe(q)}". New movie? <a href="#movies">Paste its BookMyShow link</a> to add it.</div>`;
  box.hidden=false;$('find').setAttribute('aria-expanded','true');
  $('find').setAttribute('aria-activedescendant',FIND_AT>=0?'fi-'+FIND_AT:'');
}
function goFind(x){
  $('find').value='';FIND_AT=-1;renderFind();$('find').blur();
  if(x.kind==='v'){const h='#venues/'+encodeURIComponent(x.code);if(location.hash===h)route();else location.hash=h;return;}
  location.hash='#movies';
  setTimeout(()=>{const tab=document.querySelector(`[data-mtab="${x.soon?'soon':'now'}"]`);
    if(tab&&!tab.classList.contains('on'))tab.click();chooseMovie(x.code);},50);
}
$('find').addEventListener('input',()=>{FIND_AT=0;renderFind();});
$('find').addEventListener('focus',renderFind);
$('find').addEventListener('keydown',e=>{
  if(e.key==='ArrowDown'||e.key==='ArrowUp'){if(!FIND.length)return;e.preventDefault();
    FIND_AT=(FIND_AT+(e.key==='ArrowDown'?1:-1)+FIND.length)%FIND.length;renderFind();
    $('fi-'+FIND_AT)?.scrollIntoView({block:'nearest'});}
  else if(e.key==='Enter'){if(FIND[FIND_AT]){e.preventDefault();goFind(FIND[FIND_AT]);}}
  else if(e.key==='Escape'){$('find').value='';renderFind();}
});
$('find-list').addEventListener('click',e=>{const b=e.target.closest('[data-find]');if(b)goFind(FIND[+b.dataset.find]);});
document.addEventListener('click',e=>{if(!e.target.closest('.find-box'))$('find-list').hidden=true;});

// posters: from TMDB/Wikipedia when sure, else a title card (also if the image fails to load)
function posterHtml(m){
  return m.poster?`<img src="${safe(m.poster)}" alt="${safe(m.title)} poster" loading="lazy" referrerpolicy="no-referrer"
      onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'poster-fallback',textContent:${safe(JSON.stringify(m.title))}}))">`
    :`<span class="poster-fallback">${safe(m.title)}</span>`;
}

// "Popular now": what people here track most (counts only), topped up with the widest releases
async function loadPopular(){
  let d;try{d=await (await fetch('/api/popular')).json();}catch(e){return;}
  const ms=d.movies||[],vs=d.venues||[];
  if(!ms.length&&!vs.length){$('popular').hidden=true;return;}
  $('popular').hidden=false;
  $('popular').innerHTML=`<div class="opened-head"><h2>Popular <span>now</span></h2><span class="hint">most tracked here, then the widest releases</span></div>
    <div class="pop-row">${ms.map(m=>`<button type="button" class="pop-film" data-pop-movie="${safe(m.code)}" data-soon="${m.upcoming?1:''}">
      ${posterHtml(m)}<b>${safe(m.title)}</b>${m.watching?`<small class="hot">${m.watching} tracking</small>`
        :m.cinemas?`<small>at ${m.cinemas} cinema${m.cinemas>1?'s':''}</small>`:''}</button>`).join('')}</div>
    ${vs.length?`<div class="pop-venues">${vs.map(v=>`<a href="#venues/${encodeURIComponent(v.code)}">${safe(v.name)}<small>${v.watching} tracking</small></a>`).join('')}</div>`:''}`;
}
$('soon-cal').addEventListener('click',e=>{const b=e.target.closest('[data-movie]');if(b)chooseMovie(b.dataset.movie);});
document.addEventListener('click',e=>{const b=e.target.closest('[data-pop-movie]');
  if(b)goFind({kind:'m',code:b.dataset.popMovie,soon:!!b.dataset.soon});});

// "Your watches" bar: what needs you, always in reach
let MW_N=0;
function renderBar(){
  const bar=$('mybar');if(!bar)return;
  const subs=Object.values(WATCHES),n=subs.length+MW_N;
  if(!ME||!n){bar.hidden=true;document.body.classList.remove('has-bar','bar-urgent');$('tb-n').hidden=true;return;}
  const ph=subs.map(holdPhase),count=k=>ph.filter(x=>k.includes(x)).length;
  const pay=count(['held','payment']),waiting=count(['approved','triggered']),asked=count(['pending']);
  const seats=subs.filter(s=>s.seat_filter).length;
  const parts=[`${n} watch${n>1?'es':''}`];
  if(waiting)parts.push(`${waiting} auto-hold${waiting>1?'s':''} waiting`);
  if(asked)parts.push(`${asked} awaiting approval`);
  if(seats)parts.push(`${seats} seat alert${seats>1?'s':''}`);
  bar.innerHTML=(pay?`<span class="alert">Seats held: pay now</span>`:'')+
    parts.map(x=>`<span>${safe(x)}</span>`).join('<span class="sep">·</span>')+`<span class="go">View &#8250;</span>`;
  bar.setAttribute('aria-label','Your watches: '+parts.join(', ')+(pay?'. Seats held, pay now':''));
  bar.hidden=false;document.body.classList.add('has-bar');
  bar.classList.toggle('urgent',!!pay);document.body.classList.toggle('bar-urgent',!!pay);
  const tb=$('tb-n');tb.hidden=false;tb.textContent=n;tb.classList.toggle('alert',!!pay);
}
// hide the bar while the watch list itself is on screen
let BAR_TICK=0;
function barSeen(){BAR_TICK=0;const r=$('list').getBoundingClientRect();
  document.body.classList.toggle('list-seen',r.top<innerHeight*.7&&r.bottom>innerHeight*.3);}
addEventListener('scroll',()=>{if(!BAR_TICK)BAR_TICK=requestAnimationFrame(barSeen);},{passive:true});
addEventListener('resize',barSeen);

// "Just opened": from the checks already running, so no extra BookMyShow requests
function dayLabel(dc){
  if(!dc)return '';const t=new Date(),tm=new Date();tm.setDate(t.getDate()+1);
  if(dc===code(t))return 'Today';if(dc===code(tm))return 'Tomorrow';
  return new Date(+dc.slice(0,4),+dc.slice(4,6)-1,+dc.slice(6,8)).toLocaleDateString('en-IN',{weekday:'short',day:'numeric',month:'short'});
}
function agoLabel(iso){
  const s=(Date.now()-new Date(iso).getTime())/1000;
  return s<90?'just now':s<3600?Math.round(s/60)+' min ago':s<86400?Math.round(s/3600)+' h ago':'yesterday';
}
async function loadOpenings(){
  let d;try{d=await (await fetch('/api/openings')).json();}catch(e){return;}
  const list=d.openings||[];
  $('opened').innerHTML=list.length?list.map(o=>{
    const link=o.venue?'#venues/'+encodeURIComponent(o.venue)+'/'+o.date:'#movies';
    const when=[o.show_time,dayLabel(o.date)].filter(Boolean).join(' · ');
    return `<a class="op ${safe(o.kind)}" href="${link}" ${!o.venue&&o.movie_code?`data-recent-movie="${safe(o.movie_code)}"`:''}>
      <span class="op-what">${safe(o.what)}</span><b title="${safe(o.movie)}">${safe(o.movie||o.venue_name)}</b>
      <span>${safe(when)}</span>${o.movie?`<small title="${safe(o.venue_name)}">${safe(o.venue_name)}</small>`:''}
      <span class="op-ago">${agoLabel(o.at)}</span></a>`;}).join('')
    :`<div class="op-empty">Nothing new in the last two days. When seats or shows open at a cinema someone here
       tracks, it shows up here first.</div>`;
}
setInterval(()=>{if(!document.hidden&&$('v-home').classList.contains('on'))loadOpenings();},60000);

// ---------------------------------------------------------------- views
const SUBS={home:'Get told the moment seats open at the cinemas you care about.',
  movies:'Choose a movie and date. See every listed cinema and showtime.',
  venues:'Choose a cinema. See everything it is showing now and on future dates.'};
let ROUTED=false;
function route(){
  if(document.body.classList.contains('picking')||document.getElementById('sm-p'))closePicker();
  const [v,arg,day]=(location.hash||'#home').slice(1).split('/');
  const view=SUBS[v]?v:'home';
  document.querySelectorAll('.view').forEach(s=>s.classList.toggle('on',s.id==='v-'+view));
  $('sub').textContent=SUBS[view];
  if(view==='venues'){ if(arg)openVenue(decodeURIComponent(arg),day); else showVenueList(); }
  document.body.classList.toggle('on-home',view==='home');
  const TT={home:['Seat Watch tools','Find your <span>show.</span>'],movies:['Movies','Pick a <span>movie.</span>'],
    venues:['Cinemas','Pick a <span>cinema.</span>']}[view];
  $('t-kicker').textContent=TT[0];const tt=$('t-title');
  if(tt.innerHTML!==TT[1]){tt.innerHTML=TT[1];tt.classList.remove('anim');void tt.offsetWidth;tt.classList.add('anim');}
  document.querySelectorAll('[data-nav]').forEach(a=>a.classList.toggle('on',a.dataset.nav===view));
  document.querySelectorAll('#tabbar [data-tab]').forEach(a=>a.classList.toggle('on',a.dataset.tab===view));
  if(view==='home'){loadOpenings();loadPopular();}
  // the landing story sits above the tools on the home view: a first visit starts at
  // its top; going "home" from inside the tools lands on the tools, not the story
  document.body.classList.toggle('off-home',view!=='home');
  const first=!ROUTED;ROUTED=true;
  if(view!=='home')window.scrollTo({top:0});
  else if(!first||location.hash==='#home')$('tools').scrollIntoView();
}
window.addEventListener('hashchange',route);

// ---------------------------------------------------------------- cinemas
let VENUE=null, VDATES=[], VDATE=null, vSeq=0;
const PALETTE=['#d62d45','#3478f6','#16804a','#8255d9','#c26a00','#0f766e','#b4236b','#475467'];
function venueBits(v){
  const [chain,area]=String(v.name).split(/:\s*/);
  // the brand is what tells cinemas apart: "ALLU Cinemas" -> ALLU, "Aparna Cinemas" -> AP,
  // "Sree Ramulu 70mm" -> SR; filler words (Cinemas, Theatre, 35MM...) are skipped
  const FILLER=/^(cinemas?|theat(re|er)s?|multiplex|screens?|movies?|the|\d+mm|4k|a\/c|ac)$/i;
  const words=chain.replace(/[^A-Za-z0-9 ]/g,' ').split(/\s+/).filter(w=>w&&!FILLER.test(w));
  const first=words[0]||chain;
  const initials=(first.length<=4?first:words.length>1?words[0][0]+words[1][0]:first.slice(0,2)).toUpperCase();
  let h=0;for(const ch of chain)h=(h*31+ch.charCodeAt(0))>>>0;
  return {chain,area:area||'',initials,color:PALETTE[h%PALETTE.length]};
}
function fillAreaSelect(){
  const areas={};VENUES.forEach(v=>{const a=vArea(v);if(a)areas[a]=(areas[a]||0)+1;});
  const cur=$('vgrid-area').value;
  $('vgrid-area').innerHTML='<option value="">All areas</option>'+Object.keys(areas).sort().map(a=>
    `<option value="${safe(a)}">${safe(a)} (${areas[a]})</option>`).join('');
  $('vgrid-area').value=cur;
  $('area-list').innerHTML=Object.keys(areas).sort().map(a=>`<option value="${safe(a)}">`).join('');
}
function renderVenueGrid(){
  const q=($('vgrid-search').value||'').toLowerCase();
  const area=$('vgrid-area').value;
  // starred cinemas first
  const list=VENUES.filter(v=>v.name.toLowerCase().includes(q)&&(!area||vArea(v)===area))
    .sort((a,b)=>(starred(b.code)?1:0)-(starred(a.code)?1:0));
  $('venue-grid').innerHTML=list.length?list.map(v=>{const b=venueBits(v);const on=starred(v.code);return `
    <button class="venue-tile" data-vcode="${safe(v.code)}">
      <span class="star ${on?'on':''}" data-star="${safe(v.code)}" title="${on?'Unstar':'Star'} this cinema">${on?'&#9733;':'&#9734;'}</span>
      <span class="vt-icon" style="background:${b.color}">${safe(b.initials)}</span>
      ${safe(b.chain)}${b.area?`<small>${safe(b.area)}</small>`:''}</button>`}).join('')
    :`<div class="empty">${VENUES.length?'No cinema matches that search.':'Loading cinemas…'}</div>`;
}
function showVenueList(){
  VENUE=null;$('venue-detail').style.display='none';$('venue-list').style.display='block';renderVenueGrid();
}
function closeVenue(){location.hash='#venues';}
function renderVdTitle(){
  const on=starred(VENUE.code);
  $('vd-title').innerHTML=`${safe(VENUE.name)} <span class="star ${on?'on':''}" data-star="${safe(VENUE.code)}"
    title="${on?'Unstar':'Star'} this cinema">${on?'&#9733;':'&#9734;'}</span>`;
}
async function openVenue(vcode,want){
  const v=VENUES.find(x=>x.code===vcode);
  if(!v){showVenueList();return;}
  remember('v',v.code,v.name);
  VENUE=v;BYSCREEN=false;const seq=++vSeq;
  $('venue-list').style.display='none';$('venue-detail').style.display='block';
  renderVdTitle();$('vd-shows').innerHTML='';$('vd-msg').textContent='Finding the dates it lists…';
  VDATES=[];VDATE=code(new Date());showStrip($('vd-dates'),VDATES,VDATE);
  try{const r=await (await fetch('/api/venue-dates?venue='+encodeURIComponent(vcode))).json();
      if(seq!==vSeq)return;VDATES=r.dates||[];}catch(e){VDATES=[];}
  const first=(want&&VDATES.find(d=>d.code===want))||VDATES.find(d=>d.open)||VDATES[0];if(first)VDATE=first.code;
  showStrip($('vd-dates'),VDATES,VDATE);loadVenueShows();
}
function slideVDates(dir){$('vd-dates').scrollBy({left:dir*Math.max(160,$('vd-dates').clientWidth*0.8)});}
let VSHOWS=[], BYSCREEN=false, VKNOWN={};   // VKNOWN: every screen seen at this cinema
const scrKey=x=>x.screen_name||x.attrs||x.screen||'Standard';
const scrLabel=x=>[x.screen_name,x.attrs].filter(Boolean).join(' · ')||x.screen||'Standard';
async function loadVenueShows(){
  if(!VENUE||!VDATE)return;const seq=++vSeq;
  $('vd-msg').textContent='Loading shows…';$('vd-shows').innerHTML='';
  $('vd-all').textContent='Track the whole cinema on '+pretty(VDATE);
  try{
    const r=await (await fetch(`/api/shows?venue=${encodeURIComponent(VENUE.code)}&date=${VDATE}`)).json();
    if(seq!==vSeq)return;
    if(r.error){$('vd-msg').textContent=r.error;return;}
    VSHOWS=r.shows||[];
    const known=VKNOWN[VENUE.code]=VKNOWN[VENUE.code]||{};
    VSHOWS.forEach(x=>{known[scrKey(x)]=x.attrs||'';});
    renderVenueShows();
  }catch(e){if(seq===vSeq)$('vd-msg').textContent='Could not load shows. Try another date.';}
}
function renderVenueShows(){
  if(document.body.classList.contains('picking')||document.getElementById('sm-p'))closePicker();
  const known=VKNOWN[VENUE.code]||{};
  const nScreens=Object.keys(known).length;
  const btn=$('vd-screens-btn');
  btn.style.display=nScreens?'inline-block':'none';
  btn.classList.toggle('on',BYSCREEN);
  btn.textContent=BYSCREEN?'Back to movies':`Available screens (${nScreens})`;
  if(BYSCREEN)return renderScreens(known);
  if(!VSHOWS.length){$('vd-msg').textContent=`No shows listed for ${pretty(VDATE)} yet. Track the whole cinema`+
    (nScreens?', or open Available screens to track one screen,':'')+' to be told when they appear.';return;}
  const groups={};VSHOWS.forEach(x=>(groups[x.movie||'Show']=groups[x.movie||'Show']||[]).push(x));
  $('vd-sort').innerHTML=tsortSelect();
  $('vd-msg').textContent=`${Object.keys(groups).length} movie(s), ${VSHOWS.length} show(s) on ${pretty(VDATE)}. Tap a time to see its seats and track it.`;
  $('vd-shows').innerHTML=Object.entries(groups).map(([movie,list])=>`<article class="mv-group">
    <div class="mv-head"><h3>${safe(movie)}</h3><button class="g" data-vmovie="${safe(movie)}">Track this movie here</button></div>
    <div class="time-grid">${byTime(list).map(x=>pill(x,scrLabel(x))).join('')}</div></article>`).join('');
}
// "08:00 AM" -> minutes, so each row reads earliest to latest
const mins=t=>{const m=/(\d{1,2}):(\d{2})\s*([AP]M)/i.exec(t||'');if(!m)return 9999;
  return (+m[1]%12+(m[3].toUpperCase()==='PM'?12:0))*60+ +m[2];};
// showtimes in the order the visitor chose: time, cheapest first, or seats first
let TSORT='time';
const minPrice=x=>{const p=(x.cats||[]).filter(c=>c.state!=='sold out').map(c=>c.price).filter(Boolean);return p.length?Math.min(...p):1e9;};
const availRank=x=>{const c=x.cats||[];
  if(!c.length)return !x.open?2:/fast/i.test(x.status||'')?1:0;
  return c.some(k=>k.state==='available')?0:c.some(k=>k.state==='filling fast')?1:2;};
const byTime=list=>[...list].sort((a,b)=>TSORT==='price'?minPrice(a)-minPrice(b)||mins(a.time)-mins(b.time)
  :TSORT==='avail'?availRank(a)-availRank(b)||mins(a.time)-mins(b.time):mins(a.time)-mins(b.time));
function setTSort(v){TSORT=v;document.querySelectorAll('.tsort').forEach(el=>el.value=v);
  if(VENUE&&$('venue-detail').style.display!=='none')renderVenueShows();if(MVENUES.length)renderMovieVenues();}
const tsortSelect=()=>`<label class="inline">Sort times <select class="tsort" onchange="setTSort(this.value)">
  <option value="time" ${TSORT==='time'?'selected':''}>by time</option><option value="avail" ${TSORT==='avail'?'selected':''}>seats first</option>
  <option value="price" ${TSORT==='price'?'selected':''}>cheapest first</option></select></label>`;
// a showtime button: time, screen/movie, price range and how full it is; the
// tooltip lists every category ("GOLD Rs 390 filling fast")
function pill(x,sub){
  const cats=x.cats||[];
  const open=cats.filter(c=>c.state!=='sold out');
  const prices=(open.length?open:cats).map(c=>c.price).filter(Boolean);
  const range=prices.length?(Math.min(...prices)===Math.max(...prices)?`₹${Math.min(...prices)}`:`₹${Math.min(...prices)}–${Math.max(...prices)}`):'';
  const state=!cats.length?(x.open?'Available':'Sold / closed')
    :open.some(c=>c.state==='available')?'Available':open.length?'Filling fast':'Sold out';
  const cls=state==='Sold out'||state==='Sold / closed'?'sold':state==='Filling fast'?'fast':'';
  const tip=cats.map(c=>`${c.name}${c.price?' ₹'+c.price:''}: ${c.state}`).join('\n');
  return `<button class="time-pill ${cls}" data-vsession="${safe(x.session||'')}" data-vmovie-of="${safe(x.movie||'')}"
    title="${safe(tip)}" aria-label="Track ${safe(x.time)} ${safe(x.movie)}, ${safe(state)}"><b>${safe(x.time)}</b>
    <small>${safe(sub)}</small><small>${range?safe(range)+' · ':''}${safe(state)}</small></button>`;
}
function renderScreens(known){
  // every screen this cinema has (from the dates loaded so far), each with what's
  // playing on it on the selected date; screens with nothing that day still show
  const names=Object.keys(known).sort((a,b)=>a.localeCompare(b,undefined,{numeric:true}));
  const playing={};VSHOWS.forEach(x=>(playing[scrKey(x)]=playing[scrKey(x)]||[]).push(x));
  $('vd-msg').textContent=`${names.length} screen(s) at this cinema. What's playing on each on ${pretty(VDATE)}:`;
  // same full-width rows as the movie view: header across the top, times side by side
  $('vd-shows').innerHTML=names.map(n=>{
    const list=byTime(playing[n]||[]);
    return `<article class="mv-group">
      <div class="mv-head"><h3>${safe(n)}${known[n]?` <span class="fmt">${safe(known[n])}</span>`:''}</h3>
        <button class="g" data-vscreen="${safe(n)}">Track ${safe(n)} on ${safe(pretty(VDATE))}</button></div>
      ${list.length?`<div class="time-grid">${list.map(x=>pill(x,x.movie||'Show')).join('')}</div>`
                   :`<p class="hint">Nothing listed on this screen for this date yet.</p>`}</article>`;
  }).join('');
}
$('vd-screens-btn').addEventListener('click',()=>{BYSCREEN=!BYSCREEN;renderVenueShows();});
function pretty(dc){const d=new Date(+dc.slice(0,4),+dc.slice(4,6)-1,+dc.slice(6));
  return d.toLocaleDateString(undefined,{weekday:'short',day:'numeric',month:'short'});}
// optional auto-hold that rides along with whatever gets tracked: the same box on
// the cinema page (prefix vh) and the movie page (prefix mh)
const HOLD_BOXES=[['vd-hold','vh'],['mv-hold','mh']];
function renderVHold(){
  for(const [id,p] of HOLD_BOXES){
    const box=$(id);if(!box)continue;
    if(LIVE===false){
      box.dataset.ready='';
      box.innerHTML=`<b>Auto-hold is off</b> <span class="hint">so seats can't be reserved for you automatically right now.</span>
        <button class="g" onclick="askOn()">Ask the owner to turn auto-hold on</button>`;
      continue;
    }
    if(box.dataset.ready)continue;          // keep what the visitor already typed
    box.dataset.ready='1';
    box.innerHTML=`<label class="check"><input type="checkbox" id="${p}-on" onchange="$('${p}-fields').style.display=this.checked?'grid':'none'">
        <b>Also request auto-hold</b> <span class="hint">for whatever you track below. The owner approves it, then
        seats are held the moment a matching show opens.</span></label>
      <div class="fields" id="${p}-fields" style="display:none">
        ${holdFields(p)}
      </div>`;
  }
}
async function addWatch(fields,msgEl,p){
  const t=$('topic').value.trim();if(!t){alert('Enter your notification ID first (top of the page).');$('topic').focus();return;}
  localStorage.setItem('t',t);
  const r=await (await fetch('/api/subs',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(Object.assign({topic:t},fields))})).json();
  let msg=r.error||'Watching. Check your phone.';
  // auto-hold ticked: attach the request to the new (or existing) watch
  if(p&&$(p+'-on')?.checked&&r.id){
    const h=await (await fetch('/api/subs/hold',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:r.id,topic:t,qty:$(p+'-qty').value,max_total:$(p+'-max').value,
        expire_min:$(p+'-exp')?.value,retries:$(p+'-retry')?.value,
        categories:$(p+'-cats').value,rows:$(p+'-rows').value})})).json();
    msg=(r.error?'Already watching that. ':'Watching. ')+
      (h.error?'Auto-hold not sent: '+h.error:'Auto-hold requested, waiting for the owner to approve it.');
  }
  msgEl.textContent=msg;list();
}
$('vd-all').addEventListener('click',()=>addWatch({venue:VENUE.code,date:VDATE},$('vd-msg'),'vh'));
$('venue-grid').addEventListener('click',e=>{const t=e.target.closest('[data-vcode]');if(t)location.hash='#venues/'+encodeURIComponent(t.dataset.vcode);});
$('vd-dates').addEventListener('click',e=>{const d=e.target.closest('[data-date]');if(!d)return;
  VDATE=d.dataset.date;showStrip($('vd-dates'),VDATES,VDATE);loadVenueShows();});
$('vd-shows').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;
  if(b.dataset.vsession)openPicker(b,{venue:VENUE.code,date:VDATE,session:b.dataset.vsession,movie:b.dataset.vmovieOf,
    time:b.querySelector('b')?.textContent,hold:'vh',msg:'vd-msg'});
  else if(b.dataset.vmovie)addWatch({venue:VENUE.code,date:VDATE,movie:b.dataset.vmovie},$('vd-msg'),'vh');
  else if(b.dataset.vscreen)addWatch({venue:VENUE.code,date:VDATE,screen:b.dataset.vscreen},$('vd-msg'),'vh');});

async function boot(){
  if(/[?&]invite=/.test(location.search)){const u=new URL(location.href);u.searchParams.delete('invite');history.replaceState(null,'',u);}
  try{setMe((await (await _fetch('/api/me')).json()).name||'');}catch(e){setMe('');}
  try{
    const [movies,venues,status]=await Promise.all([
      fetch('/api/movies').then(r=>r.json()),fetch('/api/venues').then(r=>r.json()),fetch('/api/status').then(r=>r.json())]);
    MOVIES=movies.movies||[];VENUES=venues.venues||[];
    await loadPrefs();
    // start the language filter on the first preferred language that's playing
    const lang=pref('languages',[]).find(l=>MOVIES.some(m=>langs(m).some(x=>x.toLowerCase()===l.toLowerCase())));
    if(lang)LANG=MOVIES.flatMap(langs).find(x=>x.toLowerCase()===lang.toLowerCase());
    fillAreaSelect();renderCats();renderMovies();filterVenues();route();
    $('stat').textContent=`${status.subs} watch(es) · ${status.pages} page(s) per check · up ${status.uptime}`;
    const t=localStorage.getItem('t');if(t){$('topic').value=t;list();}
    renderStart();
  }catch(e){$('movie-grid').innerHTML='<div class="empty">Could not load movies. Refresh to try again.</div>';}
}
async function addMovie(){
  const link=$('movie-link').value.trim();
  if(!link){$('movie-msg').textContent='Paste a BookMyShow movie link.';return;}
  $('movie-msg').textContent='Finding movie…';
  try{
    const r=await (await fetch('/api/movies/add',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url:link})})).json();
    if(r.error){$('movie-msg').textContent=r.error;return;}
    MOVIES=MOVIES.filter(m=>m.code!==r.movie.code);MOVIES.push(r.movie);renderCats();
    $('movie-msg').textContent='Movie added.';chooseMovie(r.movie.code);
  }catch(e){$('movie-msg').textContent='Could not add that movie. Try again.';}
}
const RECENT_KEY='recent-v1';
function recentList(){try{return JSON.parse(localStorage.getItem(RECENT_KEY)||'[]');}catch(e){return [];}}
function remember(kind,code,name){
  const list=[{kind,code,name},...recentList().filter(x=>!(x.kind===kind&&x.code===code))].slice(0,8);
  try{localStorage.setItem(RECENT_KEY,JSON.stringify(list));}catch(e){}
  renderRecent();
}
function renderRecent(){
  const list=recentList();
  $('recent').innerHTML=list.length?`<b>Recently viewed</b>`+list.map(x=>`<a class="fchip" href="${x.kind==='v'?'#venues/'+encodeURIComponent(x.code):'#movies'}"
    ${x.kind==='m'?`data-recent-movie="${safe(x.code)}"`:''}>${x.kind==='v'?'&#127970; ':'&#127916; '}${safe(x.name)}</a>`).join('')
    +` <a href="#" class="hint" onclick="try{localStorage.removeItem(RECENT_KEY)}catch(e){};renderRecent();return false">clear</a>`:'';
}
document.addEventListener('click',e=>{const m=e.target.closest('[data-recent-movie]');
  if(m)setTimeout(()=>{if(MOVIES.some(x=>x.code===m.dataset.recentMovie))chooseMovie(m.dataset.recentMovie);},50);});
async function chooseMovie(movieCode){
  MOVIE=MOVIES.find(m=>m.code===movieCode);if(!MOVIE)return;
  remember('m',MOVIE.code,MOVIE.title);
  const picked=MOVIE.code;
  MDATES=[];DATE=code(new Date());renderMovies();renderDates();
  $('movie-title').textContent=MOVIE.title+' showtimes';
  $('schedule').style.display='block';
  $('loadmsg').textContent='Finding the dates it plays…';$('venue-shows').innerHTML='';
  $('schedule').scrollIntoView({behavior:'smooth',block:'start'});
  try{
    const r=await (await fetch('/api/movie-dates?movie='+encodeURIComponent(picked))).json();
    if(MOVIE?.code!==picked)return;          // another movie was clicked meanwhile
    MDATES=r.dates||[];
  }catch(e){MDATES=[];}
  // open on the first day with shows (e.g. a movie that starts on the 27th)
  const first=MDATES.find(d=>d.open)||MDATES[0];
  if(first)DATE=first.code;
  MVENUES=[];renderMvFilters();
  renderDates();loadMovieShows();
}
async function loadMovieShows(){
  if(!MOVIE||!DATE)return;
  const request=++loadSeq;
  $('loadmsg').textContent='Loading showtimes…';$('venue-shows').innerHTML='';
  try{
    const r=await (await fetch(`/api/movie-shows?movie=${encodeURIComponent(MOVIE.code)}&date=${DATE}`)).json();
    if(request!==loadSeq)return;
    if(r.error){$('loadmsg').textContent=r.error;return;}
    // starred cinemas first, then ones showing it in a favourite format
    const favFmt=pref('formats',[]);
    const hasFav=v=>v.shows.some(x=>favFmt.some(f=>(x.screen||'').toUpperCase().includes(f)));
    const venues=(r.venues||[]).map((v,i)=>[v,i]).sort((a,b)=>
      (starred(b[0].code)-starred(a[0].code))||(hasFav(b[0])-hasFav(a[0]))||(a[1]-b[1])).map(x=>x[0]);
    $('loadmsg').textContent=venues.length?`${venues.length} cinemas showing ${MOVIE.title}`+
      (pref('venues',[]).length||favFmt.length?' (your starred cinemas and formats first)':''):
      'No shows listed for this movie on this date yet. You can still track a cinema or screen below.';
    const screens=new Set();
    venues.forEach(v=>v.shows.forEach(show=>{if(show.screen)screens.add(show.screen)}));
    $('screen').innerHTML=screens.size?'<option value="">Choose a screen or format</option>'+[...screens].sort().map(x=>`<option>${safe(x)}</option>`).join('')
      :'<option value="">No screens listed for this date</option>';
    $('fmt-list').innerHTML=[...screens].map(x=>`<option value="${safe(x)}">`).join('');
    MVENUES=venues;renderMvFilters();renderMovieVenues();
  }catch(e){if(request===loadSeq)$('loadmsg').textContent='Could not load showtimes. Try another date.';}
}
// ---- area / format filters on the movie page (areas from "Brand: Area" names)
let MVENUES=[], MV_AREAS=new Set(), MV_FMTS=new Set(), MV_FILTER_MOVIE=null;
const vArea=v=>v.area||(String(v.name).split(':')[1]||'').trim();
const FMT_WORDS=['DOLBY','IMAX','4DX','ICE','ATMOS','LASER','BARCO','3D','2D','EPIQ','PXL','RECLINER'];
const showFmts=x=>FMT_WORDS.filter(f=>`${x.screen||''} ${x.format||''}`.toUpperCase().includes(f));
function renderMvFilters(){
  if(MV_FILTER_MOVIE!==MOVIE?.code){        // new movie: start from the saved preferences
    MV_FILTER_MOVIE=MOVIE?.code;
    MV_AREAS=new Set(pref('areas',[]).map(a=>a.toLowerCase()));
    MV_FMTS=new Set(pref('formats',[]).map(f=>f.toUpperCase()).filter(f=>FMT_WORDS.includes(f)));
  }
  const areas={},fmts={};
  MVENUES.forEach(v=>{const a=vArea(v);if(a)areas[a]=(areas[a]||0)+1;
    new Set(v.shows.flatMap(showFmts)).forEach(f=>fmts[f]=(fmts[f]||0)+1);});
  const chip=(kind,val,n,on)=>`<button class="fchip ${on?'on':''}" data-f${kind}="${safe(val)}">${safe(val)} <span>${n}</span></button>`;
  const aList=Object.entries(areas).sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0]));
  const fList=Object.entries(fmts).sort((a,b)=>b[1]-a[1]);
  $('mv-filters').innerHTML=(aList.length?`<div class="frow"><b>Area</b>${aList.map(([a,n])=>chip('area',a,n,MV_AREAS.has(a.toLowerCase()))).join('')}</div>`:'')+
    (fList.length?`<div class="frow"><b>Format</b>${fList.map(([f,n])=>chip('fmt',f,n,MV_FMTS.has(f))).join('')}</div>`:'')+
    `<div class="frow"><button class="fchip ${MV_ONLY?'on':''}" data-fonly="1">Only cinemas with seats</button>
      <label class="inline">Sort cinemas <select onchange="MV_SORT=this.value;renderMovieVenues()">
        <option value="star" ${MV_SORT==='star'?'selected':''}>starred first</option><option value="name" ${MV_SORT==='name'?'selected':''}>by name</option>
        <option value="early" ${MV_SORT==='early'?'selected':''}>earliest show</option><option value="formats" ${MV_SORT==='formats'?'selected':''}>most formats</option></select></label>
      ${tsortSelect()}</div>`;
  renderMwPanel();
}
let MV_ONLY=false,MV_SORT='star';
function mvMatch(v){
  if(MV_AREAS.size&&!MV_AREAS.has(vArea(v).toLowerCase()))return null;
  let shows=MV_FMTS.size?v.shows.filter(x=>showFmts(x).some(f=>MV_FMTS.has(f))):v.shows;
  if(MV_ONLY)shows=shows.filter(x=>x.open);
  return shows.length?{...v,shows:byTime(shows)}:null;
}
function mvSorted(list){
  const early=v=>Math.min(...v.shows.map(x=>mins(x.time)));
  const nf=v=>new Set(v.shows.map(x=>x.screen)).size;
  if(MV_SORT==='name')return [...list].sort((a,b)=>a.name.localeCompare(b.name));
  if(MV_SORT==='early')return [...list].sort((a,b)=>early(a)-early(b));
  if(MV_SORT==='formats')return [...list].sort((a,b)=>nf(b)-nf(a));
  return list;                                  // server order: starred and favourite formats first
}
function renderMovieVenues(){
  if(document.body.classList.contains('picking')||document.getElementById('sm-p'))closePicker();
  const venues=mvSorted(MVENUES.map(mvMatch).filter(Boolean));
  const filtered=MV_AREAS.size||MV_FMTS.size||MV_ONLY;
  if(MVENUES.length)$('loadmsg').textContent=`${venues.length} of ${MVENUES.length} cinemas showing ${MOVIE.title}`+
    (filtered?' match your filters':'')+(pref('venues',[]).length?' (starred cinemas first)':'');
  $('venue-shows').innerHTML=venues.map(v=>{
      const formats=[...new Set(v.shows.map(x=>x.screen).filter(Boolean))];
      const sections=[...new Set(v.shows.map(x=>x.format||'Showtimes'))];
      return `<article class="venue-card"><div class="venue-head"><h3>${safe(v.name)} <span class="star ${starred(v.code)?'on':''}"
        data-star="${safe(v.code)}" title="${starred(v.code)?'Unstar':'Star'} this cinema">${starred(v.code)?'&#9733;':'&#9734;'}</span></h3>
        <button class="g" data-watch-venue="${safe(v.code)}">Track this movie here</button></div>
        ${sections.map(format=>`<div class="format-section"><h4 class="format-title">${safe(format)}</h4>
        <div class="time-grid">${v.shows.filter(show=>(show.format||'Showtimes')===format).map(show=>`<button class="time-pill ${show.open?(show.status==='Fast filling'?'fast':''):'sold'}" data-show="${safe(show.session||'')}" data-venue="${safe(v.code)}"
          aria-label="Track ${safe(show.time)} at ${safe(v.name)}"><b>${safe(show.time)}</b><small>${safe(show.screen||'Standard')} · ${safe(show.status)}</small></button>`).join('')}</div></div>`).join('')}
        ${formats.length?`<div class="watch-options">${formats.map(f=>`<button class="g" data-watch-screen="${safe(f)}" data-venue="${safe(v.code)}">Track ${safe(f)}</button>`).join('')}</div>`:''}</article>`;
    }).join('')||(MVENUES.length?'<div class="empty">No cinema matches these filters. Clear a filter, or track the movie in these areas below.</div>':'');
}
$('mv-filters').addEventListener('click',e=>{
  if(e.target.closest('[data-fonly]')){MV_ONLY=!MV_ONLY;renderMvFilters();renderMovieVenues();return;}
  const c=e.target.closest('[data-farea],[data-ffmt]');if(!c)return;
  if(c.dataset.farea){const a=c.dataset.farea.toLowerCase();MV_AREAS.has(a)?MV_AREAS.delete(a):MV_AREAS.add(a);}
  else{const f=c.dataset.ffmt;MV_FMTS.has(f)?MV_FMTS.delete(f):MV_FMTS.add(f);}
  renderMvFilters();renderMovieVenues();
});
// ---- track this movie everywhere (in the chosen areas / formats)
function renderMwPanel(){
  if(!MOVIE)return;
  $('mw-title').textContent=`Track ${MOVIE.title}${MV_AREAS.size?' in your areas':' everywhere'}`;
  const bookable=MDATES.some(d=>d.open);
  const opts=[['','Any date (tell me whenever it opens / a new cinema appears)']];
  if(DATE)opts.push([DATE,'Only '+pretty(DATE)]);
  $('mw-date').innerHTML=opts.map(([v,t])=>`<option value="${v}">${safe(t)}</option>`).join('');
  $('mw-msg').textContent=bookable?'':'Bookings aren\'t open yet: you\'ll be told the moment they are.';
}
async function trackEverywhere(){
  const t=$('topic').value.trim();if(!t){alert('Enter your notification ID first (top of the page).');$('topic').focus();return;}
  localStorage.setItem('t',t);
  const areas=[...new Set(MVENUES.map(vArea).filter(a=>MV_AREAS.has(a.toLowerCase())))];
  pref('areas',[]).forEach(a=>{if(MV_AREAS.has(a.toLowerCase())&&!areas.some(x=>x.toLowerCase()===a.toLowerCase()))areas.push(a);});
  const r=await (await fetch('/api/mwatch',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({topic:t,movie:MOVIE.code,date:$('mw-date').value,areas,formats:[...MV_FMTS]})})).json();
  $('mw-msg').textContent=r.error||'Tracking. Check your phone.';list();
}
async function delMw(id){
  await fetch('/api/mwatch/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id,topic:$('topic').value.trim()})});
  list();
}
async function add(body,venue){
  if(!MOVIE||!DATE){alert('Choose a movie and date first.');return;}
  const t=$('topic').value.trim();if(!t){alert('Enter your notification ID first (top of the page).');return;}
  const selected=venue||$('venue').value;if(!selected){alert('Choose a cinema first.');return;}
  addWatch(Object.assign({venue:selected,date:DATE,movie:MOVIE.title},body),$('loadmsg'),'mh');
}
const watchVenue=venue=>add({},venue);
const watchScreen=(venue,screen)=>add({screen},venue);
const watchSelectedScreen=()=>{
  const screen=$('screen').value.trim();if(!screen){alert('Choose a screen first.');return;}
  watchScreen($('venue').value,screen);
};
$('movie-grid').addEventListener('click',e=>{
  const movie=e.target.closest('[data-movie]');if(movie)chooseMovie(movie.dataset.movie);
});
$('mtabs').addEventListener('click',e=>{
  const b=e.target.closest('[data-mtab]');if(!b)return;
  MTAB=b.dataset.mtab;document.querySelectorAll('[data-mtab]').forEach(x=>x.classList.toggle('on',x===b));
  $('mtab-hint').textContent=MTAB==='soon'?'Coming soon, by release date. One tap to be told the moment bookings open.'
    :'Now showing in Hyderabad. Select a movie to see its cinemas and times.';
  CAL_OFF=false;
  renderMovies();
});
$('cats').addEventListener('click',e=>{
  const c=e.target.closest('[data-cat],[data-lang]');if(!c)return;
  if(c.dataset.cat)CAT=c.dataset.cat;else LANG=c.dataset.lang;
  renderCats();renderMovies();
});
$('date-strip').addEventListener('click',e=>{
  const date=e.target.closest('[data-date]');if(!date)return;
  DATE=date.dataset.date;renderDates();loadMovieShows();
});
$('venue-shows').addEventListener('click',e=>{
  const button=e.target.closest('button');if(!button)return;
  if(button.dataset.show){
    if(!MOVIE||!DATE)return;
    openPicker(button,{venue:button.dataset.venue,date:DATE,session:button.dataset.show,movie:MOVIE.title,
      time:button.querySelector('b')?.textContent,hold:'mh',msg:'loadmsg'});
  }
  else if(button.dataset.watchScreen)watchScreen(button.dataset.venue,button.dataset.watchScreen);
  else if(button.dataset.watchVenue)watchVenue(button.dataset.watchVenue);
});

async function list(){
  const t=$('topic').value.trim(); if(!t||!ME) return;
  localStorage.setItem('t',t);
  const [d,mw]=await Promise.all([fetch('/api/subs?topic='+encodeURIComponent(t)).then(r=>r.json()),
    fetch('/api/mwatch?topic='+encodeURIComponent(t)).then(r=>r.json()).catch(()=>({watches:[]}))]);
  const movieWatches=(mw.watches||[]).map(w=>`
    <div class="stub"><div class="v">Movie · anywhere</div>
      <div class="d">${safe(w.title)}</div>
      <div class="f">${safe(w.what)}</div>
      ${liveLine(w.live)}
      ${historyLine({...w,id:'m'+w.id})}
      <div class="x"><button class="g" onclick="delMw(${w.id})">Stop</button></div>
    </div>`).join('');
  WATCHES={};d.subs.forEach(s=>WATCHES[s.id]=s);
  HAS_WATCH=d.subs.length+(mw.watches||[]).length>0;if(HAS_WATCH)lsSet('tracked','1');renderStart();
  MW_N=(mw.watches||[]).length;renderBar();
  MW_CODES=new Set((mw.watches||[]).map(w=>w.code));if(MTAB==='soon'&&UPC)renderCalendar();
  $('list').innerHTML = (d.subs.length||movieWatches) ? movieWatches+d.subs.map(s=>`
    <div class="stub"><div class="v">${safe(s.venue_name)} ${stateChip(s)}</div>
      <div class="d mono">${safe(s.date_pretty)}</div>
      <div class="f">${safe(s.what)}</div>
      ${liveLine(s.live)}
      ${historyLine(s)}
      ${timeline(s)}
      <div class="hold-line">${holdLine(s)}</div>
      ${priceLine(s)}
      ${s.one_show?`<div class="hold-line"><button class="g" onclick="seatMap(${s.id})">Seat map</button>
        ${s.seat_filter?`<span class="badge approved">Seat alert: ${safe(seatFilterText(s.seat_filter))}</span>`
          :'<span class="hint">see free seats, pick rows, get seat-level alerts</span>'}</div>`:''}
      <div class="hold-line"><button class="g" onclick="groupPanel(${s.id})">${s.group?'Group':'Book with friends'}</button>
        ${s.group?`<span class="hint">${safe(groupLine(s.group))}</span>`:'<span class="hint">friends join with a link; seats are held together</span>'}</div>
      ${!s.one_show&&s.shows.length?`<div class="hold-line"><button class="g" onclick="toggleWShows(${s.id})">Shows &amp; seat maps (${s.shows.length})</button>
        <span class="hint">tap a time to see its seats and track rows</span></div>`:''}
      <div id="ws-${s.id}" class="wshows">${OPEN_WS.has(s.id)?wShows(s):''}</div>
      <div id="gp-${s.id}"></div>
      <div id="sm-${s.id}"></div>
      <div id="hf-${s.id}"></div>
      <div class="x"><button class="g" onclick="del(${s.id})">Stop</button></div>
    </div>`).join('') : '<div class="empty">Nothing yet.</div>';
  Object.keys(SM).forEach(id=>{if(id==='p')return;if($('sm-'+id))renderSeatMap(id);else delete SM[id];});   // keep open seat maps open
  OPEN_GP.forEach(id=>{if($('gp-'+id))renderGroup(id);else OPEN_GP.delete(id);});
  loadGroups();
  tickCountdowns();tickAgo();tickHeld();openHoldFromAlert();
}
// what a watch is doing, in one label, and where its auto-hold has got to
function holdPhase(s){
  const st=s.hold_status||'',stage=s.hold_stage||'';
  if(!st)return 'watch';
  if(st==='held'&&(stage==='pay_ask'||stage==='awaiting_payment'))return 'payment';
  return st;
}
const CHIP={watch:['Watch only',''],pending:['Auto-hold requested','warn'],approved:['Auto-hold approved','ok'],
  triggered:['Holding seats…','ok'],held:['Seats held','ok'],payment:['Waiting for payment','warn'],booked:['Booked','ok'],
  found:['Seats found (test)',''],released:['Released',''],expired:['Request expired',''],failed:['Hold failed','bad'],noanswer:['No answer','bad'],rejected:['Declined','bad']};
function stateChip(s){const [t,c]=CHIP[holdPhase(s)]||[s.hold_status,''];return `<span class="chip-st ${c}">${safe(t)}</span>`;}
function timeline(s){
  const ph=holdPhase(s);if(ph==='watch')return '';
  const steps=['Requested','Approved','Watching','Held','Payment','Booked'];
  const at={pending:0,approved:2,triggered:3,held:3,payment:4,booked:5,found:3,released:4,failed:3,noanswer:2,rejected:1,expired:1}[ph]??0;
  const end={released:'Released',failed:'Failed',noanswer:'No answer',rejected:'Declined',expired:'Expired'}[ph];
  if(end)steps.splice(at,steps.length-at,end);
  return `<ol class="tl">${steps.map((x,i)=>`<li class="${i<at?'done':i===at?(end?'end':'now'):''}">${safe(x)}</li>`).join('')}</ol>`;
}
function priceLine(s){
  const p=s.price;
  if(p&&p.tickets){
    const each=p.qty?Math.round(p.tickets/p.qty):0;
    return `<div class="price">Tickets ₹${p.tickets}${each&&p.qty>1?` (${p.qty} × ₹${each})`:''}`+
      (p.fees!=null?` + fees ₹${p.fees} = <b>₹${p.payable}</b>`:' + fees')+(p.cap?` · your cap ₹${p.cap}`:'')+
      (s.held_at?` · <span class="held" data-since="${s.held_at}"></span>`:'')+`</div>`;
  }
  if(s.hold_status&&s.cap)return `<div class="price">Cap ₹${s.cap} including fees (fees are usually about 10–12% a ticket)</div>`;
  return '';
}
function tickHeld(){
  document.querySelectorAll('.held[data-since]').forEach(el=>{
    const m=Math.max(0,Math.floor(Date.now()/1000-+el.dataset.since));
    el.textContent=`seats held ${Math.floor(m/60)}:${String(m%60).padStart(2,'0')} ago`;
  });
}
setInterval(tickHeld,1000);
const HOLD_LABEL={pending:'Auto-hold: waiting for approval',approved:'Auto-hold: approved, holds when seats open',
  triggered:'Auto-hold: holding seats now…',held:'Auto-hold: seats held',found:'Auto-hold: seats found (test)',
  failed:'Auto-hold: failed',noanswer:'Auto-hold: no answer',rejected:'Auto-hold: declined',
  booked:'Booked',released:'Auto-hold: seats released',expired:'Auto-hold: request expired'};
let LIVE=null;
async function checkLive(){
  try{
    const d=await (await fetch('/api/tracker')).json();
    const changed=LIVE!==d.live; LIVE=d.live;
    $('health').innerHTML=(d.parts||[]).map(p=>`<span class="hp ${p.ok?'ok':''}" title="${safe(p.name)}: ${safe(p.text)}">
      <span class="dot ${p.ok?'on':''}"></span>${safe(p.name)} <small>${safe(p.text)}</small></span>`).join('');
    $('armed').hidden=!d.armed;
    $('live').innerHTML=d.live
      ? `<span><b>Auto-hold is available</b>: request it on any watch below.`+
        (d.test_mode?' <span class="hint">(test mode: seats are found but not reserved)</span>':'')+'</span>'
      : `<span><b>Auto-hold is off</b>: your watches still send alerts, but seats can't be reserved for you automatically.</span>
         <button class="g" onclick="askOn()">Ask the owner to turn it on</button> <span id="askmsg" class="hint"></span>`;
    if(changed){list();HOLD_BOXES.forEach(([id])=>{if($(id))$(id).dataset.ready='';});renderVHold();}
  }catch(e){}
}
async function askOn(){
  const r=await (await fetch('/api/tracker/ask',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({topic:$('topic').value.trim()})})).json();
  $('askmsg').textContent=r.message||r.error||'';
}
function holdLine(s){
  const st=s.hold_status||'';
  if(!st) return LIVE?`<button class="g" onclick="holdForm(${s.id})">Request auto-hold</button>`
                     :'<span class="hint">Auto-hold offline</span>';
  const label=st==='approved'&&LIVE===false?'Auto-hold: approved, waiting for auto-hold to come online':HOLD_LABEL[st]||st;
  const again=['failed','noanswer','found','released','expired'].includes(st)&&LIVE;
  return `<span class="badge ${st}">${safe(label)}</span>`+
    (s.hold_result?` <span class="hint">${safe(s.hold_result)}</span>`:(s.hold_text?` <span class="hint">${safe(s.hold_text)}</span>`:''))+
    (st==='pending'||st==='rejected'?` <button class="g" onclick="cancelHold(${s.id})">${st==='pending'?'Cancel':'Clear'}</button>`:'')+
    (again?` <button class="g" onclick="holdForm(${s.id})">Request again</button>`:'')+
    (s.hold_deadline?` <span class="cd" data-until="${s.hold_deadline}"></span>`:'');
}
// alert history under each watch: "History (5)" opens a newest-first timeline
const OPEN_HIST=new Set();
const HIST_ICON={NEW_SHOW:'✚',SHOW_OPENED:'●',CATEGORY_OPENED:'●',NEW_CATEGORY:'✚',CATEGORY_CHANGED:'↕',
  CATEGORY_CLOSED:'○',BEST_SEATS:'★',DATE_OPEN:'📅',HOLD:'🎟',SEATS:'💺'};
function historyLine(s){
  const h=s.history||[];
  if(!h.length)return '<div class="hist-btn hint">No alerts yet</div>';
  const open=OPEN_HIST.has(String(s.id));
  const when=a=>{const d=new Date(a.at);return d.toLocaleDateString(undefined,{day:'numeric',month:'short'})+' '+
    d.toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});};
  return `<button class="g hist-btn" onclick="toggleHist('${safe(String(s.id))}')">${open?'Hide':'History'} (${h.length})</button>`+
    (open?`<ol class="hist">${h.map(a=>`<li><span class="mono">${safe(when(a))}</span>
      <span class="hi">${HIST_ICON[a.kind]||'•'}</span> ${safe(a.detail)}</li>`).join('')}</ol>`:'');
}
function toggleHist(id){id=String(id);OPEN_HIST.has(id)?OPEN_HIST.delete(id):OPEN_HIST.add(id);list();}
// "● checked 8s ago · 04:10 PM: GOLD Rs 390 filling fast" under each watch
const LIVE_DOT={open:'on',filling:'fast',sold:'sold',none:'',closed:'',waiting:''};
function liveLine(l){
  if(!l)return'';
  return `<div class="wl"><span class="dot ${LIVE_DOT[l.state]||''}"></span>
    <span class="ago" data-at="${l.at||''}"></span>${safe(l.text)}</div>`;
}
function tickAgo(){
  document.querySelectorAll('.ago[data-at]').forEach(el=>{
    const at=+el.dataset.at;
    if(!at){el.textContent='';return;}
    const s=Math.max(0,Math.round(Date.now()/1000-at));
    el.textContent=(s<60?`checked ${s}s ago`:`checked ${Math.floor(s/60)} min ago`)+' · ';
  });
}
setInterval(tickAgo,1000);
// refresh the watch list's live lines while the page is open
setInterval(()=>{
  const formOpen=[...document.querySelectorAll('[id^="hf-"],[id^="sm-"],.grp input:focus')].some(b=>b.innerHTML||b.matches('input'));
  if(document.visibilityState==='visible'&&$('topic').value.trim()&&!formOpen)list();
},20000);
// live countdowns on badges ("3:12 left") while a payment step waits on the person
function tickCountdowns(){
  document.querySelectorAll('.cd[data-until]').forEach(el=>{
    const left=Math.round(+el.dataset.until-Date.now()/1000);
    el.textContent=left>0?`${Math.floor(left/60)}:${String(left%60).padStart(2,'0')} left`:"time's up";
    el.classList.toggle('late',left<=60);
  });
}
setInterval(tickCountdowns,1000);
// an alert's "Request auto-hold" button opens /tools?hold=<id>: open that watch's form
const OPEN_HOLD=new URLSearchParams(location.search).get('hold');
let openHoldDone=false;
function openHoldFromAlert(){
  if(!OPEN_HOLD||openHoldDone||LIVE===null)return;
  const box=$('hf-'+OPEN_HOLD);
  if(!box){$('list').insertAdjacentHTML('afterbegin',`<p class="hint">To request auto-hold from that alert,
    open it on the device where you made the watch, or enter the same name and notification ID here.</p>`);openHoldDone=true;return;}
  openHoldDone=true;
  if(!LIVE)return;
  if(!box.innerHTML)holdForm(+OPEN_HOLD);
  box.scrollIntoView({behavior:'smooth',block:'center'});
}
function holdForm(id){
  const box=$('hf-'+id); if(box.innerHTML){box.innerHTML='';return;}
  box.innerHTML=`<div class="hold-form">
    <p class="hint" style="margin-top:0">The owner approves each request. Once approved, seats are held
      the moment a matching show opens, then the owner completes the booking.</p>
    <div class="fields">${holdFields('w'+id,(WATCHES[id]||{}).cats)}</div>
    ${seatSync(id)}${groupSeats(id)}
    <button onclick="sendHold(${id})">Send request</button> <span id="hmsg-${id}" class="hint"></span></div>`;
}
// the four auto-hold fields, filled in from the visitor's saved preferences
function seatSync(id){
  const f=(WATCHES[id]||{}).seat_filter;
  if(!f)return '';
  setTimeout(()=>{const p='w'+id;if(!$(p+'-qty'))return;
    $(p+'-qty').value=String(f.together);$(p+'-cats').value=(f.categories||[]).join(', ');
    $(p+'-rows').value=(f.rows||[]).join(', ');markCatChips(p);});
  return `<p class="hint">Filled in from your seat alert (${safe(seatFilterText(f))}): the hold only takes those seats,
    and it fires the moment they're free, not just when the category opens.</p>`;
}
function groupSeats(id){
  const g=(WATCHES[id]||{}).group;
  if(!g||!g.members.length)return '';
  setTimeout(()=>{const q=$('w'+id+'-qty');if(q){q.innerHTML=`<option>${g.total}</option>`;q.disabled=true;}});
  return `<p class="hint">Group booking: ${g.total} seats side by side for ${safe(groupLine(g).split(' · ')[0].replace(/^\d+ seats: /,''))}.</p>`;
}
function toggleCatChip(p,name){
  const el=$(p+'-cats');const list=el.value.split(',').map(x=>x.trim()).filter(Boolean);
  const i=list.findIndex(x=>x.toUpperCase()===name.toUpperCase());
  i>=0?list.splice(i,1):list.push(name);el.value=list.join(', ');markCatChips(p);
}
function markCatChips(p){
  const have=new Set($(p+'-cats').value.split(',').map(x=>x.trim().toUpperCase()));
  document.querySelectorAll(`[data-catp="${p}"]`).forEach(b=>b.classList.toggle('on',have.has(b.dataset.cat.toUpperCase())));
}
function holdFields(p,cats){
  const chips=(cats||[]).length?`<div class="catchips">${cats.map(c=>`<button type="button" class="fchip" data-catp="${p}"
    data-cat="${safe(c)}" onclick="toggleCatChip('${p}',this.dataset.cat)">${safe(c)}</button>`).join('')}</div>`:'';
  setTimeout(()=>{if($(p+'-cats'))markCatChips(p);});
  return holdFieldsBase(p).replace('<!--catchips-->',chips);
}
function holdFieldsBase(p){
  return `<div><label>Seats together</label><select id="${p}-qty">${[1,2,3,4,5,6,7,8,9,10].map(n=>
      `<option ${n===pref('qty',2)?'selected':''}>${n}</option>`).join('')}</select></div>
    <div><label>Max total incl. fees (&#8377;)</label><input id="${p}-max" type="number" min="0" placeholder="no cap"
      value="${pref('max_total',0)||''}"></div>
    <details class="seatprefs"><summary>Seat preferences <span class="hint">optional</span></summary><div class="fields">
    <div><label>Categories</label><input id="${p}-cats" placeholder="any category"
      value="${safe(pref('categories',[]).join(', '))}" oninput="markCatChips('${p}')"><!--catchips--></div>
    <div><label>Rows</label><input id="${p}-rows" placeholder="any row, e.g. F, G"
      value="${safe(pref('rows',[]).join(', '))}"></div>
    <div><label>If not approved within</label><select id="${p}-exp"><option value="0">keep waiting</option>
      <option value="30">30 min</option><option value="60">1 hour</option><option value="180">3 hours</option>
      <option value="720">12 hours</option><option value="1440">1 day</option></select></div>
    <div><label>If the hold fails, try again</label><select id="${p}-retry"><option value="0">no</option>
      <option value="1">once</option><option value="2">twice</option><option value="3">3 times</option></select></div>
    </div></details>`;
}
// ---- seat map of a single-showtime watch: see free seats, tap row letters to
// pick rows, choose categories, then get seat-level alerts or use it for auto-hold
let WATCHES={};const SM={};
function seatFilterText(f){return [f.together+' together',(f.categories||[]).join(', '),
  (f.rows||[]).length?'rows '+f.rows.join(' '):''].filter(Boolean).join(' · ');}
async function seatMap(id,refresh){
  const box=$('sm-'+id);
  if(box.innerHTML&&!refresh){box.innerHTML='';delete SM[id];return;}
  box.innerHTML=`<div class="smap"><span class="hint">Loading the seat map from BookMyShow (a few seconds)…</span></div>`;
  const d=await getMap(`/api/seatmap?id=${id}&topic=${encodeURIComponent($('topic').value.trim())}`,()=>true);
  if(d.error){box.innerHTML=`<div class="smap"><span class="hint">${safe(d.error)}</span>
    <button class="g" onclick="$('sm-${id}').innerHTML=''">Close</button></div>`;return;}
  d.rows.forEach((r,i)=>r.i=i);
  const old=SM[id],f=d.seat_filter;
  SM[id]={d,n:old?old.n:(f?f.together:pref('qty',2)),
    rows:old?old.rows:new Set(f?f.rows:pref('rows',[]).map(r=>r.toUpperCase())),
    cats:old?old.cats:new Set((f?f.categories:pref('categories',[]).map(c=>c.toUpperCase())).filter(c=>d.cats.some(x=>x.toUpperCase()===c)))};
  renderSeatMap(id);
}
function smBlocks(st){
  // free runs of n+ seats side by side, in the chosen rows and categories
  const out=[];
  for(const r of st.d.rows){
    if(st.cats.size&&!st.cats.has(r.cat.toUpperCase()))continue;
    if(st.rows.size&&!st.rows.has(r.label.toUpperCase()))continue;
    let run=[];const flush=()=>{if(run.length>=st.n)out.push({row:r,seats:run});run=[];};
    for(const x of r.seats){
      if(x[2]&&run.length&&x[0]===run[run.length-1][0]+1)run.push(x);
      else{flush();if(x[2])run=[x];}
    }
    flush();
  }
  return out;
}
function renderSeatMap(id){
  const st=SM[id],d=st.d,s=WATCHES[id]||{};
  const blocks=smBlocks(st);const inBlock=new Set();
  blocks.forEach(b=>b.seats.forEach(x=>inBlock.add(b.row.i+':'+x[0])));
  const cols=d.rows.flatMap(r=>r.seats.map(x=>x[0]));
  const c0=Math.min(...cols),c1=Math.max(...cols);
  const U=12,L=22,W=(c1-c0+1)*U+2*L;
  // rows top to bottom as BookMyShow lists them, with a named gap before each category
  let svg='',y=0,lastCat=null,prevY=null;
  for(const r of d.rows){
    if(r.cat!==lastCat){
      y+=lastCat===null?0:U*0.6;lastCat=r.cat;
      const on=!st.cats.size||st.cats.has(r.cat.toUpperCase());
      svg+=`<text x="${W/2}" y="${y+U*0.9}" text-anchor="middle" font-size="8.5" font-weight="700" letter-spacing="1"
        fill="${on?'#344054':'#98a2b3'}">${safe(r.cat)}${d.prices[r.cat]?' · ₹'+d.prices[r.cat]:''}</text>`;
      y+=U*1.3;
    }else if(prevY!==null&&r.y-prevY>1)y+=U*0.5;     // a walkway between rows
    prevY=r.y;
    const picked=st.rows.has(r.label.toUpperCase());
    const catOn=!st.cats.size||st.cats.has(r.cat.toUpperCase());
    for(const side of [0,W-L])svg+=`<g data-row="${safe(r.label)}"><rect x="${side}" y="${y}" width="${L-3}" height="${U-2}" rx="3"
      fill="${picked?'#d62d45':'transparent'}"/><text x="${side+(L-3)/2}" y="${y+U-3.5}" text-anchor="middle" font-size="8.5"
      font-weight="700" fill="${picked?'#fff':'#667085'}">${safe(r.label)}</text></g>`;
    for(const x of r.seats){
      const hit=inBlock.has(r.i+':'+x[0]);
      const fill=hit?'#16804a':x[2]?(catOn?'#fff':'#f3f4f6'):'#d0d5dd';
      const stroke=hit?'#16804a':x[2]?(catOn?'#16804a':'#d0d5dd'):'#d0d5dd';
      svg+=`<rect x="${L+(x[0]-c0)*U+1}" y="${y+1}" width="${U-3}" height="${U-3}" rx="2" fill="${fill}" stroke="${stroke}"
        stroke-width="0.8"><title>${safe(r.label+x[1])} · ${safe(r.cat)} · ${x[2]?'free':'sold'}</title></rect>`;
    }
    y+=U;
  }
  const sy=y+12,H=y+34;
  svg+=`<rect x="${L+W*0.2}" y="${sy}" width="${W*0.6-2*L}" height="4" rx="2" fill="#98a2b3"/>
    <text x="${W/2}" y="${sy+17}" text-anchor="middle" font-size="9" fill="#667085" letter-spacing="2">SCREEN THIS WAY</text>`;
  const ago=d.at?Math.max(0,Math.round(Date.now()/1000-d.at)):0;
  const names=blocks.map(b=>{const n=b.seats.map(x=>x[1]),lo=Math.min(...n),hi=Math.max(...n);   // some cinemas number right to left
    return lo===hi?b.row.label+lo:`${b.row.label}${lo}-${b.row.label}${hi}`;});
  const fits=blocks.length?`<b>${blocks.length} block(s) of ${st.n}+ together free now:</b> ${safe(names.slice(0,8).join(', '))}${names.length>8?' …':''}`
    :`No ${st.n} seats together free in your choice right now.${s.seat_filter?'':' A seat alert tells you the moment some free up.'}`;
  const rowsTxt=st.rows.size?[...st.rows].sort().join(' '):'any row';
  // "A" can be a row in more than one category: say so unless a category narrows it
  const twice=[...st.rows].filter(k=>new Set(d.rows.filter(r=>r.label.toUpperCase()===k&&
    (!st.cats.size||st.cats.has(r.cat.toUpperCase()))).map(r=>r.cat)).size>1);
  const twiceTxt=twice.length?` Row ${twice.sort().join(', ')} is in more than one category; pick a category to narrow it.`:'';
  const hl=s.hold_status;const canHold=LIVE&&!['pending','approved','triggered','held','booked'].includes(hl);
  const P=id==='p';
  $('sm-'+id).innerHTML=`${P&&document.body.classList.contains('picking')?`<button class="g pick-back" onclick="closePicker()">&#8249; Show all cinemas and times</button>`:''}<div class="smap">
    <div><b>${safe(d.movie||'')}</b> · ${safe(d.time||'')}${d.screen?' · '+safe(d.screen):''} · <span class="free">${d.free}</span> of ${d.total} seats free
      <span class="hint">(loaded ${ago<60?ago+'s':Math.round(ago/60)+' min'} ago)</span>
      <button class="g" onclick="${P?'loadPicker()':`seatMap(${id},true)`}">Refresh</button></div>
    <div class="frow"><b>Seats together</b><select onchange="SM['${id}'].n=+this.value;renderSeatMap('${id}')">${[1,2,3,4,5,6,7,8,9,10].map(n=>
      `<option ${n===st.n?'selected':''}>${n}</option>`).join('')}</select></div>
    <div class="frow"><b>Categories</b>${d.cats.map(c=>`<button class="fchip ${st.cats.has(c.toUpperCase())?'on':''}" data-smcat="${safe(c)}">${safe(c)}${
      d.prices[c]?` <span>₹${d.prices[c]}</span>`:''}</button>`).join('')}<span class="hint">none picked = any</span></div>
    <div class="hint">Tap row letters on the map to pick rows (now: ${safe(rowsTxt)}).${safe(twiceTxt)}
      ${st.rows.size?`<a href="#" onclick="SM['${id}'].rows.clear();renderSeatMap('${id}');return false">Clear rows</a>`:''}</div>
    <div class="svgbox"><svg viewBox="0 0 ${W} ${H}" data-smid="${id}">${svg}</svg></div>
    <div><span class="key"><i style="background:#fff;border:1px solid #16804a"></i>free</span>
      <span class="key"><i style="background:#16804a"></i>fits your choice</span><span class="key"><i style="background:#d0d5dd"></i>sold</span></div>
    <p class="fits">${fits}</p>
    ${P?pickerButtons(st):`<div class="row">
      <button class="g" onclick="setSeatAlert(${id})">${s.seat_filter?'Update seat alert':'Alert me when seats like these free up'}</button>
      ${s.seat_filter?`<button class="g" onclick="setSeatAlert(${id},true)">Turn off seat alert</button>`:''}
      ${canHold?`<button class="g" onclick="holdFromMap(${id})">Use for auto-hold request</button>`:''}
      <button class="g" onclick="$('sm-${id}').innerHTML=''">Close</button>
      <span id="smmsg-${id}" class="hint"></span></div>
    <p class="hint" style="margin-bottom:0">${s.seat_filter?'Seat alert on: ':'A seat alert '}checks this map about once a minute (sooner when BookMyShow's
      categories change) and tells you when ${st.n}+ seats together free up in your rows and categories, e.g. from cancellations.</p>`}</div>`;
}
document.addEventListener('click',e=>{
  const r=e.target.closest('svg[data-smid] [data-row]');
  if(r){const id=r.closest('svg').dataset.smid,k=r.dataset.row.toUpperCase(),st=SM[id];
    st.rows.has(k)?st.rows.delete(k):st.rows.add(k);renderSeatMap(id);return;}
  const c=e.target.closest('[data-smcat]');
  if(c){const id=c.closest('[id^="sm-"]').id.slice(3),k=c.dataset.smcat.toUpperCase(),st=SM[id];
    st.cats.has(k)?st.cats.delete(k):st.cats.add(k);renderSeatMap(id);}
});
// ---- showtime picker: tapping a time on the movie or cinema page opens its seat
// map right there; then track any opening, or only seats that fit a choice
let PICK=null;
// the action bar pinned to the bottom of the picker: track, and ask for auto-hold
// right here (no scrolling back up to the auto-hold box)
function pickerButtons(st){
  const pick=[st.n+' together',[...st.cats].join(', '),st.rows.size?'rows '+[...st.rows].sort().join(' '):''].filter(Boolean).join(' · ');
  const hold=LIVE?`<label class="check pick-hold"><input type="checkbox" id="pick-hold" ${st.hold?'checked':''}
      onchange="SM.p.hold=this.checked;renderSeatMap('p')"> <b>Also hold ${st.n} seat(s) for me automatically</b>
      <span class="hint">in ${safe(pick.replace(/^\d+ together( · )?/,'')||'any row')}; the owner approves first</span></label>
      ${st.hold?`<label class="inline">Max total incl. fees ₹<input id="pick-max" type="number" min="0" placeholder="no cap"
        value="${st.max||''}" onchange="SM.p.max=this.value" style="width:110px"></label>`:''}`
    :`<span class="hint">Auto-hold is off right now, so seats can't be reserved for you automatically.</span>`;
  return `<p class="hint pick-hint">"Any seat opening" alerts when this show's categories open up. "Only for…" checks the
      seat map about once a minute and alerts when ${st.n}+ seats together free up where you picked.</p>
    <div class="pick-actions">
    <div class="row">${hold}</div>
    <div class="row">
      <button onclick="trackPicked(false)">Track any seat opening</button>
      <button class="pick" onclick="trackPicked(true)">Only for ${safe(pick)}</button>
      <button class="g" onclick="closePicker()">Close</button></div>
    <span class="hint" id="pick-msg" role="status"></span></div>`;
}
function closePicker(){
  $('sm-p')?.remove();delete SM.p;PICK=null;
  document.querySelectorAll('.time-pill.picked').forEach(b=>b.classList.remove('picked'));
  document.querySelectorAll('.pick-focus').forEach(a=>a.classList.remove('pick-focus'));
  document.body.classList.remove('picking');
}
function openPicker(btn,info){
  if(PICK&&PICK.session===info.session&&PICK.venue===info.venue&&$('sm-p')){closePicker();return;}
  closePicker();PICK=info;btn.classList.add('picked');
  const box=document.createElement('div');box.id='sm-p';box.className='showpanel';
  (btn.closest('.format-section')||btn.closest('.time-grid')).insertAdjacentElement('afterend',box);
  // focus on this cinema: the other cinemas, filters and far-away forms fold away
  const card=btn.closest('article');
  if(card){card.classList.add('pick-focus');document.body.classList.add('picking');
    card.scrollIntoView({behavior:'smooth',block:'start'});}
  loadPicker();
}
// a seat map: "busy" (this visitor's last map still loading, or Chrome's queue full)
// is retried by itself, up to 5 times, 3 seconds apart, while still wanted
async function getMap(url,still,onWait){
  for(let i=0;;i++){
    let d;try{d=await (await fetch(url)).json();}catch(e){return {error:'Could not reach the site.'};}
    if(!d.busy||i>=5||!still())return d;
    if(onWait)onWait(d);
    await new Promise(r=>setTimeout(r,3000));
    if(!still())return d;
  }
}
async function loadPicker(){
  const P=PICK,box=$('sm-p');if(!P||!box)return;
  box.innerHTML=`<div class="smap"><span class="hint">Loading the seat map for ${safe(P.time||'this show')} (a few seconds)…</span></div>`;
  const d=await getMap(`/api/showmap?venue=${encodeURIComponent(P.venue)}&date=${P.date}&session=${encodeURIComponent(P.session)}`,
    ()=>PICK===P&&!!$('sm-p'),x=>{const h=box.querySelector('.hint');if(h)h.textContent=x.error;});
  if(PICK!==P||!$('sm-p'))return;          // another time was tapped meanwhile
  if(d.error){
    box.innerHTML=`<div class="smap"><div><b>${safe(d.movie||P.movie||'')}</b> · ${safe(d.time||P.time||'')}</div>
      <p class="hint">${safe(d.error)}</p><div class="row"><button onclick="trackPicked(false)">Track any seat opening</button>
      <button class="g" onclick="closePicker()">Close</button></div><p class="hint" id="pick-msg"></p></div>`;return;}
  d.rows.forEach((r,i)=>r.i=i);
  const old=SM.p&&SM.p.key===P.venue+P.session?SM.p:null;
  SM.p={key:P.venue+P.session,d,n:old?old.n:pref('qty',2),rows:old?old.rows:new Set(pref('rows',[]).map(r=>r.toUpperCase())),
    cats:old?old.cats:new Set(pref('categories',[]).map(c=>c.toUpperCase()).filter(c=>d.cats.some(x=>x.toUpperCase()===c))),
    hold:old?old.hold:false,max:old?old.max:(pref('max_total',0)||'')};
  renderSeatMap('p');
  if(!old)box.scrollIntoView({behavior:'smooth',block:'start'});
}
async function trackPicked(seatsOnly){
  const P=PICK,st=SM.p;if(!P)return;
  const msg=$('pick-msg')||$(P.msg);
  const t=$('topic').value.trim();if(!t){alert('Enter your notification ID first (top of the page).');$('topic').focus();return;}
  if(st&&st.hold&&$('pick-max'))st.max=$('pick-max').value;
  msg.textContent='Saving…';
  await addWatch({venue:P.venue,date:P.date,session:P.session,movie:P.movie},msg,null);
  const said=[msg.textContent];
  if(!st||(!seatsOnly&&!st.hold)){list();return;}
  const w=await (await fetch('/api/subs?topic='+encodeURIComponent(t))).json();
  const sub=(w.subs||[]).find(x=>x.one_show&&x.venue_code===P.venue&&x.session===P.session);
  if(!sub){msg.textContent+=' (Couldn\'t finish setting it up; open the watch in Your watches.)';return;}
  const post=(u,b)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());
  const cats=[...st.cats].map(c=>st.d.cats.find(x=>x.toUpperCase()===c)||c),rows=[...st.rows].sort();
  if(seatsOnly){
    const r=await post('/api/subs/seats',{id:sub.id,topic:t,together:st.n,categories:cats,rows});
    said[0]=r.error?'Watching, but the seat alert failed: '+r.error:`Watching. You'll be alerted only when ${r.text} frees up.`;
  }
  if(st.hold){
    const h=await post('/api/subs/hold',{id:sub.id,topic:t,qty:st.n,max_total:st.max||0,categories:cats.join(', '),rows:rows.join(', ')});
    said.push(h.error?'Auto-hold not sent: '+h.error:h.auto?'Auto-hold approved (you are on the trusted list).':'Auto-hold requested; the owner approves it.');
  }
  msg.textContent=said.join(' ');
  list();
}
const OPEN_WS=new Set();
function wShows(s){
  return `<div class="time-grid">${byTime(s.shows).map(x=>pill(x,s.movie?(x.screen||''):(x.movie||'Show'))).join('')}</div>`;
}
function toggleWShows(id){
  OPEN_WS.has(id)?OPEN_WS.delete(id):OPEN_WS.add(id);
  if(PICK&&$('ws-'+id)?.contains($('sm-p')))closePicker();
  $('ws-'+id).innerHTML=OPEN_WS.has(id)?wShows(WATCHES[id]):'';
}
$('list').addEventListener('click',e=>{
  const b=e.target.closest('.wshows [data-vsession]');if(!b)return;
  const s=WATCHES[b.closest('.wshows').id.slice(3)];
  openPicker(b,{venue:s.venue_code,date:s.date_code,session:b.dataset.vsession,movie:b.dataset.vmovieOf,
    time:b.querySelector('b')?.textContent,hold:'',msg:'pick-msg'});
});
async function setSeatAlert(id,clear){
  const st=SM[id];
  const r=await (await fetch('/api/subs/seats',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id,topic:$('topic').value.trim(),clear:!!clear,together:st.n,
      categories:[...st.cats],rows:[...st.rows]})})).json();
  if(r.error){$('smmsg-'+id).textContent=r.error;return;}
  $('sm-'+id).innerHTML='';delete SM[id];await list();
}
function holdFromMap(id){
  const st=SM[id],p='w'+id;
  if(!$('hf-'+id).innerHTML)holdForm(id);
  $(p+'-qty').value=String(st.n);
  $(p+'-cats').value=[...st.cats].map(c=>st.d.cats.find(x=>x.toUpperCase()===c)||c).join(', ');
  $(p+'-rows').value=[...st.rows].sort().join(', ');markCatChips(p);
  $('hf-'+id).scrollIntoView({behavior:'smooth',block:'center'});
}
// ---- group booking
const OPEN_GP=new Set();
const JOIN_CODE=new URLSearchParams(location.search).get('join');
const GSTATUS={pending:'Auto-hold asked, waiting for the owner',approved:'Auto-hold approved, holds when seats open',
  triggered:'Holding seats now…',held:'Seats held',booked:'Booked',found:'Seats found (test mode)',failed:"Hold didn't work",
  noanswer:'No answer from the tracker',rejected:'Auto-hold declined',released:'Seats released'};
function groupLine(g){
  return `${g.total} seats: you ×${g.org_seats}`+g.members.map(m=>`, ${m.name} ×${m.seats}`).join('')+
    (g.status&&GSTATUS[g.status]?` · ${GSTATUS[g.status]}`:'');
}
function upiLink(g,m){
  return `upi://pay?pa=${encodeURIComponent(g.upi)}&pn=${encodeURIComponent(g.organizer)}&am=${m.share}.00&cu=INR`+
    `&tn=${encodeURIComponent((g.movie||'Tickets').slice(0,30)+' x'+m.seats)}`;
}
function groupPanel(id){
  if(OPEN_GP.has(id)){OPEN_GP.delete(id);$('gp-'+id).innerHTML='';return;}
  OPEN_GP.add(id);renderGroup(id);
}
function renderGroup(id){
  const s=WATCHES[id],g=s&&s.group,box=$('gp-'+id);if(!box)return;
  if(!g){
    box.innerHTML=`<div class="grp"><b>Book with friends</b>
      <p class="hint" style="margin:4px 0 8px">Share a link; each friend says how many seats they want. One auto-hold then
        holds everyone's seats side by side (up to 10). You pay BookMyShow as usual, and friends pay you back their share by UPI.</p>
      <div class="fields"><div><label>Your seats</label><select id="gs-${id}">${[1,2,3,4,5,6].map(n=>
        `<option ${n===pref('qty',2)?'selected':''}>${n}</option>`).join('')}</select></div>
      <div><label>Your UPI ID, optional</label><input id="gu-${id}" placeholder="name@okbank" autocomplete="off"></div></div>
      <button onclick="saveGroup(${id})">Create group link</button> <span id="gm-${id}" class="hint"></span></div>`;
    return;
  }
  const paid=g.status==='booked';
  box.innerHTML=`<div class="grp"><b>Your group · ${g.total} of 10 seats</b>
    ${g.locked?`<p class="hint" style="margin:4px 0">${safe(GSTATUS[g.status]||g.status)}: the group is closed to changes.</p>`
    :`<div class="link"><input readonly value="${safe(g.link)}" id="gl-${id}" onclick="this.select()">
      <button class="g" onclick="shareGroup(${id})">Share</button><button class="g" onclick="copyGroup(${id})">Copy</button></div>
      <p class="hint" style="margin:0">Send this link to friends. When everyone's in, request auto-hold below: it asks for all ${g.total} seats.</p>`}
    <table><tr><td><b>You</b> (${safe(g.organizer)})</td><td>${g.org_seats} seat(s)</td><td>${paid&&g.per_seat?'₹'+Math.round(g.per_seat*g.org_seats):''}</td></tr>
    ${g.members.map(m=>`<tr><td>${safe(m.name)}</td><td>${m.seats} seat(s)</td><td>${paid?
      `₹${m.share} <label class="check" style="display:inline"><input type="checkbox" style="width:auto" ${m.paid?'checked':''}
        onchange="markPaid('${safe(g.code)}',this.dataset.n,this.checked)" data-n="${safe(m.name)}"> paid</label>`
      :g.locked?'':`<button class="g" data-n="${safe(m.name)}" onclick="removeMember('${safe(g.code)}',this.dataset.n,${id})">Remove</button>`}</td></tr>`).join('')}
    </table>
    ${g.members.length?'':'<p class="hint">Nobody has joined yet.</p>'}
    ${paid&&g.amount?`<p class="hint">Total ₹${g.amount} · ₹${g.per_seat} a seat. Tick friends off as they pay you.</p>`:''}
    ${g.locked?'':`<div class="fields" style="margin-top:10px"><div><label>Your seats</label><select id="gs-${id}">${[1,2,3,4,5,6].map(n=>
      `<option ${n===g.org_seats?'selected':''}>${n}</option>`).join('')}</select></div>
      <div><label>Your UPI ID, optional</label><input id="gu-${id}" value="${safe(g.upi||'')}" placeholder="name@okbank" autocomplete="off"></div></div>
      <button class="g" onclick="saveGroup(${id})">Save</button> <button class="g" onclick="deleteGroup('${safe(g.code)}')">Cancel group</button>`}
    <span id="gm-${id}" class="hint"></span></div>`;
}
const gpost=(u,b)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());
async function saveGroup(id){
  const r=await gpost('/api/groups/create',{id,topic:$('topic').value.trim(),seats:$('gs-'+id).value,upi:$('gu-'+id).value.trim()});
  if(r.error){$('gm-'+id).textContent=r.error;return;}
  OPEN_GP.add(id);await list();
}
async function removeMember(code,name,id){const r=await gpost('/api/groups/leave',{code,name});if(r.error)alert(r.error);list();}
async function markPaid(code,name,paid){await gpost('/api/groups/paid',{code,name,paid});}
async function deleteGroup(code){if(!confirm('Cancel this group? Friends who joined are removed.'))return;
  const r=await gpost('/api/groups/delete',{code});if(r.error)alert(r.error);list();}
function copyGroup(id){const el=$('gl-'+id);el.select();navigator.clipboard?.writeText(el.value);$('gm-'+id).textContent='Link copied.';}
function shareGroup(id){const g=WATCHES[id].group;
  if(navigator.share)navigator.share({title:'Join my movie group',text:`Join me for ${g.movie||g.venue} (${g.date}${g.time?' '+g.time:''})`,url:g.link}).catch(()=>{});
  else copyGroup(id);}
// groups I joined (the ones I organise show on my watch)
async function loadGroups(){
  if(!ME)return;
  let d;try{d=await (await fetch('/api/groups')).json();}catch(e){return;}
  const mine=(d.groups||[]).filter(g=>!g.me_organizer);
  $('groups-box').innerHTML=mine.length?`<h2>Groups you're in</h2>`+mine.map(g=>{
    const me=g.members.find(m=>m.me)||{seats:0,share:0};
    const pay=g.status==='booked'&&me.share&&g.upi&&!me.paid;
    return `<div class="stub"><div class="v">Group · ${safe(g.organizer)}'s booking</div>
      <div class="d">${safe(g.movie||g.venue)}${g.time?' · '+safe(g.time):''}</div>
      <div class="f">${safe(g.venue)} · ${safe(g.date)} · you ×${me.seats}, ${g.total} seats in all</div>
      <div class="wl">${safe(GSTATUS[g.status]||'Waiting for '+g.organizer+' to request auto-hold')}${g.seats_text?' · '+safe(g.seats_text):''}</div>
      ${g.status==='booked'?(me.paid?'<p class="hint">Paid. Thanks!</p>':me.share?`<div class="paybox">
        <div><b>Your share: ₹${me.share}</b> for ${me.seats} seat(s)<br>${g.upi?`<a href="${safe(upiLink(g,me))}"><button>Pay ${safe(g.organizer)} by UPI</button></a>
          <div class="hint">On a computer, scan the code with your UPI app. UPI ID ${safe(g.upi)}</div>`
          :`<span class="hint">${safe(g.organizer)} hasn't added a UPI ID; settle up with them directly.</span>`}</div>
        ${g.upi?`<div class="qr" data-upi="${safe(upiLink(g,me))}"></div>`:''}</div>`:''):''}
      ${g.locked?'':`<div class="x"><button class="g" onclick="leaveGroup('${safe(g.code)}')">Leave</button></div>`}</div>`;}).join(''):'';
  document.querySelectorAll('.paybox .qr[data-upi]').forEach(el=>{if(window.QRCode&&!el.innerHTML)new QRCode(el,{text:el.dataset.upi,width:120,height:120});});
}
async function leaveGroup(code){const r=await gpost('/api/groups/leave',{code});if(r.error)alert(r.error);loadGroups();showJoin();}
// arriving from a friend's link: /tools?join=CODE
async function showJoin(){
  if(!JOIN_CODE)return;
  const g=await (await fetch('/api/groups/info?code='+encodeURIComponent(JOIN_CODE))).json();
  const box=$('join-card');
  if(g.error){box.innerHTML=`<div class="join-card"><b>${safe(g.error)}</b></div>`;return;}
  if(g.me_organizer){box.innerHTML=`<div class="join-card">This is your own group link. Send it to friends; they join from it.</div>`;return;}
  const me=g.members.find(m=>m.me);
  box.innerHTML=`<div class="join-card"><h3>${safe(g.organizer)} invited you to book together</h3>
    <div><b>${safe(g.movie||g.venue)}</b>${g.time?' · '+safe(g.time):''} · ${safe(g.venue)} · ${safe(g.date)}</div>
    <div class="hint">${g.total} seat(s) so far: ${safe(g.organizer)} ×${g.org_seats}${g.members.map(m=>', '+safe(m.name)+' ×'+m.seats).join('')}.
      Everyone's seats are held side by side. ${safe(g.organizer)} pays BookMyShow; you pay them back your share by UPI.</div>
    ${g.locked?`<p><b>${me?"You're in.":'This group is closed:'}</b> ${safe(GSTATUS[g.status]||'')}</p>`:
    `<div class="row"><label style="margin:0">Seats for you</label><select id="join-seats">${[1,2,3,4,5,6].map(n=>
      `<option ${n===(me?me.seats:1)?'selected':''}>${n}</option>`).join('')}</select>
      <button onclick="joinGroup()">${me?'Update':'Join the group'}</button>
      ${me?`<button class="g" onclick="leaveGroup('${safe(g.code)}')">Leave</button>`:''}</div>
    <p class="hint" style="margin-bottom:0">Updates go to your notification ID above${$('topic').value.trim()?'':' (set one to get them)'}.</p>`}
    <span id="join-msg" class="hint"></span></div>`;
}
async function joinGroup(){
  const r=await gpost('/api/groups/join',{code:JOIN_CODE,seats:$('join-seats').value,topic:$('topic').value.trim()});
  if(r.error){$('join-msg').textContent=r.error;return;}
  await showJoin();$('join-msg').textContent=`You're in. The group now wants ${r.total} seats.`;loadGroups();
}
showJoin();loadGroups();
// ---- your own holder: auto-holds on your own BookMyShow account, from your PC
async function loadMyHolder(){
  let d;try{d=await (await fetch('/api/myholder')).json();}catch(e){return;}
  const box=$('myholder');
  if(!d.set_up){
    box.innerHTML=`<p class="hint">Run a small holder on your own Windows PC, logged in to your own BookMyShow account.
      Your watches' auto-holds then hold seats on <b>your</b> account: the payment page opens on your PC, you pay yourself,
      and the tickets are yours. No owner approval needed. Your login never leaves your PC.</p>
      <p class="hint">You need: a Windows PC that's on during releases, Google Chrome and Python 3.</p>
      <button onclick="myHolder('create')">Set up my own holder</button>`;return;}
  const state=!d.enabled?'<span class="badge">switched off</span>':d.online?'<span class="badge approved">online</span>'
    :`<span class="badge failed">offline</span> <span class="hint">${d.seen!=null?'last seen '+Math.round(d.seen/60)+' min ago':'never connected yet'}</span>`;
  box.innerHTML=`<p>Your own holder: ${state} ${d.online&&!d.armed?'<span class="hint">(test mode: it only selects seats)</span>':''}</p>
    <ol class="hint" style="margin:6px 0 10px 18px;padding:0">
      <li>Download your setup pack and unzip it on your PC.</li>
      <li>Install <a href="https://www.python.org/downloads/" target="_blank" rel="noopener">Python 3</a> (tick "Add to PATH") and Google Chrome if you don't have them.</li>
      <li>Double-click <b>start_my_holder.bat</b>, log in to BookMyShow in the Chrome window it opens, press a key.</li>
      <li>This shows <b>online</b>. Keep both windows open during releases.</li></ol>
    <div class="row"><a class="g" href="/api/myholder/pack" style="text-decoration:none;padding:9px 13px;border:1px solid var(--edge);border-radius:9px;font-size:13px;font-weight:600;color:var(--ink)">Download my setup pack</a>
      <button class="g" onclick="myHolder('${d.enabled?'off':'on'}')">${d.enabled?'Switch off':'Switch on'}</button>
      <button class="g" onclick="if(confirm('Make a new key? Your current pack stops working until you download the new one.'))myHolder('reset')">Reset key</button>
      <button class="g" onclick="if(confirm('Remove your own holder? Auto-holds go back to the owner\\'s account (with approval).'))myHolder('delete')">Remove</button></div>
    <p class="hint">While it's online, your auto-hold requests go to it with no approval needed. When it's offline or
      switched off, they go to the owner as usual. Alerts from it go to your notification ID${d.notify?'':' (set one at the top first)'}.</p>`;
}
async function myHolder(what){
  await fetch('/api/myholder',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({do:what,topic:$('topic').value.trim()})});
  loadMyHolder();
}
setInterval(()=>{if($('myholder-box')?.open)loadMyHolder();},30000);
async function sendHold(id){
  const p='w'+id;
  const r=await (await fetch('/api/subs/hold',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:id,topic:$('topic').value.trim(),qty:$(p+'-qty').value,
      expire_min:$(p+'-exp')?.value,retries:$(p+'-retry')?.value,
      max_total:$(p+'-max').value,categories:$(p+'-cats').value,rows:$(p+'-rows').value})})).json();
  if(r.error){$('hmsg-'+id).textContent=r.error;return;}
  list();
}
async function cancelHold(id){
  await fetch('/api/subs/hold/cancel',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:id,topic:$('topic').value.trim()})});
  list();
}
async function del(id){
  await fetch('/api/subs/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:id,topic:$('topic').value.trim()})});
  list();
}
$('topic').addEventListener('change',()=>{list();showSubscribe();});
renderRecent();
boot();
checkLive(); setInterval(checkLive,30000);
</script></body></html>"""



ADMIN_PAGE = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Seat Watch · Admin</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@700;800&family=Figtree:wght@400;500;600;700&display=swap">
<style>
:root{--paper:#f3f2ef;--ink:#1d1a21;--dim:#5d5866;--edge:#dedbe2;--marquee:#a3123a;--marquee-dark:#7c0d2c;
  --open:#1f8a5b;--bad:#c0352b;--warn:#b07d10;
  --display:"Big Shoulders Display","Arial Narrow",Impact,sans-serif;--body:Figtree,ui-sans-serif,system-ui,"Segoe UI",sans-serif;
  --card-shadow:0 1px 0 var(--edge),0 10px 26px rgba(29,26,33,.06)}
*{box-sizing:border-box}
html{scroll-padding-top:84px}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 var(--body)}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.92em}
.wrap{max-width:1280px;margin:0 auto;padding:0 clamp(16px,3.5vw,48px)}
.dim{color:var(--dim)}
a{color:var(--marquee)}

/* top bar, as on the site */
.a-bar{position:sticky;top:0;z-index:20;background:rgba(243,242,239,.9);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  border-bottom:1px solid var(--edge)}
.a-bar .wrap{display:flex;align-items:center;gap:14px;height:64px}
.logo{font:800 28px/1 var(--display);letter-spacing:.04em;text-transform:uppercase;color:var(--ink);text-decoration:none;white-space:nowrap}
.logo b{color:var(--marquee)}
.badge{font:800 13px/1 var(--display);letter-spacing:.14em;text-transform:uppercase;background:var(--ink);color:#fff;border-radius:99px;padding:6px 11px 5px}
.a-nav{display:flex;gap:2px;margin-left:8px}
.a-nav a{color:var(--dim);text-decoration:none;font-weight:600;font-size:14.5px;padding:8px 13px;border-radius:99px;transition:background .2s,color .2s}
.a-nav a:hover{background:rgba(29,26,33,.06);color:var(--ink)}
.a-open{margin-left:auto;background:var(--ink);color:#fff!important;text-decoration:none;border-radius:99px;padding:9px 16px;font-weight:600;font-size:14px;white-space:nowrap}
.a-open:hover{background:var(--marquee)}

/* headline */
.a-head{padding:clamp(28px,4vw,54px) 0 24px}
.kicker{display:flex;align-items:center;gap:10px;margin:0;font:700 13px/1.2 var(--body);letter-spacing:.16em;text-transform:uppercase;color:var(--marquee)}
.kicker i{width:9px;height:9px;border-radius:50%;background:var(--marquee);animation:live 1.8s infinite}
h1{font:800 clamp(54px,7vw,104px)/.86 var(--display);text-transform:uppercase;letter-spacing:.005em;margin:14px 0 14px;animation:rise .7s cubic-bezier(.2,.8,.2,1)}
h1 span,h2 span{color:var(--marquee)}
.sub{margin:0;color:var(--dim);font-size:clamp(15px,1.3vw,18px);max-width:64ch}

/* numbers */
.stats{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:1px;background:var(--edge);border-radius:18px;overflow:hidden;box-shadow:var(--card-shadow)}
.stat{background:#fff;padding:20px 20px 18px}
.stat b{display:block;font:800 clamp(38px,4vw,58px)/.9 var(--display)}
.stat span{display:block;margin-top:8px;color:var(--dim);font-size:13.5px}

/* panels */
.panel{background:#fff;border-radius:18px;box-shadow:var(--card-shadow);padding:clamp(18px,2.4vw,28px);margin-top:22px;min-width:0;
  animation:up .6s cubic-bezier(.2,.8,.2,1) both}
.panel.hot{box-shadow:0 0 0 2px var(--marquee),var(--card-shadow)}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px;margin-top:22px}
.grid2 .panel{margin-top:0}
.span2{grid-column:1/-1}
h2{font:800 clamp(28px,2.6vw,38px)/.95 var(--display);text-transform:uppercase;letter-spacing:.01em;margin:0 0 10px}
.p-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap}
.p-side{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.p-sub{color:var(--dim);font-size:14px;margin:0 0 14px;max-width:72ch}
.mini-h{display:block;font:800 18px/1 var(--display);letter-spacing:.06em;text-transform:uppercase;margin:20px 0 8px}

/* tables */
.tbl{overflow-x:auto;margin:6px 0 0;-webkit-overflow-scrolling:touch}
.tbl table{min-width:620px}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;font:700 11.5px/1.2 var(--body);letter-spacing:.12em;text-transform:uppercase;color:var(--dim);
  padding:10px 8px;border-bottom:2px solid var(--ink)}
td{padding:11px 8px;border-bottom:1px solid var(--edge);vertical-align:middle}
tbody tr:hover td{background:#faf9f7}

/* controls: the site's red button, outlined button and danger button */
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
button{padding:11px 18px;border:0;border-radius:12px;font:800 15px/1 var(--display);letter-spacing:.08em;text-transform:uppercase;
  background:var(--marquee);color:#fff;cursor:pointer;box-shadow:0 8px 18px rgba(163,18,58,.22);
  transition:transform .15s,box-shadow .15s,background .15s,color .15s}
button:hover{background:var(--marquee-dark);transform:translateY(-1px)}
button.g{background:transparent;color:var(--ink);box-shadow:inset 0 0 0 1.5px var(--ink);font:600 14px/1 var(--body);letter-spacing:0;text-transform:none}
button.g:hover{background:var(--ink);color:#fff}
button.r{background:transparent;color:var(--bad);box-shadow:inset 0 0 0 1.5px var(--bad);font:600 14px/1 var(--body);letter-spacing:0;text-transform:none}
button.r:hover{background:var(--bad);color:#fff}
td button{padding:8px 12px}
input{padding:11px 13px;background:#fff;color:var(--ink);border:1.5px solid var(--edge);border-radius:12px;font:15px var(--body);width:100px}
input:focus{outline:none;border-color:var(--marquee);box-shadow:0 0 0 3px rgba(163,18,58,.15)}
#tracker-command{width:min(100%,420px)}
.visitors{white-space:pre-line;font-size:14px;max-height:340px;overflow:auto;margin:0}

/* what an action said: a toast that fades away */
#msg{position:fixed;z-index:30;left:50%;bottom:22px;transform:translateX(-50%);max-width:calc(100vw - 32px);background:var(--ink);color:#fff;
  border-radius:14px;padding:12px 18px;font-size:14px;white-space:pre-wrap;box-shadow:0 16px 36px rgba(29,26,33,.3);animation:up .3s}
#msg:empty{display:none}

.a-foot{background:#141117;color:#6f6977;margin-top:56px;padding:34px 0 40px}
.a-foot .wrap{display:flex;gap:18px;align-items:baseline;flex-wrap:wrap}
.a-foot .logo{color:#fff}
.a-foot .tag{font:800 18px/1 var(--display);letter-spacing:.06em;text-transform:uppercase}

@keyframes live{0%{box-shadow:0 0 0 0 rgba(163,18,58,.5)}80%,100%{box-shadow:0 0 0 12px rgba(163,18,58,0)}}
@keyframes rise{from{opacity:0;transform:translateY(24px)}to{opacity:1;transform:none}}
@keyframes up{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@media(max-width:1000px){.stats{grid-template-columns:repeat(3,minmax(0,1fr))}.grid2{grid-template-columns:1fr}.a-nav{display:none}}
@media(max-width:560px){.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.badge{display:none}.a-open{padding:8px 12px}.logo{font-size:24px}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<div class="a-bar"><div class="wrap">
  <a class="logo" href="/">Seat <b>Watch</b></a><span class="badge">Admin</span>
  <nav class="a-nav" aria-label="Sections"><a href="#a-holds">Requests</a><a href="#a-watches">Watches</a>
    <a href="#a-site">Site</a><a href="#a-people">People</a><a href="#a-log">Activity</a></nav>
  <a class="a-open" href="/" target="_blank" rel="noopener">Open the site &#8599;</a>
</div></div>
<div class="wrap">
<header class="a-head">
  <p class="kicker"><i></i>Owner controls</p>
  <h1>Run the <span>show.</span></h1>
  <p class="sub">The Seat Watch control room. Anyone with this link has full control, so keep it private.</p>
</header>

<div class="stats">
  <div class="stat"><b id="subs">–</b><span>watches</span></div>
  <div class="stat"><b id="people">–</b><span>people</span></div>
  <div class="stat"><b id="pages">–</b><span>pages per check</span></div>
  <div class="stat"><b id="passes">–</b><span>checks</span></div>
  <div class="stat"><b id="alerts">–</b><span>alerts sent</span></div>
  <div class="stat"><b id="rate">–</b><span>requests a minute</span></div>
</div>
<div id="msg" role="status"></div>

<section class="panel hot" id="a-holds">
  <div class="p-head"><h2>Auto-hold <span>requests</span></h2>
    <div class="p-side"><span id="holder-status" class="dim"></span>
      <button class="g" onclick="act('tracker_start')">Turn on trackers</button></div></div>
  <p class="p-sub">Approved requests hold seats in your Chrome on your BookMyShow account (seat_holder.py --listen)
    the moment a matching show opens.</p>
  <div class="tbl"><table><thead><tr><th>#</th><th>Who</th><th>Show</th><th>Wants</th><th>Status</th><th></th></tr></thead>
    <tbody id="holds"></tbody></table></div>
</section>

<section class="panel" id="a-watches">
  <h2>Everyone's <span>watches</span></h2>
  <div class="tbl"><table><thead><tr><th>Person</th><th>Cinema</th><th>Date</th><th>What</th><th></th></tr></thead>
    <tbody id="rows"></tbody></table></div>
  <span class="mini-h">Blocked</span><div id="blocked" class="dim"></div>
</section>

<div class="grid2" id="a-site">
  <section class="panel">
    <h2>Site <span>controls</span></h2>
    <p class="p-sub">Pause every check, tidy up, or change how often pages are checked.</p>
    <div class="row">
      <button class="g" onclick="act('pause')" id="pausebtn">Pause polling</button>
      <button class="g" onclick="act('purge')">Purge past dates</button>
      <button class="r" onclick="if(confirm('Delete every website watch?'))act('killall')">Clear website watches</button>
    </div>
    <div class="row">
      <span class="dim">Check every</span><input id="iv" type="number" min="5" max="600">
      <span class="dim">seconds · max</span><input id="cap" type="number" min="1" max="200">
      <span class="dim">watches per person</span>
      <button class="g" onclick="saveSettings()">Save</button>
    </div>
  </section>
  <section class="panel">
    <h2>BookMyShow <span>requests</span></h2>
    <p class="p-sub">Everything the website asks BookMyShow, against a budget of at most 30 a minute, one at a time.
      A refusal pauses all checks and sends you an alert.</p>
    <div class="row"><span id="bms-budget"></span></div>
  </section>
  <section class="panel">
    <h2>Invite <span>code</span></h2>
    <p class="p-sub">With a code on, anyone can see the front page, but checking showtimes and seat maps, making
      watches and asking you anything needs the code (once per device). Your own browsers never need it. A new code
      locks out everyone until they get it.</p>
    <div class="row"><span id="invite-status" class="dim"></span></div>
    <div class="row"><input id="invite-code" placeholder="choose a code, e.g. FRIENDS26" style="width:240px;max-width:100%" autocomplete="off">
      <button class="g" onclick="act('invite',$('invite-code').value);$('invite-code').value=''">Set code</button>
      <button class="g" onclick="act('invite','new')">Make a random one</button>
      <button class="r" onclick="if(confirm('Turn the invite code off? Anyone will be able to use the tools.'))act('invite','')">Turn off</button></div>
  </section>
  <section class="panel">
    <h2>Movie <span>posters</span></h2>
    <p class="p-sub">Posters never come from BookMyShow. With a free TMDB key (themoviedb.org, Settings, API) they
      come from TMDB; without one, from Wikipedia. A poster is only shown when the match is sure; otherwise the movie
      keeps its title card.</p>
    <div class="row"><span id="poster-status" class="dim"></span></div>
    <div class="row"><input id="tmdb-key" type="password" placeholder="TMDB API key or read access token" autocomplete="off" style="width:420px;max-width:100%">
      <button class="g" onclick="act('tmdb_key',$('tmdb-key').value);$('tmdb-key').value=''">Save key</button></div>
  </section>
  <section class="panel span2">
    <h2>Local <span>tracker</span></h2>
    <div id="tracker-status" class="p-sub"></div>
    <div class="row">
      <button class="g" onclick="trackerAct('health')">Check status</button>
      <button class="g" onclick="trackerAct('pause')">Pause</button>
      <button class="g" onclick="trackerAct('resume')">Resume</button>
      <button class="r" onclick="if(confirm('Remove all local tracker targets?'))trackerAct('clear')">Clear local targets</button>
      <button class="r" onclick="if(confirm('Remove all local and website targets?'))trackerAct('clear all')">Clear all targets</button>
    </div>
    <div class="row">
      <input id="tracker-command" placeholder="Tracker command or BookMyShow URL" onkeydown="if(event.key==='Enter')sendTrackerCommand()">
      <button onclick="sendTrackerCommand()">Run command</button>
    </div>
    <div class="tbl"><table><thead><tr><th>Local target</th><th>Date</th><th>Filters</th><th></th></tr></thead>
      <tbody id="tracker-rows"></tbody></table></div>
  </section>
  <section class="panel span2">
    <h2>Cinemas</h2>
    <div class="row"><button class="g" onclick="importCity()">Import every cinema in Hyderabad</button>
      <input id="imp" placeholder="or paste a BookMyShow movie link" style="width:340px;max-width:100%">
      <button class="g" onclick="importVenues()">Import from that link</button> <span id="impmsg" class="dim"></span></div>
  </section>
</div>

<div class="grid2" id="a-people">
  <section class="panel">
    <h2>Trusted <span>people</span></h2>
    <p class="p-sub">Their auto-hold requests are approved automatically (you still get a notification). Use the name
      exactly as they entered it on the site.</p>
    <div class="row"><input id="trust-name" placeholder="name" style="width:220px" onkeydown="if(event.key==='Enter')trustAdd()">
      <button class="g" onclick="trustAdd()">Add</button></div>
    <div class="row"><span id="trusted" class="dim"></span></div>
    <div class="row"><span class="dim">Auto-hold requests per person per day</span>
      <input id="hold-limit" type="number" min="0" max="50" title="0 = no limit">
      <button class="g" onclick="saveSettings()">Save</button></div>
  </section>
  <section class="panel">
    <h2>Recent <span>visitors</span></h2>
    <p id="visitors" class="visitors dim"></p>
  </section>
</div>

<section class="panel" id="a-log">
  <h2>Activity <span>log</span></h2>
  <p class="p-sub">Requests, approvals, holds, payments and the tracker turning on/off, newest first.</p>
  <div class="tbl"><table><thead><tr><th>When</th><th>Who</th><th>What</th><th>Details</th></tr></thead>
    <tbody id="activity"></tbody></table></div>
</section>
</div>
<footer class="a-foot"><div class="wrap"><span class="logo">Seat <b>Watch</b></span><span class="tag">Housefull isn't the end.</span></div></footer>
<script>
const $=i=>document.getElementById(i);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const KEY=new URLSearchParams(location.search).get('key')||'';
const api=(p,b)=>fetch(p+(p.includes('?')?'&':'?')+'key='+encodeURIComponent(KEY),
  b?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:{})
  .then(r=>r.json());
async function load(){
  const d=await api('/api/admin/state');
  if(d.error){document.body.innerHTML='<div class="wrap" style="padding:70px 0"><p class="kicker"><i></i>Admin</p>'+
    '<h1>Wrong <span>key.</span></h1><p class="sub">This admin link is missing its key or the key is wrong.</p></div>';return;}
  subs.textContent=d.subs; people.textContent=d.people; pages.textContent=d.pages;
  passes.textContent=d.passes; alerts.textContent=d.alerts; rate.textContent=d.rate;
  $('pausebtn').textContent=d.paused?'Resume polling':'Pause polling';
  $('visitors').textContent=d.visitors.length?d.visitors.map(v=>
    v.name+' · '+v.visits+' visit(s) · '+v.last_seen.replace('T',' ')).join('\n'):'none';
  $('iv').value=d.interval; $('cap').value=d.max_subs;
  if(document.activeElement!==$('hold-limit'))$('hold-limit').value=d.hold_limit;
  $('trusted').innerHTML=(d.trusted||[]).length?d.trusted.map(n=>
    `<span class="mono">${esc(n)}</span> <button class="g" onclick='act("trust_remove",${JSON.stringify(n)})'>remove</button>`).join(' '):'nobody yet';
  const KIND_COLOR={requested:'',approved:'var(--open)',BOOKED:'var(--open)','seats held':'var(--open)',
    rejected:'var(--bad)','seats released':'var(--dim)','payment step failed':'var(--bad)'};
  $('activity').innerHTML=(d.activity||[]).map(a=>`<tr>
    <td class="mono dim" style="white-space:nowrap">${esc(a.at.replace('T',' ').slice(5,16))}</td>
    <td>${esc(a.who)}</td><td style="color:${KIND_COLOR[a.kind]||'inherit'};white-space:nowrap">${esc(a.kind)}</td>
    <td class="dim">${esc(a.detail)}</td></tr>`).join('')||'<tr><td colspan="4" class="dim">nothing yet</td></tr>';
  const tracker=d.tracker||{};
  const targets=Array.isArray(tracker.targets)?tracker.targets:[];
  $('tracker-status').textContent=tracker.error||(
    (tracker.paused?'Paused':'Running')+' · '+targets.length+' target(s) · '+
    (tracker.uptime||'')+' uptime');
  $('tracker-rows').innerHTML=targets.map((t,i)=>`<tr>
    <td>${esc(t.venue)}</td><td class="mono">${esc(t.date)}</td>
    <td class="dim">${esc(t.filters)}</td>
    <td style="text-align:right"><button class="g" onclick="trackerAct('remove ${i}')">Remove</button></td>
    </tr>`).join('')||'<tr><td colspan="4" class="dim">No local targets</td></tr>';
  const b=d.bms||{};
  const ago=t=>{const m=Math.round((Date.now()/1000-t)/60);return m<1?'just now':m<60?m+' min ago':Math.round(m/60)+' h ago';};
  $('bms-budget').innerHTML=`<b>${b.last_min??0}</b> in the last minute (limit ${b.per_min}) · <b>${b.last_hour??0}</b> in the last hour`+
    ` · <b>${b.pages??0}</b> of ${b.max_pages??0} cinema-days watched`+
    (b.late?` · <span style="color:var(--bad)">${b.late} page(s) behind schedule, worst ${Math.round((b.worst||0)/60)} min</span>`:' · all checks on time')+
    (b.paused_for?` · <span style="color:var(--bad)">paused ${b.paused_for}s after a refusal</span>`:'')+
    (b.refused_at?` · last refusal ${ago(b.refused_at)}`:' · no refusals since the website started');
  const iv=d.invite||{};
  const ivLink=iv.code&&iv.site?`${iv.site}/?invite=${encodeURIComponent(iv.code)}`:'';
  $('invite-status').innerHTML=iv.code?`<b>On</b> · code <b>${esc(iv.code)}</b>`+
    (ivLink?` · share link <a href="${esc(ivLink)}" target="_blank" rel="noopener">${esc(ivLink)}</a>
      <button class="g" onclick="navigator.clipboard.writeText('${esc(ivLink)}');this.textContent='Copied'">Copy</button>`:'')
    :'<b>Off</b> · anyone with the address can use the tools';
  const ps=d.posters||{};
  $('poster-status').innerHTML=`<b>${(ps.tmdb||0)+(ps.wikipedia||0)}</b> of ${ps.movies||0} movies have a poster`+
    ` (TMDB ${ps.tmdb||0}, Wikipedia ${ps.wikipedia||0}) · TMDB key ${ps.key?'<b>set</b>':'not set'}`;
  const hs=d.holder||{};
  $('holder-status').innerHTML=hs.live
    ? `<span style="color:var(--open)">&#9679; holder live</span>${hs.armed?'':' · test mode (not armed)'}`+
      (hs.warm&&hs.warm.length?' · warm: '+esc(hs.warm.join(', ')):'')
    : `<span style="color:var(--bad)">&#9679; holder offline</span> · ${esc(hs.why||'')}`;
  const holds=d.rows.filter(r=>r.hold_status);
  holds.sort((a,b)=>(a.hold_status==='pending'?0:1)-(b.hold_status==='pending'?0:1));
  $('holds').innerHTML=holds.map(r=>`<tr>
    <td class="mono">${r.id}</td><td>${esc(r.hold_by||r.topic)}</td>
    <td>${esc(r.venue)}<br><span class="dim mono">${esc(r.date)} · ${esc(r.what)}</span></td>
    <td class="dim">${esc(r.hold_text)}${r.hold_result?'<br>'+esc(r.hold_result):''}</td>
    <td class="${r.hold_status==='pending'?'':'dim'}">${esc(r.hold_status)}</td>
    <td style="text-align:right;white-space:nowrap">${r.hold_status==='pending'?
      `<button onclick="act('hold_approve',${r.id})">Approve</button>
       <button class="r" onclick="act('hold_reject',${r.id})">Reject</button>`:''}</td>
    </tr>`).join('')||'<tr><td colspan="6" class="dim">No requests</td></tr>';
  $('rows').innerHTML=d.rows.map(r=>`<tr>
    <td class="mono">${esc(r.topic)}</td><td>${esc(r.venue)}</td>
    <td class="mono">${esc(r.date)}</td><td class="dim">${esc(r.what)}${r.hold_status?
      ' · auto-hold '+esc(r.hold_status):''}</td>
    <td style="text-align:right;white-space:nowrap">
      <button class="g" onclick="act('drop',${r.id})">Drop</button>
      <button class="r" onclick="if(confirm('Block ${r.topic}?'))act('block','${r.topic}')">Block</button>
    </td></tr>`).join('') || '<tr><td colspan="5" class="dim">nothing</td></tr>';
  $('blocked').innerHTML=d.blocked.length? d.blocked.map(t=>
    `<span class="mono">${t}</span> <button class="g" onclick="act('unblock','${t}')">unblock</button><br>`
    ).join('') : 'none';
}
async function importCity(){
  document.getElementById('impmsg').textContent='searching BookMyShow…';
  const r=await (await fetch('/api/venues/import',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({all_city:true,city:'hyderabad'})})).json();
  document.getElementById('impmsg').textContent=r.message||r.error||'';
}
async function importVenues(){
  document.getElementById('impmsg').textContent='looking…';
  const r=await (await fetch('/api/venues/import',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:document.getElementById('imp').value.trim()})})).json();
  document.getElementById('impmsg').textContent=r.message||r.error||'';
}
let FLASH_T=0;
function flash(text){$('msg').textContent=text||'';clearTimeout(FLASH_T);if(text)FLASH_T=setTimeout(()=>$('msg').textContent='',6000);}
async function act(what,arg){
  const d=await api('/api/admin/act',{action:what,arg:arg});
  flash(d.message); load();
}
function trackerAct(command){act('tracker_command',command)}
function sendTrackerCommand(){
  const command=$('tracker-command').value.trim();
  if(command){trackerAct(command);$('tracker-command').value='';}
}
async function saveSettings(){
  const d=await api('/api/admin/act',{action:'settings',
    interval:$('iv').value,max_subs:$('cap').value,hold_limit:$('hold-limit').value});
  flash(d.message); load();
}
function trustAdd(){
  const n=$('trust-name').value.trim();if(!n)return;
  act('trust_add',n);$('trust-name').value='';
}
load(); setInterval(load,10000);
</script></body></html>"""

# what anyone may look at without giving a name: browsing, never anyone's data
PUBLIC_GETS = {"/api/movies", "/api/venues", "/api/venue-dates", "/api/movie-dates", "/api/movie-shows",
               "/api/shows", "/api/showmap", "/api/tracker", "/api/status", "/api/groups/info",
               "/api/openings", "/api/popular", "/api/upcoming"}


LOADING_MAPS = set()        # visitor addresses with a seat map loading right now
LOADING_LOCK = threading.Lock()
MAP_BUSY = "Your last seat map is still loading. Trying again in a few seconds…"


def seat_map_or_stale(url, who=None):
    """A seat map for a visitor: the shared copy if fresh, else a new load if the
    budget allows, else the last copy (marked stale). False = busy, nothing to show;
    "busy" = this visitor already has a load running (or Chrome's queue is full).
    One load at a time per visitor (`who` = their address; None for the owner)."""
    from seat_maps import CACHE_SECONDS
    sm = seat_maps()
    hit = sm.cache.get(url)
    if hit and time.time() - hit[0] <= CACHE_SECONDS:
        return hit[1]
    if who:
        with LOADING_LOCK:
            mine = who not in LOADING_MAPS
            if mine:
                LOADING_MAPS.add(who)
        if not mine:
            if hit:
                mark_stale(hit[0])
                return hit[1]
            return "busy"
    try:
        if not budget_take(max_wait=4 if hit else 8):
            if hit:
                mark_stale(hit[0])
                return hit[1]
            return False
        layout = sm.get(url)
        if not layout and hit:
            mark_stale(hit[0])
            return hit[1]
        if not layout and "busy" in (sm.last_error or ""):
            return "busy"
        return layout
    finally:
        if who:
            with LOADING_LOCK:
                LOADING_MAPS.discard(who)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _s(self, code, body, ctype="application/json", headers=None):
        age = getattr(FRESH, "age", 0)
        if age > STALE_AFTER and ctype == "application/json" and isinstance(body, str) and body.endswith("}"):
            # answered from an older copy: say how old, the page shows it
            body = body[:-1] + (", " if len(body) > 2 else "") + f'"stale_min": {max(2, round(age / 60))}}}'
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if "Cache-Control" not in (headers or {}):
            self.send_header("Cache-Control", "no-store")   # updates reach visitors on the next load
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _groups_post(self, b):
        me = visitor_name(self)
        J = lambda d: self._s(200, json.dumps(d))
        try:
            seats = int(b.get("seats") or 1)
        except (TypeError, ValueError):
            return J({"error": "Seats must be a number."})
        if self.path == "/api/groups/create":
            # the watch's owner starts (or edits) its group
            with db() as c:
                row = c.execute("SELECT * FROM subs WHERE id=? AND topic=? AND owner=?",
                                (b.get("id"), (b.get("topic") or "").strip(), me)).fetchone()
            if not row:
                return J({"error": "That watch isn't yours."})
            upi = str(b.get("upi") or "").strip()
            if upi and not UPI_RE.fullmatch(upi):
                return J({"error": "That doesn't look like a UPI ID (name@bank)."})
            g = group_for_sub(row["id"])
            if g and group_locked(dict(row)):
                return J({"error": "The group can't change while its auto-hold is active."})
            if not 1 <= seats <= GROUP_MAX or (g and seats + group_total(g) - g["seats"] > GROUP_MAX):
                return J({"error": f"At most {GROUP_MAX} seats for the whole group."})
            with LOCK, db() as c:
                if g:
                    c.execute("UPDATE groups SET seats=?, upi=? WHERE id=?", (seats, upi, g["id"]))
                else:
                    c.execute("INSERT INTO groups (sub_id,code,organizer,seats,upi,created) VALUES (?,?,?,?,?,?)",
                              (row["id"], secrets.token_urlsafe(8), me, seats, upi,
                               datetime.now().isoformat(timespec="seconds")))
            if not g:
                log_activity("group", me, f"started a group on {row['venue_name']} {row['date_code']}", row["id"])
            return J({"ok": True})

        g = group_by_code(b.get("code"))
        if not g:
            return J({"error": "That group link isn't valid any more."})
        with db() as c:
            row = c.execute("SELECT * FROM subs WHERE id=?", (g["sub_id"],)).fetchone()
        if not row:
            return J({"error": "That group's watch has ended."})
        sub, org = dict(row), me == g["organizer"]

        if self.path == "/api/groups/join":
            if not me:
                return J({"error": "Share your name first."})
            if org:
                return J({"error": "You started this group; change your own seats on your watch."})
            if group_locked(sub):
                return J({"error": "This group's seats are already being held, so it's closed to changes."})
            if not 1 <= seats <= 6:
                return J({"error": "Pick 1 to 6 seats."})
            others = [m for m in group_members(g) if m["name"] != me]
            if g["seats"] + sum(m["seats"] for m in others) + seats > GROUP_MAX:
                return J({"error": f"That would pass {GROUP_MAX} seats, BookMyShow's limit per booking."})
            topic = str(b.get("topic") or "").strip()[:64]
            with LOCK, db() as c:
                had = c.execute("SELECT 1 FROM group_members WHERE group_id=? AND name=?", (g["id"], me)).fetchone()
                c.execute("INSERT INTO group_members (group_id,name,seats,topic,joined) VALUES (?,?,?,?,?)"
                          " ON CONFLICT(group_id,name) DO UPDATE SET seats=excluded.seats, topic=excluded.topic",
                          (g["id"], me, seats, topic, datetime.now().isoformat(timespec="seconds")))
            total = group_total(g)
            push(sub["topic"], f"{me} {'changed to' if had else 'joined your group:'} {seats} seat(s)",
                 f"{sub['venue_name']} · {describe(sub)}\nGroup now: {group_names(g)}")
            log_activity("group", me, f"{'changed' if had else 'joined'} {g['organizer']}'s group "
                                      f"({seats} seats, total {total})", sub["id"])
            return J({"ok": True, "total": total})

        if self.path == "/api/groups/leave":
            name = str(b.get("name") or me) if org else me
            if group_locked(sub):
                return J({"error": "The group can't change while its auto-hold is active."})
            with LOCK, db() as c:
                c.execute("DELETE FROM group_members WHERE group_id=? AND name=?", (g["id"], name))
            if not org:
                push(sub["topic"], f"{me} left your group", f"Group now: {group_names(g)}")
            return J({"ok": True})

        if self.path == "/api/groups/paid" and org:
            with LOCK, db() as c:
                c.execute("UPDATE group_members SET paid=? WHERE group_id=? AND name=?",
                          (1 if b.get("paid") else 0, g["id"], str(b.get("name") or "")))
            return J({"ok": True})

        if self.path == "/api/groups/delete" and org:
            if group_locked(sub):
                return J({"error": "The group can't be removed while its auto-hold is active."})
            with LOCK, db() as c:
                c.execute("DELETE FROM group_members WHERE group_id=?", (g["id"],))
                c.execute("DELETE FROM groups WHERE id=?", (g["id"],))
            return J({"ok": True})
        return J({"error": "not allowed"})

    def do_GET(self):
        FRESH.age, FRESH.wait = 0, None
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in BROWSE_PATHS and limited(self, "browse"):
            return self._s(200, json.dumps({"error": SLOW_DOWN, "busy": True}))
        if u.path in ("/api/showmap", "/api/seatmap") and limited(self, "seatmap"):
            return self._s(200, json.dumps({"error": SLOW_DOWN, "busy": True}))
        if u.path in ("/", "/tools"):
            # the tools are the front page; browsing needs no name. An invite link
            # (?invite=CODE), or a friend's group link (?join=), lets them in for good.
            code, hdr = setting("invite_code", ""), {}
            if code and not has_invite(self):
                given = (q.get("invite") or [""])[0].strip().upper()
                joining = (q.get("join") or [""])[0]
                if (given and hmac.compare_digest(given, code)) or (joining and group_by_code(joining)):
                    hdr = invite_header(code)
            return self._s(200, PAGE, "text/html; charset=utf-8", headers=hdr)
        if u.path.startswith("/api/") and not u.path.startswith("/api/admin/") \
                and u.path not in INVITE_FREE and not has_invite(self):
            return self._s(401, json.dumps({"error": "need_invite"}))
        if u.path == "/api/upcoming":
            return self._s(200, json.dumps({"films": upcoming_view()}))
        if u.path == "/api/popular":
            return self._s(200, json.dumps(popular_now()))
        if u.path == "/api/openings":
            return self._s(200, json.dumps({"openings": openings_feed()}))
        if u.path == "/api/me":
            return self._s(200, json.dumps({"name": visitor_name(self)}))
        if (u.path.startswith("/api/") and not u.path.startswith("/api/admin/")
                and u.path not in PUBLIC_GETS and not visitor_name(self)):
            # personal data (watches, preferences, groups): the page asks for a name, then retries
            return self._s(401, json.dumps({"error": "need_name"}))

        if u.path == "/api/movies":
            movies = with_posters(movie_catalog())
            return self._s(200, json.dumps({"movies": movies}))

        if u.path == "/api/venue-dates":
            dates = venue_dates((q.get("venue") or [""])[0].upper())
            if dates is None:
                return self._s(200, json.dumps({"error": BUSY, "dates": []}))
            return self._s(200, json.dumps({"dates": dates}))

        if u.path == "/api/movie-dates":
            code = (q.get("movie") or [""])[0]
            movie = next((m for m in movie_catalog() if m["code"] == code), None)
            if not movie:
                return self._s(200, json.dumps({"error": "Unknown movie."}))
            dates = movie_dates(movie)
            if dates is None:
                return self._s(200, json.dumps({"error": BUSY, "dates": []}))
            # the page asks for the first open date's showtimes next: start on them now
            prefetch_shows(movie, [d["code"] for d in dates if d["open"]][:1])
            return self._s(200, json.dumps({"dates": dates}))

        if u.path == "/api/movie-shows":
            code = (q.get("movie") or [""])[0]
            date = (q.get("date") or [""])[0]
            movie = next((m for m in movie_catalog() if m["code"] == code), None)
            if not movie or not re.fullmatch(r"\d{8}", date):
                return self._s(200, json.dumps({"error": "Pick a movie and date first."}))
            venues = shows_cached(movie, date)
            if venues is None:
                return self._s(200, json.dumps({"error": BUSY}))
            return self._s(200, json.dumps({"venues": venues}))

        if u.path == "/api/venues":
            with db() as c:
                rows = c.execute("SELECT * FROM venues ORDER BY name").fetchall()
            return self._s(200, json.dumps({"venues": [
                {"code": r["code"], "name": r["name"], "region": r["region"],
                 "area": venue_area(r["name"])} for r in rows]}))

        if u.path == "/api/shows":
            code, date = (q.get("venue") or [""])[0], (q.get("date") or [""])[0]
            with db() as c:
                v = c.execute("SELECT * FROM venues WHERE code=?", (code,)).fetchone()
            if not v:
                return self._s(200, json.dumps({"error": "unknown cinema"}))
            url = venue_url(code, v["name"], v["region"], date)
            _, html = cinema_page(code, date, max_age=180)
            if not html:
                return self._s(200, json.dumps({"error": "couldn't reach BookMyShow"}))
            if page_date(html) != date:
                return self._s(200, json.dumps({"shows": [], "screens": [], "url": url}))
            shows, screens = [], []
            for s in parse_cinema_page(html):
                scr = s.get("attributes") or s.get("screen") or ""
                if scr and scr not in screens:
                    screens.append(scr)
                shows.append({
                    "time": s.get("show_time"), "screen": scr,
                    "screen_name": s.get("screen") or "", "attrs": s.get("attributes") or "",
                    "movie": s.get("movie"), "session": s.get("session_id"),
                    "open": str(s.get("avail")) not in ("0", "None", ""),
                    "cats": live_show(s)["cats"]})
            shows.sort(key=lambda x: (x["movie"] or "", x["time"] or ""))
            return self._s(200, json.dumps({"shows": shows, "screens": screens, "url": url}))

        if u.path == "/api/subs":
            topic = (q.get("topic") or [""])[0]
            me = visitor_name(self)
            with LOCK, db() as c:
                # watches made before owners existed go to whoever opens them first
                c.execute("UPDATE subs SET owner=? WHERE topic=? AND IFNULL(owner,'')=''", (me, topic))
                rows = c.execute("SELECT * FROM subs WHERE topic=? AND owner=? ORDER BY id DESC",
                                 (topic, me)).fetchall()
            out = []
            for r in rows:
                try:
                    pretty = datetime.strptime(r["date_code"], "%Y%m%d").strftime("%a %d %b")
                except Exception:
                    pretty = r["date_code"]
                opts = json.loads(r["hold_opts"] or "{}") if r["hold_opts"] else {}
                status, result = hold_outcome(dict(r))
                cur = ledger_since(dict(r)) if r["hold_status"] == "triggered" else []
                e = cur[-1] if cur else None
                held_at = None
                if e:
                    try:
                        held_at = next((datetime.strptime(f"{x['day']} {x['at']}", "%Y-%m-%d %H:%M:%S").timestamp()
                                        for x in cur if x.get("stage") == "held"), None)
                    except (KeyError, ValueError):
                        held_at = None
                price = None
                if e and (e.get("total") or e.get("payable")):
                    total, payable, qty = e.get("total"), e.get("payable"), e.get("qty") or opts.get("qty")
                    price = {"tickets": total, "qty": qty, "payable": payable,
                             "fees": round(float(payable) - float(total), 2) if payable and total else None,
                             "cap": opts.get("max_total") or 0}
                out.append({"id": r["id"], "venue_name": r["venue_name"] or r["venue"],
                            "hold_stage": (e or {}).get("stage", ""), "held_at": held_at, "price": price,
                            "cap": opts.get("max_total") or 0,
                            "date_pretty": pretty, "what": describe(dict(r)),
                            "hold_status": status, "hold_result": result,
                            "hold_deadline": pay_deadline(dict(r)),
                            "live": live_status(dict(r)),
                            "history": watch_history(r["id"]),
                            "hold_text": hold_opts_text(opts) if opts else "",
                            "one_show": bool(r["session"]), "venue_code": r["venue"], "session": r["session"] or "",
                            "date_code": r["date_code"], "movie": r["movie"] or "", "shows": watch_shows(dict(r)),
                            "group": group_view(group_for_sub(r["id"]), me) if group_for_sub(r["id"]) else None,
                            "seat_filter": seat_filter_of(dict(r)),
                            "cats": watch_cats(dict(r))})
            return self._s(200, json.dumps({"subs": out}))

        if u.path == "/api/groups":
            # groups I organise or joined
            me = visitor_name(self)
            with db() as c:
                ids = [r["id"] for r in c.execute(
                    "SELECT id FROM groups WHERE organizer=? UNION SELECT group_id FROM group_members WHERE name=?",
                    (me, me)).fetchall()]
                gs = [dict(r) for r in c.execute(
                    f"SELECT * FROM groups WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()] if ids else []
            out = [v for v in (group_view(g, me) for g in gs) if v]
            return self._s(200, json.dumps({"groups": out}))

        if u.path == "/api/groups/info":
            # the join link's preview
            g = group_by_code((q.get("code") or [""])[0])
            v = group_view(g, visitor_name(self)) if g else None
            if not v:
                return self._s(200, json.dumps({"error": "That group link isn't valid any more."}))
            v["joined"] = any(m["me"] for m in v["members"])
            return self._s(200, json.dumps(v))

        if u.path == "/api/showmap":
            # the seat map behind a showtime button, before anything is tracked
            from seat_maps import compact
            code = (q.get("venue") or [""])[0].upper()
            date, session = (q.get("date") or [""])[0], (q.get("session") or [""])[0]
            if not re.fullmatch(r"\d{8}", date) or not re.fullmatch(r"\d{1,12}", session):
                return self._s(200, json.dumps({"error": "bad show"}))
            v, show = cinema_show(code, date, session)
            if not show or not show.get("event_code"):
                return self._s(200, json.dumps({"error": "This show isn't listed on BookMyShow right now."}))
            info = {"movie": show.get("movie"), "time": show.get("show_time"),
                    "screen": show.get("attributes") or show.get("screen") or ""}
            if not holder_state()["live"]:
                return self._s(200, json.dumps({**info, "error": "The seat map needs the tracker on. "
                                                                 "You can still track this show."}))
            url = seat_url({"region": v["region"], "venue": code, "session": session, "date_code": date},
                           {"event_code": show["event_code"]})
            layout = seat_map_or_stale(url, None if is_admin(self) else client_ip(self))
            if layout == "busy":
                return self._s(200, json.dumps({**info, "error": MAP_BUSY, "busy": True}))
            if layout is False:
                return self._s(200, json.dumps({**info, "error": "The site is busy checking BookMyShow. "
                                                                 "Try again in a minute, or track this show."}))
            if not layout:
                return self._s(200, json.dumps({**info, "error": f"Couldn't load the seat map ({seat_maps().last_error}). "
                                                                 "You can still track this show."}))
            out = compact(layout)
            prices = {c["name"].upper(): c["price"] for c in live_show(show)["cats"]}
            out.update(info)
            out.update({"prices": {n: prices.get(n.upper(), 0) for n in out["cats"]},
                        "at": seat_maps().cached_at(url)})
            return self._s(200, json.dumps(out))

        if u.path == "/api/myholder":
            me = visitor_name(self)
            with db() as c:
                r = c.execute("SELECT * FROM personal_holders WHERE owner=?", (me,)).fetchone()
            if not r:
                return self._s(200, json.dumps({"set_up": False}))
            ago = time.time() - (r["last_beat"] or 0)
            return self._s(200, json.dumps({
                "set_up": True, "enabled": bool(r["enabled"]), "online": ago < PERSONAL_STALE and bool(r["chrome"]),
                "seen": int(ago) if r["last_beat"] else None, "armed": bool(r["armed"]),
                "chrome": bool(r["chrome"]), "notify": r["notify"] or ""}))

        if u.path == "/api/myholder/pack":
            # the setup pack: the holder's files, its config and a one-click starter
            me = visitor_name(self)
            with db() as c:
                r = c.execute("SELECT * FROM personal_holders WHERE owner=?", (me,)).fetchone()
            if not r:
                return self._s(404, json.dumps({"error": "Set up your own holder first."}))
            data = personal_pack(dict(r))
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="seatwatch-my-holder.zip"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if u.path == "/api/seatmap":
            # the seat map of a single-showtime watch (owner only): rows, seats, free/sold
            from seat_maps import compact
            topic = (q.get("topic") or [""])[0]
            with db() as c:
                row = c.execute("SELECT * FROM subs WHERE id=? AND topic=? AND owner=?",
                                ((q.get("id") or [""])[0], topic, visitor_name(self))).fetchone()
            if not row:
                return self._s(200, json.dumps({"error": "That watch isn't yours."}))
            sub = dict(row)
            if not sub["session"]:
                return self._s(200, json.dumps({"error": "Seat maps are for one showtime. Track a single time to see its seats."}))
            show = watched_show(sub)
            if not show or not show.get("event_code"):
                return self._s(200, json.dumps({"error": "This show isn't listed on BookMyShow right now."}))
            if not holder_state()["live"]:
                return self._s(200, json.dumps({"error": "Seat maps need the tracker on. Ask the owner to turn it on."}))
            layout = seat_map_or_stale(seat_url(sub, show), None if is_admin(self) else client_ip(self))
            if layout == "busy":
                return self._s(200, json.dumps({"error": MAP_BUSY, "busy": True}))
            if layout is False:
                return self._s(200, json.dumps({"error": "The site is busy checking BookMyShow. Try again in a minute."}))
            if not layout:
                return self._s(200, json.dumps({"error": f"Couldn't load the seat map: {seat_maps().last_error}. Try again."}))
            out = compact(layout)
            prices = {c["name"].upper(): c["price"] for c in show["cats"]}
            out.update({"prices": {n: prices.get(n.upper(), 0) for n in out["cats"]},
                        "at": seat_maps().cached_at(seat_url(sub, show)),
                        "time": show["time"], "movie": show.get("movie"),
                        "seat_filter": seat_filter_of(sub)})
            return self._s(200, json.dumps(out))

        if u.path == "/admin":
            if not is_admin(self):
                return self._s(403, "<h3 style='font:16px sans-serif'>Add ?key=YOUR_KEY "
                                    "(printed in the server console at startup)</h3>",
                               "text/html; charset=utf-8")
            b = ADMIN_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Set-Cookie",
                             f"adm={setting('admin_token')}; Path=/; HttpOnly; SameSite=Lax")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return

        if u.path == "/api/admin/state":
            if not is_admin(self):
                return self._s(403, json.dumps({"error": "forbidden"}))
            with db() as c:
                rows = c.execute("SELECT * FROM subs ORDER BY date_code, venue_name").fetchall()
                people = c.execute("SELECT COUNT(DISTINCT topic) n FROM subs").fetchone()["n"]
                pages = c.execute("SELECT COUNT(DISTINCT url) n FROM subs").fetchone()["n"]
                blocked = [r["topic"] for r in c.execute("SELECT topic FROM blocked").fetchall()]
                visitors = [dict(r) for r in c.execute(
                    "SELECT name,last_seen,visits FROM visitors ORDER BY last_seen DESC LIMIT 20").fetchall()]
            iv = int(setting("interval", "10") or 10)
            out = []
            for r in rows:
                try:
                    d = datetime.strptime(r["date_code"], "%Y%m%d").strftime("%a %d %b")
                except Exception:
                    d = r["date_code"]
                opts = json.loads(r["hold_opts"] or "{}") if r["hold_opts"] else {}
                status, result = hold_outcome(dict(r))
                out.append({"id": r["id"], "topic": r["topic"],
                            "venue": (r["venue_name"] or r["venue"])[:34],
                            "date": d, "what": describe(dict(r)),
                            "hold_status": status, "hold_result": result, "hold_by": r["hold_by"] or "",
                            "hold_text": hold_opts_text(opts) if opts else ""})
            return self._s(200, json.dumps({
                "subs": len(rows), "people": people, "pages": pages,
                "passes": STATE["passes"], "alerts": STATE["alerts"],
                "rate": round(pages * 60.0 / max(iv, 1), 1),
                "paused": setting("paused", "0") == "1",
                "interval": iv, "max_subs": int(setting("max_subs", "25") or 25),
                "rows": out, "blocked": blocked, "visitors": visitors,
                "tracker": tracker_state(), "holder": holder_state(),
                "trusted": sorted(trusted()), "hold_limit": int(setting("hold_limit", "3") or 3),
                "bms": {**budget_state(), "pages": len(watched_pages()),
                        "max_pages": int(setting("max_pages", str(MAX_PAGES)) or MAX_PAGES), **POLL_LAG},
                "posters": poster_state(),
                "invite": {"code": setting("invite_code", ""), "site": setting("public_url", "").rstrip("/")},
                "activity": activity_log()}))

        if u.path == "/api/prefs":
            return self._s(200, json.dumps({"prefs": get_prefs(visitor_name(self))}))

        if u.path == "/api/mwatch":
            topic = (q.get("topic") or [""])[0]
            with db() as c:
                rows = [dict(r) for r in c.execute("SELECT * FROM mwatch WHERE topic=? AND owner=? ORDER BY id DESC",
                                                   (topic, visitor_name(self))).fetchall()]
            out = []
            for w in rows:
                st = json.loads(w["state"] or "{}")
                summ = st.get("summary") or {}
                if "checked" not in st:
                    text, state = "first check in a moment", "waiting"
                elif not st.get("bookable"):
                    text, state = "bookings not open yet, you'll be told the moment they open", "closed"
                elif not summ:
                    text, state = "no matching show on the chosen date(s) yet", "none"
                else:
                    total = sum(summ.values())
                    days = ", ".join(datetime.strptime(d, "%Y%m%d").strftime("%d %b") + f": {n}"
                                     for d, n in sorted(summ.items()) if n)
                    text = f"{total} matching cinema-day(s)" + (f" ({days})" if days else "")
                    state = "open" if total else "none"
                out.append({"id": w["id"], "code": w["code"], "date": w["date"], "title": w["title"], "what": mwatch_describe(w),
                            "live": {"at": st.get("checked"), "state": state, "text": text},
                            "history": watch_history(-w["id"])})
            return self._s(200, json.dumps({"watches": out}))

        if u.path == "/api/tracker":
            hs = holder_state()
            ws = tracker_state()
            try:
                hb = json.loads((Path(__file__).resolve().parent / "holder_status.json").read_text(encoding="utf-8"))
                holder_up = bool(hb.get("running")) and time.time() - float(hb.get("at") or 0) < HOLDER_STALE
            except (OSError, ValueError):
                holder_up = False
            parts = [
                {"name": "Website", "ok": True, "text": "checking your watches"},
                {"name": "Watcher", "ok": "error" not in ws and not ws.get("paused"),
                 "text": "paused" if ws.get("paused") else "off" if "error" in ws else "on"},
                {"name": "Auto-hold", "ok": holder_up, "text": "on" if holder_up else hs.get("why") or "off"},
                {"name": "Browser", "ok": chrome_up(), "text": "connected" if chrome_up() else "not running"},
            ]
            return self._s(200, json.dumps({"live": hs["live"], "why": hs.get("why", ""), "parts": parts,
                                            "armed": bool(hs["live"] and hs.get("armed")),
                                            "test_mode": hs["live"] and not hs.get("armed")}))

        if u.path == "/api/status":
            with db() as c:
                n = c.execute("SELECT COUNT(*) n FROM subs").fetchone()["n"]
                p = c.execute("SELECT COUNT(DISTINCT url) n FROM subs").fetchone()["n"]
            secs = int(time.time() - STATE["started"])
            return self._s(200, json.dumps({"subs": n, "pages": p,
                                            "uptime": f"{secs // 3600}h {secs % 3600 // 60}m"}))
        return self._s(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        FRESH.age, FRESH.wait = 0, None
        try:
            n = int(self.headers.get("Content-Length") or 0)
            b = json.loads(self.rfile.read(n) or "{}")
        except Exception:
            return self._s(400, json.dumps({"error": "bad request"}))

        if self.path == "/api/access":
            name = re.sub(r"\s+", " ", (b.get("name") or "").strip())
            if not 2 <= len(name) <= 40 or any(ord(ch) < 32 for ch in name):
                return self._s(400, json.dumps({"error": "Enter a valid name."}))
            if limited(self, "name"):
                return self._s(200, json.dumps({"error": "Too many new names from your connection. Try again later."}))
            remember_visitor(name)
            return self._s(200, json.dumps({"ok": True, "name": name}), headers={
                "Set-Cookie": f"visitor={access_token(name)}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax"
            })
        if self.path == "/api/invite":
            code = setting("invite_code", "")
            given = re.sub(r"\s+", "", str(b.get("code") or "")).upper()[:40]
            if not code:
                return self._s(200, json.dumps({"ok": True}))
            if not rate_ok("invite@" + client_ip(self), 10, 3600):
                return self._s(200, json.dumps({"error": "Too many tries. Try again in an hour."}))
            if not given or not hmac.compare_digest(given, code):
                return self._s(200, json.dumps({"error": "That code isn't right. Ask the person who shared Seat Watch with you."}))
            return self._s(200, json.dumps({"ok": True}), headers=invite_header(code))
        if self.path.startswith("/api/") and not self.path.startswith("/api/admin/") \
                and self.path not in INVITE_FREE and not has_invite(self):
            return self._s(401, json.dumps({"error": "need_invite"}))
        if self.path.startswith("/api/") and not self.path.startswith("/api/admin/") \
                and self.path != "/api/tracker/ask" and not visitor_name(self):
            return self._s(401, json.dumps({"error": "need_name"}))

        if self.path == "/api/movies/add":
            link = urlparse((b.get("url") or "").strip())
            match = re.search(r"/(?:movies/(?:hyderabad/)?|hyderabad/movies/)([^/]+)/(ET\d+)/?$",
                              link.path)
            if link.hostname != "in.bookmyshow.com" or not match:
                return self._s(200, json.dumps({"error": "Enter a Hyderabad BookMyShow movie link."}))
            page = CLIENT.fetch(f"https://in.bookmyshow.com/movies/hyderabad/{match[1]}/{match[2]}",
                                as_json=False)
            metadata = MovieMetadata()
            if page:
                metadata.feed(page)
            movie = next((x for x in metadata.movies if x.get("@type") == "Movie"), {})
            if not movie.get("name"):
                return self._s(200, json.dumps({"error": "Could not find that movie."}))
            portrait = re.search(
                r'https://assets-in\.bmscdn\.com/iedb/movies/images/mobile/thumbnail/xlarge/[^"<> ]+?\.jpg',
                page)
            item = {"title": movie["name"], "slug": match[1], "code": match[2],
                    "poster": portrait.group(0) if portrait else movie.get("image") or "",
                    "genre": "/".join(movie.get("genre") or [])}
            try:
                saved = json.loads(setting("custom_movies", "[]"))
            except ValueError:
                saved = []
            saved = [x for x in saved if x.get("code") != item["code"]]
            saved.append(item)
            set_setting("custom_movies", json.dumps(saved))
            MOVIE_CACHE.update(at=0, items=[])
            return self._s(200, json.dumps({"movie": item}))

        if self.path == "/api/subs":
            topic = (b.get("topic") or "").strip()
            boss = is_owner(self, topic)
            if not boss and (not rate_ok("watch:" + visitor_name(self), 12, 600) or limited(self, "watch")):
                return self._s(200, json.dumps({"error": "That's a lot of new watches at once. Try again in a few minutes."}))
            code = (b.get("venue") or "").strip().upper()
            date = (b.get("date") or "").strip()
            screen = (b.get("screen") or "").strip()
            movie = (b.get("movie") or "").strip()
            session = (b.get("session") or "").strip()
            if len(topic) < 6:
                return self._s(200, json.dumps({"error": "Topic must be at least 6 characters."}))
            with db() as c:
                if c.execute("SELECT topic FROM blocked WHERE topic=?", (topic,)).fetchone():
                    return self._s(200, json.dumps({"error": "That topic has been blocked."}))
                n = c.execute("SELECT COUNT(*) n FROM subs WHERE topic=?", (topic,)).fetchone()["n"]
            cap = int(setting("max_subs", "25") or 25)
            if n >= cap:
                return self._s(200, json.dumps({
                    "error": f"You're at the limit of {cap} watches. Remove one first."}))
            with db() as c:
                v = c.execute("SELECT * FROM venues WHERE code=?", (code,)).fetchone()
            if not v or not re.fullmatch(r"\d{8}", date or ""):
                return self._s(200, json.dumps({"error": "Pick a cinema and a date first."}))
            url = venue_url(code, v["name"], v["region"], date)
            if not boss:
                with db() as c:
                    mine_ip = c.execute("SELECT COUNT(*) n FROM subs WHERE ip=?", (client_ip(self),)).fetchone()["n"]
                if mine_ip >= MAX_PER_IP:
                    return self._s(200, json.dumps({"error": f"There are already {MAX_PER_IP} watches from your connection. "
                                                             "Remove one first."}))
                pages = watched_pages()
                cap = int(setting("max_pages", str(MAX_PAGES)) or MAX_PAGES)
                if url not in pages and len(pages - owner_pages()) >= cap:
                    return self._s(200, json.dumps({"error": "The tracker is full right now: it already checks as many "
                                                             "cinema-days as BookMyShow allows. Pick a cinema and date "
                                                             "someone already tracks, or try again later."}))
            with LOCK, db() as c:
                dup = c.execute(
                    "SELECT id FROM subs WHERE topic=? AND owner=? AND url=? AND IFNULL(screen,'')=?"
                    " AND IFNULL(movie,'')=? AND IFNULL(session,'')=?",
                    (topic, visitor_name(self), url, screen, movie, session)).fetchone()
                if dup:
                    # the id lets the page still attach an auto-hold request to it
                    return self._s(200, json.dumps({"error": "You're already watching that.",
                                                    "id": dup["id"]}))
                new_id = c.execute("INSERT INTO subs (topic,venue,venue_name,region,date_code,"
                                   "screen,movie,session,url,created,owner,token,ip)"
                                   " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (topic, code, v["name"], v["region"], date, screen, movie,
                                    session, url, datetime.now().isoformat(timespec="seconds"),
                                    visitor_name(self), secrets.token_urlsafe(12), client_ip(self))).lastrowid
            try:
                pretty = datetime.strptime(date, "%Y%m%d").strftime("%a %d %b")
            except Exception:
                pretty = date
            push(topic, "Watching " + str(v["name"])[:34],
                 f"{v['name']}\n{pretty}\n"
                 f"{describe({'screen': screen, 'movie': movie, 'session': session})}\n\n"
                 "You'll be told when seats open or a show is added.", url)
            return self._s(200, json.dumps({"ok": True, "id": new_id}))

        if self.path == "/api/prefs":
            prefs = clean_prefs(b)
            with LOCK, db() as c:
                c.execute("INSERT OR REPLACE INTO prefs VALUES (?,?,?)",
                          (visitor_name(self), json.dumps(prefs), datetime.now().isoformat(timespec="seconds")))
            return self._s(200, json.dumps({"ok": True, "prefs": prefs}))

        if self.path == "/api/test-alert":
            # "Send me a test alert" while setting up ntfy; at most once a minute each
            topic = (b.get("topic") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", topic):
                return self._s(200, json.dumps({"error": "Topic must be 6-64 letters, digits, - or _."}))
            key = f"test:{visitor_name(self)}"
            if time.time() - ASKED["by"].get(key, 0) < 60:
                return self._s(200, json.dumps({"error": "Sent one a moment ago. Try again in a minute."}))
            ASKED["by"][key] = time.time()
            ok = push(topic, "Seat Watch test alert",
                      "It works! Alerts for your watches will arrive here.")
            return self._s(200, json.dumps({"ok": ok} if ok else {"error": "Couldn't reach ntfy. Try again."}))

        if self.path == "/api/tracker/ask" and not rate_ok("ask:" + client_ip(self), 5, 3600):
            return self._s(200, json.dumps({"message": "You've asked a few times already. The owner has been told."}))

        if self.path == "/api/tracker/ask":
            msg = ask_to_turn_on(visitor_name(self), (b.get("topic") or "").strip())
            return self._s(200, json.dumps({"message": msg}))

        if self.path == "/api/admin/pay-decide":
            # the Approve / Decline buttons of a "Pay for these seats?" alert. The
            # one-time code comes from the holder (pay_pending.json); the answer is
            # handed back through pay_decisions.json, which the holder watches.
            here = Path(__file__).resolve().parent
            code = str(b.get("code") or "")
            decision = b.get("decision")
            try:
                pending = json.loads((here / "pay_pending.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pending = {}
            try:
                decided = json.loads((here / "pay_decisions.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                decided = {}
            if not code or code not in pending or decision not in ("approve", "decline"):
                return self._s(403, json.dumps({"error": "invalid code"}))
            if code in decided:
                return self._s(200, json.dumps({"message": f"already answered: {decided[code]}"}))
            decided[code] = decision
            (here / "pay_decisions.json").write_text(json.dumps(decided, indent=1), encoding="utf-8")
            return self._s(200, json.dumps({"message": "approved: the UPI QR is on its way"
                                            if decision == "approve" else "declined: releasing the seats"}))

        if self.path == "/api/admin/tracker-start":
            # the Turn on button in the owner's ntfy alert (one-time code), or the admin portal
            code = str(b.get("code") or "")
            saved = setting("start_code", "")
            if not is_admin(self) and not (code and saved and hmac.compare_digest(code, saved)):
                return self._s(403, json.dumps({"error": "invalid or used code"}))
            return self._s(200, json.dumps({"message": start_trackers()}))

        if self.path == "/api/subs/hold":
            # a visitor asks for an auto-hold on one of their own watches
            with db() as c:
                row = c.execute("SELECT * FROM subs WHERE id=? AND topic=? AND owner=?",
                                (b.get("id"), (b.get("topic") or "").strip(), visitor_name(self))).fetchone()
            if not row:
                return self._s(200, json.dumps({"error": "That watch isn't yours."}))
            return self._s(200, json.dumps(request_hold(dict(row), visitor_name(self), b)))

        if self.path in ("/api/admin/holder-beat", "/api/admin/holder-result"):
            # a friend's own holder reporting in, authorised by its key
            key = str(b.get("key") or "")
            with db() as c:
                row = c.execute("SELECT * FROM personal_holders WHERE key=?", (key,)).fetchone() if key else None
            if not row or not hmac.compare_digest(key, row["key"]):
                return self._s(403, json.dumps({"error": "unknown holder"}))
            if self.path.endswith("beat"):
                with LOCK, db() as c:
                    c.execute("UPDATE personal_holders SET last_beat=?, chrome=?, armed=? WHERE owner=?",
                              (time.time(), 1 if b.get("chrome") else 0, 1 if b.get("armed") else 0, row["owner"]))
                return self._s(200, json.dumps({"ok": True}))
            e = b.get("entry") or {}
            with db() as c:
                sub = c.execute("SELECT owner FROM subs WHERE id=?", (e.get("sub_id"),)).fetchone()
            if not sub or sub["owner"] != row["owner"]:
                return self._s(403, json.dumps({"error": "not your watch"}))
            keep = {k: e.get(k) for k in ("day", "at", "stage", "sub_id", "detail", "seats", "total", "qty", "payable")}
            keep["own"] = True                 # held on the requester's own account, by their holder
            if keep["stage"] not in ("selected", "held", "pay_ask", "awaiting_payment", "booked", "released",
                                     "no_map", "no_seats", "click_failed", "mismatch", "over_budget",
                                     "pay_failed", "already_held", "limit"):
                return self._s(400, json.dumps({"error": "bad stage"}))
            with LOCK, db() as c:
                c.execute("INSERT INTO remote_ledger (sub_id, owner, data, at) VALUES (?,?,?,?)",
                          (keep["sub_id"], row["owner"], json.dumps(keep), datetime.now().isoformat(timespec="seconds")))
            return self._s(200, json.dumps({"ok": True}))

        if self.path == "/api/myholder":
            # set up / switch / reset this visitor's own holder
            me = visitor_name(self)
            act_ = b.get("do")
            with LOCK, db() as c:
                row = c.execute("SELECT * FROM personal_holders WHERE owner=?", (me,)).fetchone()
                if act_ == "create" and not row:
                    c.execute("INSERT INTO personal_holders (owner, topic, key, notify, created) VALUES (?,?,?,?,?)",
                              (me, "seatwatch-hold-" + secrets.token_urlsafe(12).lower().replace("_", "x"),
                               secrets.token_urlsafe(24), str(b.get("topic") or "")[:64],
                               datetime.now().isoformat(timespec="seconds")))
                elif act_ == "reset" and row:        # new topic and key: the old pack stops working
                    c.execute("UPDATE personal_holders SET topic=?, key=?, last_beat=0 WHERE owner=?",
                              ("seatwatch-hold-" + secrets.token_urlsafe(12).lower().replace("_", "x"),
                               secrets.token_urlsafe(24), me))
                elif act_ in ("on", "off") and row:
                    c.execute("UPDATE personal_holders SET enabled=? WHERE owner=?", (1 if act_ == "on" else 0, me))
                elif act_ == "delete" and row:
                    c.execute("DELETE FROM personal_holders WHERE owner=?", (me,))
                if b.get("topic") is not None and act_ != "delete":
                    c.execute("UPDATE personal_holders SET notify=? WHERE owner=?", (str(b.get("topic"))[:64], me))
            if act_ in ("create", "reset", "on", "off", "delete"):
                log_activity("own holder", me, act_)
            return self._s(200, json.dumps({"ok": True}))

        if self.path == "/api/admin/sub-hold":
            # "Request auto-hold" button in a watch's alert: sends the request right
            # there, with the person's saved preferences (or their seat alert's seats);
            # the watch's own secret token authorises it (no cookie on a notification tap)
            token = str(b.get("token") or "")
            with db() as c:
                row = c.execute("SELECT * FROM subs WHERE id=?", (b.get("id"),)).fetchone()
            if not row or not token or not row["token"] or not hmac.compare_digest(token, row["token"]):
                return self._s(403, json.dumps({"error": "invalid link"}))
            sub, who = dict(row), row["owner"] or row["topic"]
            p = get_prefs(row["owner"]) if row["owner"] else {}
            res = request_hold(sub, who, {"qty": p.get("qty") if not seat_filter_of(sub) else "",
                                          "max_total": p.get("max_total") or 0,
                                          "categories": ", ".join(p.get("categories") or []),
                                          "rows": ", ".join(p.get("rows") or [])})
            if res.get("error"):
                push(sub["topic"], "Auto-hold not requested", f"{sub['venue_name']} · {describe(sub)}\n{res['error']}")
            else:
                with db() as c:
                    opts = json.loads(c.execute("SELECT hold_opts FROM subs WHERE id=?", (sub["id"],)).fetchone()[0] or "{}")
                push(sub["topic"], "Auto-hold approved" if res.get("auto") else "Auto-hold requested",
                     f"{sub['venue_name']} · {describe(sub)}\n{hold_opts_text(opts)}\n\n"
                     + ("You're on the trusted list, so it's approved. " if res.get("auto") else
                        "The owner approves it; you'll be told here. ")
                     + "Seats, rows and price come from your saved preferences on the site.")
            return self._s(200, json.dumps(res))

        if self.path == "/api/subs/hold/cancel":
            with LOCK, db() as c:
                c.execute("UPDATE subs SET hold_status='', hold_code='' WHERE id=? AND topic=? AND owner=?",
                          (b.get("id"), (b.get("topic") or "").strip(), visitor_name(self)))
            return self._s(200, json.dumps({"ok": True}))

        if self.path.startswith("/api/groups/"):
            return self._groups_post(b)

        if self.path == "/api/subs/seats":
            # set (or clear) a seat-level alert on a single-showtime watch
            topic = (b.get("topic") or "").strip()
            with db() as c:
                row = c.execute("SELECT * FROM subs WHERE id=? AND topic=? AND owner=?",
                                (b.get("id"), topic, visitor_name(self))).fetchone()
            if not row:
                return self._s(200, json.dumps({"error": "That watch isn't yours."}))
            if b.get("clear"):
                with LOCK, db() as c:
                    c.execute("UPDATE subs SET seat_filter='', seat_seen='' WHERE id=?", (row["id"],))
                SEAT_LIVE.pop(row["id"], None)
                return self._s(200, json.dumps({"ok": True}))
            if not row["session"]:
                return self._s(200, json.dumps({"error": "Seat alerts are for one showtime."}))
            sf, err = clean_seat_filter(b)
            if err:
                return self._s(200, json.dumps({"error": err}))
            cap = int(setting("max_seat_watches", "12") or 12)
            with db() as c:
                n = c.execute("SELECT COUNT(*) n FROM subs WHERE IFNULL(seat_filter,'') NOT IN ('','null')"
                              " AND id<>?", (row["id"],)).fetchone()["n"]
            if n >= cap:
                return self._s(200, json.dumps({"error": f"All {cap} seat-alert slots are in use. Try later."}))
            with LOCK, db() as c:
                c.execute("UPDATE subs SET seat_filter=?, seat_seen='' WHERE id=?", (json.dumps(sf), row["id"]))
            SEAT_LIVE.pop(row["id"], None)
            SEAT_DUE.add(row["id"])
            log_activity("seat alert", visitor_name(self), f"{row['venue_name']} {row['date_code']} · "
                                                           f"{seat_filter_text(sf)}", row["id"])
            return self._s(200, json.dumps({"ok": True, "text": seat_filter_text(sf)}))

        if self.path == "/api/admin/hold-decide":
            # the Approve / Reject buttons in the owner's ntfy alert; authorised by
            # the request's one-time code instead of the admin key
            code = str(b.get("code") or "")
            with db() as c:
                row = c.execute("SELECT hold_code FROM subs WHERE id=?", (b.get("id"),)).fetchone()
            if not row or not code or not row["hold_code"] or not hmac.compare_digest(code, row["hold_code"]):
                return self._s(403, json.dumps({"error": "invalid or used code"}))
            msg = decide_hold(b.get("id"), b.get("decision") == "approve")
            return self._s(200, json.dumps({"message": msg}))

        if self.path.startswith("/api/admin/act"):
            if not is_admin(self):
                return self._s(403, json.dumps({"error": "forbidden"}))
            a = (b.get("action") or "").strip()
            arg = b.get("arg")
            if a in ("hold_approve", "hold_reject"):
                return self._s(200, json.dumps({"message": decide_hold(arg, a == "hold_approve")}))
            if a == "tracker_start":
                return self._s(200, json.dumps({"message": start_trackers()}))
            if a == "tracker_command":
                command = str(arg or "").strip()
                if not command or len(command) > 500:
                    return self._s(200, json.dumps({"message": "Enter a tracker command."}))
                try:
                    r = requests.post(tracker_panel_url() + "/api/cmd",
                                      json={"text": command}, timeout=10)
                    r.raise_for_status()
                    return self._s(200, json.dumps({"message": r.json().get("reply", "")}))
                except (requests.RequestException, ValueError):
                    return self._s(200, json.dumps({
                        "message": "Local tracker control panel is offline."}))
            if a == "invite":
                want = str(arg or "").strip().upper()
                if want == "NEW":
                    want = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(6))
                elif want and not re.fullmatch(r"[A-Z0-9-]{4,24}", want):
                    return self._s(200, json.dumps({"message": "Use 4-24 letters, digits or dashes."}))
                set_setting("invite_code", want)
                log_activity("invite code", "owner", "changed" if want else "turned off")
                return self._s(200, json.dumps({"message": f"Invite code is now {want}. Anyone without it needs it to "
                                                           "use the tools." if want else "Invite code off: the site is open."}))
            if a == "tmdb_key":
                key = re.sub(r"\s+", "", str(arg or ""))[:400]
                if key and tmdb_poster("Avengers", key) is None:
                    return self._s(200, json.dumps({"message": "TMDB didn't accept that key. Check it and try again."}))
                set_setting("tmdb_key", key)
                with LOCK, db() as c:              # look again with the new source
                    c.execute("DELETE FROM posters WHERE url='' OR source='wikipedia'")
                for t in [t for t, v in POSTERS.items() if not v[0] or v[1] == "wikipedia"]:
                    POSTERS.pop(t, None)
                RELEASES.clear()
                return self._s(200, json.dumps({"message": "TMDB key saved. Posters update within a few minutes."
                                                if key else "TMDB key removed. Posters come from Wikipedia."}))
            if a == "pause":
                now = setting("paused", "0") != "1"
                set_setting("paused", "1" if now else "0")
                return self._s(200, json.dumps({"message": "polling paused" if now else "polling resumed"}))
            if a == "purge":
                return self._s(200, json.dumps({"message": f"purged {purge_stale()} expired watch(es)"}))
            if a == "killall":
                with LOCK, db() as c:
                    n = c.execute("SELECT COUNT(*) n FROM subs").fetchone()["n"]
                    c.execute("DELETE FROM subs")
                    c.execute("DELETE FROM snapshots")
                return self._s(200, json.dumps({"message": f"deleted all {n} watch(es)"}))
            if a == "drop":
                with LOCK, db() as c:
                    c.execute("DELETE FROM subs WHERE id=?", (arg,))
                return self._s(200, json.dumps({"message": "dropped"}))
            if a == "block":
                with LOCK, db() as c:
                    c.execute("INSERT OR REPLACE INTO blocked VALUES (?,?)",
                              (str(arg), datetime.now().isoformat(timespec="seconds")))
                    c.execute("DELETE FROM subs WHERE topic=?", (str(arg),))
                return self._s(200, json.dumps({"message": f"blocked {arg} and removed their watches"}))
            if a == "unblock":
                with LOCK, db() as c:
                    c.execute("DELETE FROM blocked WHERE topic=?", (str(arg),))
                return self._s(200, json.dumps({"message": f"unblocked {arg}"}))
            if a == "settings":
                set_setting("interval", max(5, int(b.get("interval") or 10)))
                set_setting("max_subs", max(1, int(b.get("max_subs") or 25)))
                if b.get("hold_limit") is not None:
                    set_setting("hold_limit", max(0, min(50, int(b.get("hold_limit") or 0))))
                return self._s(200, json.dumps({"message": "saved"}))
            if a in ("trust_add", "trust_remove"):
                name = re.sub(r"\s+", " ", str(arg or "").strip())[:40]
                if not name:
                    return self._s(200, json.dumps({"message": "enter a name"}))
                t = trusted()
                (t.add if a == "trust_add" else t.discard)(name)
                set_setting("trusted", json.dumps(sorted(t)))
                log_activity("trusted" if a == "trust_add" else "untrusted", name, "changed by the owner")
                return self._s(200, json.dumps({"message": f"{name} {'added to' if a == 'trust_add' else 'removed from'} "
                                                           "the trusted list"}))
            return self._s(200, json.dumps({"message": "unknown action"}))

        if self.path == "/api/subs/delete":
            with LOCK, db() as c:
                c.execute("DELETE FROM subs WHERE id=? AND topic=? AND owner=?",
                          (b.get("id"), (b.get("topic") or "").strip(), visitor_name(self)))
            return self._s(200, json.dumps({"ok": True}))

        if self.path == "/api/mwatch":
            topic = (b.get("topic") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", topic):
                return self._s(200, json.dumps({"error": "Enter your ntfy topic first (6+ letters/digits)."}))
            movie = next((m for m in movie_catalog() if m["code"] == b.get("movie")), None)
            if not movie:
                return self._s(200, json.dumps({"error": "Pick a movie first."}))
            date = str(b.get("date") or "")
            if date and not re.fullmatch(r"\d{8}", date):
                return self._s(200, json.dumps({"error": "Bad date."}))
            clean = lambda v, n: [str(x).strip()[:40] for x in (v or []) if str(x).strip()][:n]
            areas, formats = clean(b.get("areas"), 12), [f.upper() for f in clean(b.get("formats"), 8)]
            me = visitor_name(self)
            if not is_owner(self, topic):
                if limited(self, "watch"):
                    return self._s(200, json.dumps({"error": "That's a lot of new watches at once. Try again in a few minutes."}))
                with db() as c:
                    films = {r["code"] for r in c.execute("SELECT DISTINCT code FROM mwatch").fetchall()}
                    from_ip = c.execute("SELECT COUNT(*) n FROM mwatch WHERE ip=?", (client_ip(self),)).fetchone()["n"]
                if from_ip >= MAX_PER_IP:
                    return self._s(200, json.dumps({"error": f"There are already {MAX_PER_IP} movie watches from your "
                                                             "connection. Remove one first."}))
                if movie["code"] not in films and len(films) >= MAX_FILMS:
                    return self._s(200, json.dumps({"error": "The tracker is full right now. Try a movie someone already "
                                                             "tracks, or try again later."}))
            with LOCK, db() as c:
                n = c.execute("SELECT COUNT(*) n FROM mwatch WHERE owner=?", (me,)).fetchone()["n"]
                if n >= 10:
                    return self._s(200, json.dumps({"error": "You have 10 movie watches already. Remove one first."}))
                dup = c.execute("SELECT id FROM mwatch WHERE owner=? AND topic=? AND code=? AND date=? AND areas=? AND formats=?",
                                (me, topic, movie["code"], date, json.dumps(areas), json.dumps(formats))).fetchone()
                if dup:
                    return self._s(200, json.dumps({"error": "You're already tracking that.", "id": dup["id"]}))
                wid = c.execute("INSERT INTO mwatch (owner,topic,code,title,slug,date,areas,formats,created,state,token,ip)"
                                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                (me, topic, movie["code"], movie["title"], movie["slug"], date, json.dumps(areas),
                                 json.dumps(formats), datetime.now().isoformat(timespec="seconds"), "{}",
                                 secrets.token_urlsafe(12), client_ip(self))).lastrowid
                w = dict(c.execute("SELECT * FROM mwatch WHERE id=?", (wid,)).fetchone())
            # baseline now (in the background), so later checks only report what's new
            threading.Thread(target=mwatch_check, args=(w, {}, {}, False), daemon=True).start()
            push(topic, f"Tracking {movie['title']}",
                 f"{mwatch_describe(w)}\n\nYou'll be told when bookings open or a new matching cinema/show appears.")
            return self._s(200, json.dumps({"ok": True, "id": wid}))

        if self.path == "/api/mwatch/delete":
            with LOCK, db() as c:
                c.execute("DELETE FROM mwatch WHERE id=? AND topic=? AND owner=?",
                          (b.get("id"), (b.get("topic") or "").strip(), visitor_name(self)))
            return self._s(200, json.dumps({"ok": True}))

        if self.path == "/api/admin/mwatch-stop":
            token = str(b.get("token") or "")
            with LOCK, db() as c:
                row = c.execute("SELECT id, token, title FROM mwatch WHERE id=?", (b.get("id"),)).fetchone()
                if not row or not token or not row["token"] or not hmac.compare_digest(token, row["token"]):
                    return self._s(403, json.dumps({"error": "invalid link"}))
                topic = c.execute("SELECT topic FROM mwatch WHERE id=?", (row["id"],)).fetchone()["topic"]
                c.execute("DELETE FROM mwatch WHERE id=?", (row["id"],))
            push(topic, f"Stopped tracking {row['title']}", "You won't get more alerts for this movie.")
            return self._s(200, json.dumps({"message": f"stopped tracking {row['title']}"}))

        if self.path == "/api/admin/sub-stop":
            # "Stop watching" button in a watch's alert: the watch's own secret
            # token authorises it (no cookie on a notification tap)
            token = str(b.get("token") or "")
            with LOCK, db() as c:
                row = c.execute("SELECT id, token, venue_name FROM subs WHERE id=?", (b.get("id"),)).fetchone()
                if not row or not token or not row["token"] or not hmac.compare_digest(token, row["token"]):
                    return self._s(403, json.dumps({"error": "invalid link"}))
                topic = c.execute("SELECT topic FROM subs WHERE id=?", (row["id"],)).fetchone()["topic"]
                c.execute("DELETE FROM subs WHERE id=?", (row["id"],))
            push(topic, f"Stopped watching {row['venue_name']}", "You won't get more alerts for this watch.")
            return self._s(200, json.dumps({"message": f"stopped watching {row['venue_name']}"}))

        if self.path == "/api/venues/import":
            if not is_admin(self):
                return self._s(403, json.dumps({"error": "Only the owner can import cinemas."}))
            url = (b.get("url") or "").strip()
            if b.get("all_city"):
                city = (b.get("city") or "hyderabad").strip().lower()
                tried, total, msgs = [], 0, []
                for u in (f"https://in.bookmyshow.com/{city}/cinemas",
                          f"https://in.bookmyshow.com/explore/cinemas-{city}"):
                    tried.append(u)
                    n, m = import_venues(CLIENT, u, "HYD")
                    total += n
                    msgs.append(f"{u.split('/')[-1]}: {m}")
                    if n:
                        break
                return self._s(200, json.dumps({
                    "added": total,
                    "message": (f"added {total} cinema(s)" if total else
                                "found none. paste a movie link instead (those "
                                "pages list every cinema showing that film)")}))
            if "bookmyshow.com" not in url:
                return self._s(200, json.dumps({"error": "That isn't a BookMyShow link."}))
            m = re.search(r"/(?:cinemas|movies)/([A-Za-z]{3})/", url)
            region = m.group(1).upper() if m else "HYD"
            added, msg = import_venues(CLIENT, url, region)
            return self._s(200, json.dumps({"added": added, "message": msg}))

        return self._s(404, json.dumps({"error": "not found"}))


def main():
    global POLL_SECONDS, CLIENT
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()
    POLL_SECONDS = args.interval
    CLIENT = HttpClient({})
    init_db()
    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=mwatch_poller, daemon=True).start()
    threading.Thread(target=seat_poller, daemon=True).start()
    threading.Thread(target=group_poller, daemon=True).start()
    threading.Thread(target=movie_catalog, daemon=True).start()     # ready before the first visitor
    load_posters()
    load_releases()
    threading.Thread(target=poster_worker, daemon=True).start()
    threading.Thread(target=upcoming_worker, daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    tok = setting("admin_token")
    print(f"Seat Watch on http://{args.host}:{args.port}  (checking every {POLL_SECONDS}s)")
    print(f"ADMIN:  http://localhost:{args.port}/admin?key={tok}")
    print("        keep that link private - it has full control")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
