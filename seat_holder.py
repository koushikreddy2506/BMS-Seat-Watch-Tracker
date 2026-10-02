#!/usr/bin/env python3
"""
seat_holder.py — pick seats on a show and hold them up to the payment page.

The seat map is a Konva.js canvas; every seat is a Konva Group with id
"Seat-<area>-<rowId>-<disp>-<col>" and a seatObj (row, number, price, status).
We pick the best free block from that data, pan the map the way a person
would (drag), and make a real mouse click on the seat. With quantity N set,
BMS fills N adjacent seats from the clicked one. Selection is client-side;
nothing is held until "Pay" is clicked.

Stops at the payment page. Never enters payment details.

Modes:
    # one show, select only (safe: nothing is held, page is closed)
    python seat_holder.py --url "<seat-layout url>" --qty 2 --rows F,G
    # one show, actually hold (clicks Pay, leaves the payment page open)
    python seat_holder.py --url "<seat-layout url>" --qty 2 --hold
    # wait for hold requests published by the watcher (targets with auto_hold)
    python seat_holder.py --config watch_config.json --listen
    # wait for a venue/date to open, then hold whatever is available at once
    python seat_holder.py --release "<cinema buytickets url>" --qty 2 --hold --accept-terms
        [--movie "Paradise"] [--show-time "06:15 PM"] [--screen DOLBY] [--poll 2]

Config ("holder" section of watch_config.json):
    "hold_topic": "<ntfy topic the watcher publishes hold requests to>",
    "holder": {"armed": false, "qty": 2, "categories": [], "rows": [],
               "max_total": 1500, "max_holds_per_day": 2, "max_age_seconds": 120,
               "accept_terms": false, "warm_cinema_urls": [],
               "upi_pay": false, "pay_approve_minutes": 4, "pay_minutes": 8}
    upi_pay = after a hold, ask the requester (their topic + the requests topic)
    to approve paying; approve -> UPI QR sent to them to pay from their own app,
    decline / no answer in pay_approve_minutes -> booking cancelled, seats
    released. Unpaid after pay_minutes -> released. Ticket screenshot on success.
    The listener keeps a warm seat map (~1.5s faster holds) for every venue in
    the watcher's cinema_page targets, re-reading the config each minute so new
    targets are picked up; warm_cinema_urls adds venues that aren't targets.
    armed false = select seats and report what WOULD be held, never click Pay.
    accept_terms = click Accept on the venue T&C popup that follows Pay; its text
    is included in the HELD notification. Off = stop at the popup for you.
"""

import argparse
import json
import logging
import re
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from bms_seat_watch import (Chrome, SEAT_LAYOUT_PAGE, flush_outbox,  # noqa: E402
                            ntfy_auth, send_ntfy)
from seat_capture import click_text  # noqa: E402
from seat_decode_probe import HOOK_JS  # noqa: E402
from seat_layout import parse_layout  # noqa: E402

LOG = logging.getLogger("holder")
HOLD_DIR = Path("captures") / "holds"
LEDGER = Path("holds.json")

# Screen point of a seat group. Konva's cached absolute transform goes stale
# when the page pans its layers, so rebuild it: node-local transforms, then the
# live layer attrs, then the stage transform.
_PT_FN = """
function __seatPoint(st, g) {
  const r = g.findOne('Rect') || g;
  let pt = {x: r.width() / 2, y: r.height() / 2}, n = r;
  while (n && n.getType() !== 'Layer') { pt = n.getTransform().point(pt); n = n.getParent(); }
  const L = new Konva.Transform();
  L.translate(n.x(), n.y()); L.rotate(n.rotation() * Math.PI / 180);
  L.scale(n.scaleX(), n.scaleY()); L.translate(-n.offsetX(), -n.offsetY());
  pt = st.getTransform().point(L.point(pt));
  const cv = n.getCanvas()._canvas.getBoundingClientRect();
  const p = {x: cv.left + pt.x, y: cv.top + pt.y,
             left: cv.left, top: cv.top, right: cv.right, bottom: Math.min(cv.bottom, innerHeight)};
  p.vis = p.top + 40 < p.y && p.y < p.bottom - 40 && p.left + 40 < p.x && p.x < p.right - 40;
  return p;
}"""

# the minimap is a second Konva stage and may be created first, so never assume stages[0]
_STAGE = "(window.Konva ? Konva.stages : []).find(s => s.find('Group').some(g => g.attrs.seatObj))"

SEATS_JS = """() => {""" + _PT_FN + """
  const st = """ + _STAGE + """;
  if (!st) return null;
  return st.find('Group').filter(g => g.attrs.seatObj && !g.id().endsWith('-selected')).map(g => {
    const o = g.attrs.seatObj, m = g.id().split('-'), p = __seatPoint(st, g);
    return {id: g.id(), area: m[1], rowId: +m[2], col: +m[4], row: o.rowNumber,
            num: o.displaySeatNumber, areaCode: o.areaCode, price: +o.curPrice, status: o.seatStatus,
            vis: o.seatStatus === 1 && p.vis, y: p.y};
  });
}"""

SELECTED_JS = """() => { const st = """ + _STAGE + """;
  return st ? st.find('Group').filter(g => g.id().endsWith('-selected'))
                .map(g => g.id().replace(/-selected$/, '')) : []; }"""

POINT_JS = """(id) => {""" + _PT_FN + """
  const st = """ + _STAGE + """, g = st && st.findOne('#' + id);
  return g ? __seatPoint(st, g) : null;
}"""

# One look at the page per call: answers the quantity popup (reopening it from
# the header if a different qty is remembered) and says when seats are clickable.
PAGE_STATE_JS = """(q) => {
  const leaf = t => [...document.querySelectorAll('div,span,button,li,a,p')].find(el => {
      if (el.children.length || (el.innerText || '').trim() !== t) return false;
      const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; });
  const go = leaf('Select Seats');
  if (go) {
      const n = leaf(String(q));
      if (n) n.click();
      (go.closest('button') || go).click();
      window.__qtyAnswered = true;
      return 'answered';
  }
  const hdr = [...document.querySelectorAll('button,div,span')].find(el =>
      !el.children.length && /^\\d+ Tickets?$/.test((el.innerText || '').trim()));
  if (hdr && !window.__qtyAnswered && !window.__qtyReopened && parseInt(hdr.innerText) !== q) {
      window.__qtyReopened = true; (hdr.closest('button') || hdr).click(); return 'reopened';
  }
  const seats = window.Konva && Konva.stages.some(s => s.find('Group').some(g => g.attrs.seatObj));
  // BMS normally shows the popup on every load; until it has been answered a
  // click on the map could land under it
  return seats ? (window.__qtyAnswered ? 'ready' : 'seats') : 'loading';
}"""


# --------------------------------------------------------------------------
# choosing seats
# --------------------------------------------------------------------------

BEST_DEPTH = 0.2    # 0 = back row, 1 = front row (screen): best view just in front of the back rows
SIDE_WEIGHT = 0.5   # how much being off-centre in the row counts against the row's depth


def rank(seats, qty, cat_names, categories=(), rows=(), exclude=()):
    """All windows of qty adjacent free seats, best first: preferred category;
    then the best view, i.e. rows near the back of the hall (front rows by the
    screen last) and seats near the middle of the row, weighed together; then already on screen (no panning = faster)
    between blocks about equally good. rows / categories, when given, only
    allow those. Seats in exclude (ones we already hold) count as taken."""
    cats = [c.upper() for c in categories]
    want_rows = [r.upper() for r in rows]
    exclude = set(exclude)
    by_row = {}
    for s in seats:
        s["category"] = cat_names.get(s["areaCode"], s["area"])
        by_row.setdefault((s["area"], s["rowId"]), []).append(s)

    # how far each row is from the middle of its category: 0 = middle row,
    # 1 = front/back row. Bucketed, so rows about equally central tie and the
    # faster on-screen pick decides between them.
    area_rows = {}
    for area, row_id in by_row:
        area_rows.setdefault(area, []).append(row_id)
    middleness = {}
    for area, ids in area_rows.items():
        ids.sort()
        half = max(1, (len(ids) - 1) / 2)
        for i, row_id in enumerate(ids):
            middleness[(area, row_id)] = round(abs(i - (len(ids) - 1) / 2) / half / 0.2)

    # depth of each row in the whole hall from where BMS draws it (screen at the
    # bottom): 0 = back row, 1 = front row
    row_y = {k: sum(s["y"] for s in v) / len(v) for k, v in by_row.items()
             if all(isinstance(s.get("y"), (int, float)) for s in v)}
    depth = {}
    if len(row_y) == len(by_row) and len(row_y) > 1:
        lo, hi = min(row_y.values()), max(row_y.values())
        if hi > lo:
            depth = {k: (y - lo) / (hi - lo) for k, y in row_y.items()}

    found = []
    for row_key, row_seats in by_row.items():
        row_seats.sort(key=lambda s: s["col"])
        cat, label = row_seats[0]["category"].upper(), row_seats[0]["row"].upper()
        if cats and cat not in cats:
            continue
        if want_rows and label not in want_rows:
            continue
        centre = sum(s["col"] for s in row_seats) / len(row_seats)
        half_width = max(1.0, (row_seats[-1]["col"] - row_seats[0]["col"]) / 2)
        free = lambda s: s and s["status"] == 1 and s["id"] not in exclude
        run = []
        for s in row_seats + [None]:
            if free(s) and (not run or s["col"] == run[-1]["col"] + 1):
                run.append(s)
                continue
            for i in range(len(run) - qty + 1):
                win = run[i:i + qty]
                mid = (win[0]["col"] + win[-1]["col"]) / 2
                # centre of the row before "already on screen": a short pan costs
                # ~0.2s, an end-of-row block costs the seats (seen: F1-F5 picked
                # over central seats because it was on screen)
                side = abs(mid - centre) / half_width          # 0 = middle of the row, 1 = the end
                if depth:
                    view = abs(depth[row_key] - BEST_DEPTH) / (1 - BEST_DEPTH) + SIDE_WEIGHT * side
                    key = (round(view / 0.1), 0)
                else:          # no positions (old page): middle rows of the category
                    key = (middleness[row_key], round(side / 0.25))
                found.append(((cats.index(cat) if cats else 0,) + key +
                              (not win[0].get("vis"), abs(mid - centre)), win))
            run = [s] if free(s) else []
    found.sort(key=lambda f: f[0])
    return [win for _, win in found]


def choose(seats, qty, cat_names, categories=(), rows=(), exclude=()):
    wins = rank(seats, qty, cat_names, categories, rows, exclude)
    return wins[0] if wins else None


def ledger_key(url):
    """event/venue/session/date of a seat-layout url, whatever the region spelling."""
    m = re.search(r"/seat-layout/(.+?)/?$", url)
    return m.group(1).upper() if m else url


HELD_FOR_MIN = 30    # an unpaid BMS hold is gone well within this; after it a "held" record is stale


def still_held(e):
    """A ledger "held" entry that could still be a live hold on BookMyShow. Holds
    whose release was never recorded (holder restarted, tab closed by hand)
    must not block the show for ever."""
    if e.get("stage") != "held":
        return False
    try:
        at = datetime.strptime(f"{e['day']} {e['at']}", "%Y-%m-%d %H:%M:%S")
    except (KeyError, ValueError):
        return False
    return (datetime.now() - at).total_seconds() < HELD_FOR_MIN * 60


def held_seat_ids(url):
    key = ledger_key(url)
    return {i for e in load_ledger() if e.get("key") == key and still_held(e)
            for i in e.get("seat_ids", [])}


DEFAULT_FEE_RATE = 0.12      # BMS fee per ticket when a venue hasn't been seen yet
                             # (seen at ALUC: Rs 40.12 on a Rs 390 ticket, ~10.3%)


def fee_per_ticket(url, ticket_price):
    """Convenience fee per ticket at this venue: learned from the payable amount
    of the last hold there, else a conservative share of the ticket price."""
    venue = ledger_key(url).split("/")[1] if "/" in ledger_key(url) else ""
    for e in reversed(load_ledger()):
        parts = str(e.get("key", "")).split("/")
        if (len(parts) > 1 and parts[1] == venue and e.get("payable") and e.get("total")
                and e.get("qty")):
            return round((float(e["payable"]) - float(e["total"])) / int(e["qty"]), 2)
    return round(ticket_price * DEFAULT_FEE_RATE, 2)


# A friend's own holder (personal mode, "personal" in its config) runs on their PC:
# it tells the website it's alive and reports each hold result, since the website
# can't read this PC's holds.json.
PERSONAL = {}


def report_to_site(path, payload):
    if not PERSONAL.get("site") or not PERSONAL.get("key"):
        return

    def send():
        try:
            requests.post(PERSONAL["site"].rstrip("/") + path, json={"key": PERSONAL["key"], **payload}, timeout=15)
        except requests.RequestException as e:
            LOG.warning("couldn't reach the website (%s)", str(e)[:80])
    threading.Thread(target=send, daemon=True).start()


def record(url, res, seat_ids=(), who="", sub_id=None):
    """Ledger entry per attempt. sub_id ties it to a website auto-hold request,
    which is how the website learns the outcome (it reads this file, or for a
    personal holder gets it posted)."""
    ledger = load_ledger()
    ledger.append({"key": ledger_key(url), "day": f"{datetime.now():%Y-%m-%d}", "stage": res["stage"],
                   "for": who, "sub_id": sub_id, "detail": res.get("detail"),
                   "seats": res.get("seats"), "seat_ids": sorted(seat_ids), "total": res.get("total"),
                   "qty": res.get("qty"), "payable": res.get("payable"),
                   "at": f"{datetime.now():%H:%M:%S}"})
    LEDGER.write_text(json.dumps(ledger[-200:], indent=2), encoding="utf-8")
    if sub_id and PERSONAL:
        e = ledger[-1]
        report_to_site("/api/admin/holder-result", {"entry": {k: e.get(k) for k in (
            "day", "at", "stage", "sub_id", "detail", "seats", "total", "qty", "payable")}})


def seat_names(win):
    # some halls number right to left; name the block low-high either way
    win = sorted(win, key=lambda s: int(s["num"]) if str(s["num"]).isdigit() else 0)
    return f"{win[0]['row']}{win[0]['num']}" + (f"-{win[-1]['row']}{win[-1]['num']}" if len(win) > 1 else "")


# --------------------------------------------------------------------------
# page driving
# --------------------------------------------------------------------------

def bring_into_view(page, seat_id):
    """Drag the map (from the row-label strip, never on a seat) until the seat is visible."""
    for _ in range(8):
        p = page.evaluate(POINT_JS, seat_id)
        if p is None or p["vis"]:
            return p
        # one long drag: start at the edge we're pulling away from, so the whole
        # canvas height is available (short fixed drags cost ~0.3s each)
        want = (p["top"] + p["bottom"]) / 2 - p["y"]
        sy = p["bottom"] - 15 if want < 0 else p["top"] + 15
        ey = max(p["top"] + 15, min(p["bottom"] - 15, sy + want))
        sx = p["left"] + 10
        dx = max(-300, min(300, (p["left"] + p["right"]) / 2 - p["x"])) if not (
            p["left"] + 40 < p["x"] < p["right"] - 40) else 0
        page.mouse.move(sx, sy)
        page.mouse.down()
        page.mouse.move(sx + dx, ey, steps=3)
        page.mouse.up()
    return None


_hooked = set()

def bypass_service_worker(ctx, page):
    """
    The seat map isn't drawn until /api/members/v1/purchase-history answers.
    Routed through BMS's service worker that call took ~3-5s; straight to the
    network it takes ~0.2s (seats drawn at 2.4s instead of 5.2s). This is
    DevTools' "Bypass for network", for this tab only.
    """
    if getattr(page, "_sw_bypassed", False):
        return
    cdp = ctx.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Network.setBypassServiceWorker", {"bypass": True})
    page._sw_cdp = cdp            # keep the session alive with the page
    page._sw_bypassed = True


def switch_in_page(page, url, once, timeout=3.0):
    """
    Warm-page fast path: if the tab already shows a seat map, move BMS's own
    router to the new show (pushState + popstate) instead of reloading. The app
    fetches only the new layout; measured: layout requested after 0.06s, new
    seat map drawn at 0.79s. Returns seats, or None to fall back to a full load.
    """
    try:
        if "/seat-layout/" not in page.url:
            return None
        # remember the current seat nodes, so the old map is never mistaken for the new one
        old = page.evaluate("() => { const st = " + _STAGE + ";"
                            " if (!st) return null; const g = st.findOne(n => n.attrs && n.attrs.seatObj);"
                            " return g ? g._id : null; }")
        if old is None:
            return None
        page.evaluate("(u) => { history.pushState({}, '', u);"
                      " dispatchEvent(new PopStateEvent('popstate', {state: {}})); }", url)
        once("  switched warm page in place")
        end = time.time() + timeout
        want = re.search(r"/seat-layout/.+$", url).group(0)
        while time.time() < end:
            page.wait_for_timeout(40)
            fresh = page.evaluate("(old) => { const st = " + _STAGE + ";"
                                  " if (!st) return false; const g = st.findOne(n => n.attrs && n.attrs.seatObj);"
                                  " return !!g && g._id !== old; }", old)
            if fresh and want in page.url:
                seats = page.evaluate(SEATS_JS)
                if seats:
                    once("  seats drawn")
                    return seats
    except Exception as e:
        LOG.info("  in-page switch failed (%s), doing a full load", str(e)[:60])
    return None


RELOAD_AFTER = 8.0      # seconds without BMS's seat layout before the page is reloaded

# BookMyShow's error screens offer a button; pressing it is quicker than a reload
RETRY_JS = """() => {
  const el = [...document.querySelectorAll('button,a,div,span')].find(e => {
    if (e.children.length > 1) return false;
    const t = (e.innerText || '').trim().toLowerCase();
    if (!['try again', 'retry', 'reload', 'refresh', 'try again later'].includes(t)) return false;
    const r = e.getBoundingClientRect(); return r.width > 8 && r.height > 8; });
  if (!el) return false;
  (el.closest('button,a') || el).click();
  return true;
}"""


def open_seat_map(chrome, url, qty, wait_s=30, page=None, mark=lambda what: None):
    """Load the seat page (or reuse a warm tab) and return (page, seats) once seats are clickable.
    mark() gets the sub-steps, so BMS time and our time can be told apart."""
    ctx = chrome.ctx
    if id(ctx) not in _hooked:
        ctx.add_init_script(HOOK_JS)   # also gives us the decrypted category names
        _hooked.add(id(ctx))
    page = page or ctx.new_page()
    bypass_service_worker(ctx, page)
    page.bring_to_front()
    seen = set()

    def once(what):
        if what not in seen:
            seen.add(what)
            mark(what)

    got_layout = []
    page.on("request", lambda r: once("  layout requested (doTrans)") if "dotrans" in r.url.lower() else None)
    page.on("response", lambda r: (once("  layout received from BMS"), got_layout.append(1))
            if "dotrans" in r.url.lower() else None)

    # an in-place switch keeps the ticket count the tab was loaded with (BMS asks it
    # once per load): only switch when that's the count wanted, else load fresh so the
    # quantity popup is answered with it (seen: 5 wanted, warm tab at 2 -> 2 selected)
    seats = switch_in_page(page, url, once) if getattr(page, "_qty", None) == qty else None
    if seats:
        return page, seats
    end = time.time() + wait_s

    def load():
        """(Re)load the seat page. BMS at a release often errors or hangs: never
        wait on one attempt for long, the whole window is wait_s."""
        got_layout.clear()
        try:
            page.goto(url, wait_until="commit", timeout=max(1000, min(RELOAD_AFTER, end - time.time()) * 1000))
            return True
        except Exception:
            return False

    load()
    once("  page response started")
    loaded_at, reloads, retries_clicked, pressed_at = time.time(), 0, 0, 0.0
    seats_since = None
    answered = 0
    while time.time() < end:
        page.wait_for_timeout(80)
        try:
            state = page.evaluate(PAGE_STATE_JS, qty)
        except Exception:          # still navigating
            state = "loading"
        if state == "loading":
            # BMS's own "Try again" / "Retry" button: press it straight away
            try:
                if time.time() - pressed_at > 3 and page.evaluate(RETRY_JS):
                    pressed_at = time.time()
                    retries_clicked += 1
                    mark(f"  pressed BookMyShow's retry button ({retries_clicked})")
                    continue          # the reload timer keeps running: a dead button still gets a reload
            except Exception:
                pass
            # no seat layout from BMS for a while: reload instead of waiting it out
            if not got_layout and time.time() - loaded_at > RELOAD_AFTER and end - time.time() > 2:
                reloads += 1
                mark(f"  no seat layout after {RELOAD_AFTER:.0f}s, reloading ({reloads})")
                load()
                loaded_at = time.time()
            continue
        if state in ("seats", "ready"):
            once("  seats drawn")
        if state == "answered":
            once("  quantity popup answered")
            answered += 1
            if answered == 15:     # popup still up ~1.2s later: JS click didn't take
                click_text(page, "Select Seats", timeout=1000)
            continue
        if state == "seats":       # seats drawn but no popup yet: give it a moment
            seats_since = seats_since or time.time()
            if time.time() - seats_since < 1.5:
                continue
            once("  no quantity popup after 1.5s grace")
        if state in ("ready", "seats"):
            seats = page.evaluate(SEATS_JS)
            if seats:
                page._qty = qty
                return page, seats
    mark(f"  gave up: no seat map in {wait_s}s ({reloads} reload(s), {retries_clicked} retry press(es))")
    page.close()
    return None, None


def category_names(page):
    texts = page.evaluate("(window.__seatHook ? window.__seatHook.hits : [])"
                          ".filter(h => h.src === 'crypto.decrypt' && h.text.includes('||'))"
                          ".map(h => h.text)")
    if not texts:
        return {}
    return {c["code"]: c["name"] for c in parse_layout(texts[-1])["categories"].values()}


def hold(chrome, url, qty=2, categories=(), rows=(), pay=False, max_total=None,
         accept_terms=False, page=None, skip_food=True, who="", sub_id=None):
    """
    who: whose request this is (website auto-hold), kept in the ledger.
    skip_food: decline the food & drinks upsell page some venues show after Pay.
    accept_terms: click Accept on the venue's Terms & Conditions popup that
    appears after Pay (its text is returned in res["terms"]).
    Returns a dict: {ok, stage, seats, total, detail, url, screenshot}.
    stage: no_map | no_seats | click_failed | mismatch | selected | over_budget | held | pay_failed
    The page is left open only when stage == held.
    """
    t0 = time.time()
    timings = {}

    def mark(what):
        timings[what] = round(time.time() - t0, 2)
        LOG.info("  %5.2fs  %s", timings[what], what)

    page, seats = open_seat_map(chrome, url, qty, page=page, mark=mark)
    if not page:
        return {"ok": False, "stage": "no_map", "url": url,
                "detail": "seat map didn't load in 30s, even after retrying and reloading"}
    mark("seat map loaded")
    commands = []
    page.on("request", lambda r: commands.append(m_.group(1)) if "dotrans" in r.url.lower() and (
        m_ := re.search(r'name="strCommand"\s+(\w+)', r.post_data or "")) else None)
    keep_open = False
    try:
        mine = held_seat_ids(url)
        if mine:
            LOG.info("  skipping %d seat(s) already held for this show", len(mine))
        # candidates that don't overlap each other, best first
        wins, used = [], set()
        for w in rank(seats, qty, category_names(page), categories, rows, exclude=mine):
            if not used & {s["id"] for s in w}:
                wins.append(w)
                used |= {s["id"] for s in w}
            if len(wins) == 4:
                break
        if not wins:
            return {"ok": False, "stage": "no_seats", "url": url,
                    "detail": f"no {qty} adjacent free seats matching the filter"}

        picked, win, names, retried = set(), None, "", False
        for win in wins:
            names = f"{win[0]['category']} {seat_names(win)}"
            mark(f"chose {names}")
            for attempt in range(2):
                pt = bring_into_view(page, win[0]["id"])
                if not win[0].get("vis"):
                    mark("  panned seat into view")
                if not pt:
                    break
                # the quantity popup can come back once the layout arrives and
                # swallow the click; answer it first if it's up (measured: 2s lost)
                if page.evaluate(PAGE_STATE_JS, qty) == "answered":
                    mark("  quantity popup came back, answered")
                    for _ in range(15):
                        page.wait_for_timeout(40)
                        if page.evaluate(PAGE_STATE_JS, qty) in ("ready", "seats"):
                            break
                    pt = bring_into_view(page, win[0]["id"]) or pt
                page.mouse.click(pt["x"], pt["y"])
                for _ in range(8):     # BMS fills the neighbours; normally within ~0.3s
                    picked = set(page.evaluate(SELECTED_JS))
                    if len(picked) >= qty:
                        break
                    page.wait_for_timeout(50)
                if picked or retried:
                    break
                retried = True
                mark("  click selected nothing, retrying")
                # a late quantity popup may have eaten the click; answer it, then retry once
                for _ in range(20):
                    if page.evaluate(PAGE_STATE_JS, qty) == "ready":
                        break
                    page.wait_for_timeout(80)
            if picked:
                break
            mark("  those seats didn't take (just sold?), trying the next block")
        if not picked:
            return {"ok": False, "stage": "click_failed", "url": url,
                    "detail": f"none of {len(wins)} candidate blocks could be selected"}
        wanted = {s["id"] for s in win}
        if picked != wanted:
            got = [s for s in seats if s["id"] in picked]
            if len(got) == qty and not (categories or rows):
                # "whatever is available": take the block BMS actually filled
                names = f"{got[0]['category']} {seat_names(sorted(got, key=lambda s: s['col']))}"
            else:
                return {"ok": False, "stage": "mismatch", "url": url,
                        "detail": f"wanted {names}, page selected [{', '.join(s['row'] + s['num'] for s in got)}]"}

        m = None
        for _ in range(40):        # the Pay button renders just after selection
            m = re.search(r"Pay\s*₹\s*([\d,]+)", page.inner_text("body"))
            if m:
                break
            page.wait_for_timeout(50)
        total = int(m.group(1).replace(",", "")) if m else sum(s["price"] for s in win)
        mark("seats selected")
        res = {"ok": True, "stage": "selected", "seats": names, "total": total, "url": url,
               "timings": timings, "detail": f"selected {names} for Rs {total} in {time.time() - t0:.1f}s"}
        # the cap is on what you'd actually pay: tickets + BMS fees per ticket
        fee = fee_per_ticket(url, total / max(qty, 1))
        est = round(total + fee * qty, 2)
        res.update(qty=qty, est_total=est)
        if max_total and est > max_total:
            res.update(ok=False, stage="over_budget",
                       detail=f"{names}: tickets Rs {total} + ~Rs {fee * qty:.0f} fees = ~Rs {est:.0f}, "
                              f"over the Rs {max_total} cap")
            return res
        if not pay:
            return res

        # ---- the hold: this is the one step that reserves seats on BMS ----
        before = page.url
        if not click_text(page, m.group(0) if m else "Pay", timeout=3000):
            res.update(ok=False, stage="pay_failed", detail="Pay button not found")
            return res
        mark("Pay clicked")
        terms = ""
        for _ in range(400):
            page.wait_for_timeout(50)
            if page.url != before:
                break
            # the venue's Terms & Conditions popup sits between Pay and the hold
            if not terms:
                terms = page.evaluate("""() => {
                    if (!document.body.innerText.includes('Terms & Conditions')) return '';
                    for (const el of document.querySelectorAll('div')) {
                        const t = el.innerText || '';
                        if (t.startsWith('Terms & Conditions') && t.includes('Accept') && t.length < 3000)
                            return t;
                    }
                    return '';
                }""")
                if terms:
                    mark("terms popup shown")
                    if not accept_terms:
                        break
                    # direct click on the exact "Accept" button; Playwright's
                    # text click spent ~1s on actionability checks here
                    ok = page.evaluate("""() => {
                        const el = [...document.querySelectorAll('button,div,span')].find(e =>
                            !e.children.length && (e.innerText || '').trim() === 'Accept');
                        if (!el) return false;
                        (el.closest('button') || el).click(); return true; }""")
                    if not ok and not click_text(page, "Accept", timeout=2000):
                        break
                    mark("terms accepted")
        moved = page.url != before
        if moved:
            mark("payment page reached")
        # some venues (Allu) show a food & drinks upsell first; decline it so the
        # hold always ends on the order summary
        if moved and skip_food and "food-and-beverages" in page.url:
            fnb = page.url
            for _ in range(40):
                clicked = page.evaluate("""() => {
                    const el = [...document.querySelectorAll('div,span,button')].find(e =>
                        !e.children.length && (e.innerText || '').trim() === 'Skip');
                    if (!el) return false;
                    (el.closest('button') || el).click(); return true; }""")
                if clicked:
                    break
                page.wait_for_timeout(50)
            for _ in range(200):
                if page.url != fnb:
                    mark("food & drinks skipped")
                    break
                page.wait_for_timeout(50)
        took = max(timings.values())
        # the hold is done; let the payment page render and read what BMS will
        # actually charge (tickets + fees), which also teaches fee_per_ticket
        payable = None
        for _ in range(40):
            body = page.inner_text("body")
            m_pay = re.search(r"(?:Amount Payable|Order total)\s*₹\s*([\d,]+(?:\.\d+)?)", body)
            if m_pay:
                payable = float(m_pay.group(1).replace(",", ""))
                break
            page.wait_for_timeout(100)
        res["payable"] = payable
        HOLD_DIR.mkdir(parents=True, exist_ok=True)
        shot = HOLD_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.png"
        page.screenshot(path=str(shot))
        # a popup (terms, login) instead of a new page is left for a person to answer
        if moved:
            detail = f"held {names} for Rs {payable or total} in {took:.1f}s"
            if payable and max_total and payable > max_total:
                # the estimate was off: the seats are held, but say so plainly
                res["over_cap"] = True
                detail += f" (Rs {payable:.2f} with fees is over the Rs {max_total} cap)"
        elif terms and not accept_terms:
            detail = "venue Terms & Conditions popup is waiting (accept_terms is off) — answer it in Chrome"
        else:
            detail = "clicked Pay but the page did not move on (popup? see screenshot)"
        res.update(stage="held" if moved else "pay_failed", screenshot=str(shot), terms=terms,
                   payment_url=page.url, page_text=page.inner_text("body")[:600], commands=commands,
                   detail=detail)
        res["ok"] = moved
        if moved:
            record(url, res, picked, who, sub_id)   # later attempts on this show skip these seats
        keep_open = True      # either the payment page, or whatever needs a human now
        if moved:
            res["_page"] = page   # for the UPI payment flow; never serialised
        return res
    finally:
        if not keep_open:
            page.close()


# --------------------------------------------------------------------------
# listening for hold requests
# --------------------------------------------------------------------------

def load_ledger():
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def handle_request(chrome, cfg, req, page=None):
    """Returns True if the (warm) page was used up by a hold attempt."""
    h = cfg.get("holder") or {}
    armed = bool(h.get("armed", False))
    url = SEAT_LAYOUT_PAGE.format(region=req.get("region", "HYD"), event=req["event"], venue=req["venue"],
                                  session=req["session"], date=req["date"])
    session_key = ledger_key(url)
    who = str(req.get("requester") or "")
    ledger = load_ledger()
    today = f"{datetime.now():%Y-%m-%d}"
    # one hold per show per person: a friend's approved request can still hold
    # on a show you (or someone else) already have seats for. A hold that was
    # released (declined / unpaid), or older than HELD_FOR_MIN, doesn't count any more.
    last = next((e for e in reversed(ledger) if e["key"] == session_key and e.get("for", "") == who
                 and e["stage"] in ("held", "booked", "released")), None)
    if last and (last["stage"] == "booked" or still_held(last)):
        LOG.info("already held %s%s, ignoring", session_key, f" for {who}" if who else "")
        if req.get("sub_id"):
            record(url, {"stage": "already_held", "detail": "seats were already held for this show"},
                   who=who, sub_id=req["sub_id"])
        return False
    day_cap = int(h.get("max_holds_per_day", 2))       # 0 = no daily limit (each hold is still approved)
    if armed and day_cap and sum(e["day"] == today and e["stage"] == "held" for e in ledger) >= day_cap:
        LOG.warning("daily hold limit reached, not holding %s", session_key)
        if who and req.get("notify_topic"):
            send_ntfy({**cfg, "ntfy_topic": req["notify_topic"]}, "Auto-hold skipped",
                      "The owner's daily hold limit is reached, so no seats were held.", url, "high")
        if req.get("sub_id"):
            record(url, {"stage": "limit", "detail": "the owner's daily hold limit was reached"},
                   who=who, sub_id=req["sub_id"])
        return False

    # a website request carries the requester's choices; the owner's config is the
    # default, and its price cap is a ceiling nobody's request can raise
    pick = lambda k, default: req[k] if k in req and req[k] is not None else h.get(k, default)
    qty = int(pick("qty", 2))
    caps = [c for c in (h.get("max_total"), req.get("max_total")) if c]
    max_total = min(caps) if caps else None

    LOG.info("hold request%s: %s %s -> %s", f" for {who}" if who else "",
             req.get("movie"), req.get("show_time"), url)
    res = hold(chrome, url, qty, pick("categories", []), pick("rows", []),
               pay=armed, max_total=max_total, accept_terms=bool(h.get("accept_terms", False)),
               page=page, who=who, sub_id=req.get("sub_id"))
    LOG.info("  %s: %s", res["stage"], res["detail"])
    held_page = res.pop("_page", None)
    # the person who asked approves the payment and pays by UPI themselves
    upi = bool(h.get("upi_pay")) and res["stage"] == "held" and held_page is not None \
        and not res.get("over_cap")

    show = f"{req.get('movie', '')} {req.get('show_time', '')}".strip()
    for_ = f" for {who}" if who else ""
    if res["stage"] == "held":
        paid = (f"Rs {res['payable']:.2f} to pay (tickets Rs {res['total']} + fees)" if res.get("payable")
                else f"Rs {res['total']} + fees")
        warn = "\nOVER YOUR PRICE CAP once fees were added.\n" if res.get("over_cap") else ""
        next_step = ("Asking for payment approval on the requests topic (UPI QR on approve)."
                     if upi else "Payment page is open in Chrome on the PC. Complete it before the hold expires.")
        send_ntfy(cfg, f"HELD{for_}: {show}",
                  f"{res['seats']}  {paid}{warn}\n\n{next_step}\n\n{res.get('page_text', '')[:300]}"
                  + (f"\n\nTerms accepted:\n{res['terms'][:400]}" if res.get("terms") else ""), "", "urgent")
    elif res["stage"] == "selected":
        send_ntfy(cfg, f"WOULD HOLD{for_}: {show}",
                  f"{res['seats']}  Rs {res['total']}\n(holder not armed, nothing reserved)\n{url}", url, "high")
    else:
        send_ntfy(cfg, f"Hold failed{for_}: {show}", f"{res['stage']}: {res['detail']}\n{url}", url, "high")

    if upi:                            # the payment flow takes it from here
        flow = PayFlow(cfg, req, res, held_page, url)
        flow.start()
        return flow

    # tell the person who asked, on their own topic
    if who and req.get("notify_topic"):
        told = {"held": ("Seats held for you",
                         f"{show}\n{res.get('seats')}  Rs {res.get('payable') or res.get('total')}\n\n"
                         "The owner is completing the booking."),
                "selected": ("Seats found",
                             f"{show}\n{res.get('seats')}  Rs {res.get('total')}\n\n"
                             "Auto-hold is in test mode, so nothing was reserved yet.")}
        title, body = told.get(res["stage"], ("Auto-hold couldn't get seats",
                                              f"{show}\n{res['detail']}"))
        send_ntfy({**cfg, "ntfy_topic": req["notify_topic"]}, title, body, "", "high")

    if res["stage"] != "held":         # hold() records successful holds itself
        record(url, res, who=who, sub_id=req.get("sub_id"))
    return True


# --------------------------------------------------------------------------
# UPI payment by the person who asked (holder.upi_pay: true)
#
# Once seats are held on the payment page, the requester (and the owner, on the
# requests topic) is asked to approve paying for them. Approve -> the page is
# switched to "Pay by any UPI App" and the QR is sent to them to pay from their
# own UPI app. Decline or no answer -> the booking is cancelled on BookMyShow
# (seats released) and the tab closed. After payment the ticket is sent as a
# screenshot. The bot never enters card details, CVV, OTP or UPI PIN.
# --------------------------------------------------------------------------

import queue      # noqa: E402
import secrets    # noqa: E402
import sqlite3    # noqa: E402
import threading  # noqa: E402

HERE = Path(__file__).resolve().parent
PAY_PENDING = HERE / "pay_pending.json"       # codes the website may decide on
PAY_DECISIONS = HERE / "pay_decisions.json"   # written by the website's Approve/Decline buttons

CONFIRMED_URL = re.compile(r"booking-?confirm|/confirmation|order-?confirm|/booking-?success|/mybookings", re.I)
CONFIRMED_TEXT = re.compile(r"booking (?:id|confirmed|successful)|your booking is confirmed|"
                            r"booking confirmed|tickets? (?:are )?confirmed", re.I)


def clock(seconds_from_now):
    """'9:43 PM': an exact time to act by reads better than 'within 4 min'."""
    from datetime import timedelta
    return (datetime.now() + timedelta(seconds=seconds_from_now)).strftime("%I:%M %p").lstrip("0")


def _jload(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _jsave(path, data):
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def public_url():
    """The website's current public link (start_site.py keeps it in the site's DB)."""
    try:
        c = sqlite3.connect(HERE / "bms_server.db")
        row = c.execute("SELECT v FROM settings WHERE k='public_url'").fetchone()
        c.close()
        return (row[0] if row else "").rstrip("/")
    except sqlite3.Error:
        return ""


def requests_topic(cfg):
    return cfg.get("requests_topic") or (f"{cfg['ntfy_topic']}-requests" if cfg.get("ntfy_topic") else "")


def ntfy_actions(cfg, topic, title, body, actions=None, priority=5):
    msg = {"topic": topic, "title": title, "message": body, "priority": priority, "tags": ["ticket"]}
    if actions:
        msg["actions"] = actions
    try:
        requests.post(cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/") + "/", json=msg,
                      headers=ntfy_auth(cfg), timeout=15)
    except requests.RequestException as e:
        LOG.warning("ntfy send failed (%s): %s", topic, e)


def ntfy_image(cfg, topic, path, title, body):
    """Send a screenshot as an ntfy attachment (shows as an image in the app)."""
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Filename": Path(path).name,
               "Message": body.replace("\n", "\\n").encode("ascii", "ignore").decode(), "Priority": "5",
               **ntfy_auth(cfg)}
    try:
        with open(path, "rb") as f:
            requests.put(f"{cfg.get('ntfy_server', 'https://ntfy.sh').rstrip('/')}/{topic}",
                         data=f, headers=headers, timeout=60)
    except (OSError, requests.RequestException) as e:
        LOG.warning("ntfy image failed (%s): %s", topic, e)


CLICK_TEXT_JS = """(re) => {
  const rx = new RegExp(re, 'i');
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const el = [...document.querySelectorAll('button,a,[role=button],li,div,span,p')]
    .find(e => vis(e) && e.children.length < 4 && rx.test((e.innerText || '').trim()));
  if (!el) return false;
  (el.closest('button,a,[role=button],li') || el).click();
  return true;
}"""

# "Pay by any UPI App" on desktop shows a "Scan QR code" card (with an arrow);
# the QR only appears after that card is clicked. Click the clickable card
# around the text, not the text itself.
SCAN_QR_JS = """() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const t = [...document.querySelectorAll('div,span,p,b,strong,h3,h4,button,a')]
    .find(e => vis(e) && /^scan qr code$/i.test((e.innerText || '').trim()));
  if (!t) return false;
  let el = t;
  for (let i = 0; i < 6 && el.parentElement; i++) {
    const cs = getComputedStyle(el);
    if (cs.cursor === 'pointer' || el.onclick || el.getAttribute('role') === 'button' ||
        el.tagName === 'BUTTON' || el.tagName === 'A') break;
    el = el.parentElement;
  }
  el.scrollIntoView({block: 'center'});
  el.click();
  return true;
}"""

QR_JS = """() => {
  // the biggest roughly-square image/canvas/svg on the page: the UPI QR
  const c = [...document.querySelectorAll('canvas,img,svg')].map(e => [e, e.getBoundingClientRect()])
    .filter(([e, r]) => r.width >= 90 && r.height >= 90 && Math.abs(r.width - r.height) < r.width * 0.15)
    .sort((a, b) => b[1].width - a[1].width)[0];
  if (!c) return null;
  const r = c[1];
  return {x: r.left, y: r.top, w: r.width, h: r.height};
}"""


class PayFlow:
    """One held booking waiting on the requester: ask -> (approve) QR -> booked,
    or (decline / no answer) -> released. tick() is called about once a second
    from the listener's own thread (Playwright isn't thread-safe)."""

    def __init__(self, cfg, req, res, page, url):
        h = cfg.get("holder") or {}
        self.cfg, self.req, self.res, self.page, self.url = cfg, req, res, page, url
        self.who = str(req.get("requester") or "")
        self.sub_id = req.get("sub_id")
        self.show = f"{req.get('movie', '')} {req.get('show_time', '')}".strip()
        self.amount = res.get("payable") or res.get("total")
        self.code = secrets.token_urlsafe(10)
        self.ask_for = int(h.get("pay_approve_minutes", 4)) * 60
        self.pay_for = int(h.get("pay_minutes", 8)) * 60
        self.topics = list(dict.fromkeys(t for t in (req.get("notify_topic"), requests_topic(cfg)) if t))
        self.state, self.deadline, self.refreshed = "new", 0.0, False

    # -- helpers -----------------------------------------------------------
    def note(self, stage, detail):
        LOG.info("  pay #%s: %s", self.sub_id or "-", detail)
        record(self.url, {"stage": stage, "detail": detail, "seats": self.res.get("seats"),
                          "total": self.res.get("total"), "qty": self.res.get("qty"),
                          "payable": self.res.get("payable")}, who=self.who, sub_id=self.sub_id)

    def tell(self, title, body, priority=4):
        for t in self.topics:
            ntfy_actions(self.cfg, t, title, body, priority=priority)

    def shot(self, name, clip=None):
        HOLD_DIR.mkdir(parents=True, exist_ok=True)
        path = HOLD_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.png"
        try:
            self.page.screenshot(path=str(path), clip=clip, full_page=clip is None)
        except Exception:
            self.page.screenshot(path=str(path))
        return path

    # -- steps -------------------------------------------------------------
    def start(self):
        pending = _jload(PAY_PENDING)
        pending[self.code] = {"sub_id": self.sub_id, "who": self.who, "at": time.time()}
        _jsave(PAY_PENDING, pending)
        base = public_url()
        actions = [] if not base else [
            {"action": "http", "label": label, "method": "POST", "clear": True,
             "url": f"{base}/api/admin/pay-decide", "headers": {"Content-Type": "application/json"},
             "body": json.dumps({"code": self.code, "decision": d})}
            for label, d in (("Approve & pay by UPI", "approve"), ("Decline", "decline"))]
        body = (f"{self.show}\n{self.res.get('seats')}  Rs {self.amount} to pay\n"
                f"{'For ' + self.who if self.who else ''}\n\n"
                + ("Approve to get a UPI QR to pay from your own UPI app. Decline releases the seats. "
                   if actions else "No public link for buttons; the seats will be released. ")
                + f"Answer by {clock(self.ask_for)} ({self.ask_for // 60} min), or the seats are released.")
        for t in self.topics:
            ntfy_actions(self.cfg, t, "Pay for these seats?", body, actions)
        self.note("pay_ask", f"asked {', '.join(self.topics)} to approve paying Rs {self.amount}")
        self.state, self.deadline = "await_ok", time.time() + self.ask_for

    def find_qr(self):
        """Screen rectangle of the QR, looking in the page and in any embedded
        payment frame (UPI QRs are often drawn inside a gateway iframe)."""
        p = self.page
        try:
            r = p.evaluate(QR_JS)
            if r:
                return r
        except Exception:
            pass
        for f in p.frames[1:]:
            try:
                r = f.evaluate(QR_JS)
                box = f.frame_element().bounding_box() if r else None
                if r and box:
                    return {"x": box["x"] + r["x"], "y": box["y"] + r["y"], "w": r["w"], "h": r["h"]}
            except Exception:
                continue
        return None

    def go_upi(self):
        p = self.page
        if not p.evaluate(CLICK_TEXT_JS, r"^pay by any upi app$|^upi$"):
            return self.fail("couldn't find 'Pay by any UPI App' on the payment page")
        clicked = False
        for _ in range(25):                     # the "Scan QR code" card appears after ~0.2-1s
            p.wait_for_timeout(200)
            if p.evaluate(SCAN_QR_JS):
                clicked = True
                break
        qr = None
        for i in range(75):                     # up to ~15s for the QR itself
            p.wait_for_timeout(200)
            qr = self.find_qr()
            if qr:
                break
            if i in (15, 40):                   # the first click may not have taken
                clicked = p.evaluate(SCAN_QR_JS) or clicked
        p.wait_for_timeout(700)                 # let it finish drawing
        if qr:
            pad = 40
            clip = {"x": max(0, qr["x"] - pad), "y": max(0, qr["y"] - pad),
                    "width": qr["w"] + 2 * pad, "height": qr["h"] + 2 * pad}
            path = self.shot("upi-qr", clip)
            note = ""
        else:
            # couldn't pinpoint it: send what's on screen rather than give up,
            # the QR is almost certainly on it
            HOLD_DIR.mkdir(parents=True, exist_ok=True)
            path = HOLD_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-upi-screen.png"
            p.screenshot(path=str(path))
            note = "\n(Couldn't crop the QR, so this is the whole payment screen.)"
            LOG.warning("  pay #%s: QR not pinpointed (scan card clicked: %s); sent the whole screen",
                        self.sub_id or "-", clicked)
        body = (f"{self.show}\n{self.res.get('seats')}\nScan with GPay/PhonePe/Paytm/any UPI app "
                f"and pay Rs {self.amount}. Pay by {clock(self.pay_for)} ({self.pay_for // 60} min).{note}")
        for t in self.topics:
            ntfy_image(self.cfg, t, path, "Scan to pay", body)
        self.note("awaiting_payment", f"UPI QR sent; waiting for payment of Rs {self.amount}")
        self.state, self.deadline = "await_paid", time.time() + self.pay_for

    def booked(self):
        self.page.wait_for_timeout(2500)        # let the ticket render
        path = self.shot("ticket")
        body = f"{self.show}\n{self.res.get('seats')}\nBooked. Show this at the cinema."
        for t in self.topics:
            ntfy_image(self.cfg, t, path, "Your tickets", body)
        self.note("booked", f"booked {self.res.get('seats')}; ticket sent")
        self.state = "done"

    def release(self, why):
        """Cancel on BookMyShow: back out of the payment page and confirm, which
        releases the seats; then close the tab."""
        p, released = self.page, False
        try:
            p.on("dialog", lambda d: d.accept())
            start = p.url
            clicked = p.evaluate("""() => {
              const b = [...document.querySelectorAll('button,a,[role=button],svg,img,span,div')].find(e => {
                const r = e.getBoundingClientRect();
                const label = (e.getAttribute('aria-label') || e.getAttribute('alt') || '').toLowerCase();
                return r.width > 0 && r.width < 60 && r.top < 90 && r.left < 420 &&
                       (label.includes('back') || e.tagName === 'svg');
              });
              if (!b) return false;
              (b.closest('button,a,[role=button]') || b).dispatchEvent(new MouseEvent('click', {bubbles: true}));
              return true;
            }""")
            if not clicked:
                p.go_back()
            for _ in range(25):                 # confirm the "cancel this booking?" prompt
                p.wait_for_timeout(200)
                if p.evaluate(CLICK_TEXT_JS, r"^(yes|yes,? cancel|cancel (booking|transaction|payment)|leave|confirm|ok)$"):
                    p.wait_for_timeout(1500)
                    break
                if p.url != start and "order-summary" not in p.url and "payment" not in p.url:
                    break
            released = "order-summary" not in p.url and "payment" not in p.url
        except Exception as e:
            LOG.warning("  release: %s", e)
        try:
            p.close()
        except Exception:
            pass
        how = "Booking cancelled on BookMyShow" if released else \
              "Tab closed; BookMyShow releases the seats when the hold runs out"
        self.tell("Seats released", f"{self.show}\n{self.res.get('seats')}\n{why}. {how}.")
        self.note("released", f"{why}; {how.lower()}")
        self.state = "done"

    def fail(self, why):
        self.tell("Payment step needs the owner", f"{self.show}\n{self.res.get('seats')}\n{why}. "
                  "The payment page is still open on the PC.", priority=5)
        self.note("pay_failed", why)
        self.state = "done"

    def tick(self):
        """Advance; returns False once finished."""
        if self.state == "done":
            return False
        if self.page.is_closed():
            self.tell("Payment tab closed", f"{self.show}\n{self.res.get('seats')}\nThe tab was closed on the PC.")
            self.note("released", "payment tab was closed")
            self.state = "done"
            return False
        try:
            if self.state == "await_ok":
                d = _jload(PAY_DECISIONS).get(self.code)
                if d == "approve":
                    self.go_upi()
                elif d == "decline":
                    self.release("Declined")
                elif time.time() > self.deadline:
                    self.release(f"No answer within {self.ask_for // 60} min")
            elif self.state == "await_paid":
                body = self.page.inner_text("body")[:6000]
                if CONFIRMED_URL.search(self.page.url) or CONFIRMED_TEXT.search(body):
                    self.booked()
                elif re.search(r"qr (?:code )?(?:has )?expired|session (?:has )?expired", body, re.I) \
                        and not self.refreshed:
                    self.refreshed = True
                    self.go_upi()               # one fresh QR
                elif time.time() > self.deadline:
                    self.release(f"Not paid within {self.pay_for // 60} min")
        except Exception as e:
            LOG.warning("  pay #%s tick: %s", self.sub_id or "-", e)
        return self.state != "done"


def find_warm_url(http, cinema_url, skip=lambda s, d: False):
    """Seat-layout URL of any open show at this venue (its date, today or tomorrow)."""
    from datetime import timedelta
    from bms_seat_watch import CLOSED, page_date, parse_cinema_page

    m = re.search(r"/cinemas/([^/]+)/[^/]+/buytickets/([^/]+)/(\d{8})", cinema_url)
    if not m:
        return None
    region, venue, date = m.groups()
    today = datetime.now()
    for d in dict.fromkeys((date, f"{today:%Y%m%d}", f"{today + timedelta(days=1):%Y%m%d}")):
        html = http.fetch(re.sub(r"/\d{8}(?=/?$)", f"/{d}", cinema_url.split("?")[0]), as_json=False)
        if not html or page_date(html) != d:
            continue
        for s in parse_cinema_page(html):
            if (s.get("session_id") and any(v not in CLOSED for v in (s.get("categories") or {}).values())
                    and not skip(s, d)):
                return SEAT_LAYOUT_PAGE.format(region=region, event=s["event_code"], venue=venue,
                                               session=s["session_id"], date=d)
    return None


def make_warm(chrome, http, cinema_url, qty, page=None, skip=lambda s, d: False):
    """
    A tab showing a live seat map of some open show at the venue, so a hold on
    another show there switches in place (~0.85s to a clickable map) instead of
    loading cold (~2.5s). Falls back to a plain cinema page if nothing is open.
    """
    page = page or chrome.ctx.new_page()
    url_ = find_warm_url(http, cinema_url, skip)
    if url_:
        _, ok = open_seat_map(chrome, url_, qty, page=page)
        if ok:
            LOG.info("warm seat map ready (%s)", url_.split("/seat-layout/")[1])
            return page
        page = chrome.ctx.new_page()
    LOG.info("no open show to warm with; keeping a cinema page open instead")
    try:
        page.goto(cinema_url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass
    return page


MAX_WARM = 4      # warm tabs are cheap but not free; one per venue, at most this many

# The website reads this to show visitors whether auto-hold is live. Written at
# least every ~45s while listening (ntfy keepalives), so a stale file = holder down.
HEARTBEAT = Path(__file__).resolve().parent / "holder_status.json"


def heartbeat(running, **info):
    try:
        HEARTBEAT.write_text(json.dumps({"running": running, "at": time.time(), **info}), encoding="utf-8")
    except OSError:
        pass


# cinemas where the website has an approved auto-hold waiting (written by bms_server)
WARM_FILE = Path(__file__).resolve().parent / "warm_venues.json"
WARM_QTY = Path(__file__).resolve().parent / "warm_qty.json"     # seats wanted per venue (bms_server)
# local hand-off from the website on this PC: skips the ntfy round trip (~0.8s)
LOCAL_KEY = Path(__file__).resolve().parent / "holder_local.key"


def start_local_intake(lines, port):
    """Hold requests from the website on this PC, straight into the same queue as
    ntfy messages (same shape, so they're handled identically). Loopback only,
    and the website must send the key from holder_local.key."""
    import secrets as _secrets
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    if not LOCAL_KEY.exists():
        LOCAL_KEY.write_text(_secrets.token_urlsafe(24), encoding="utf-8")
    key = LOCAL_KEY.read_text(encoding="utf-8").strip()

    class Intake(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            import hmac as _hmac
            ok = (self.path == "/hold" and self.client_address[0] == "127.0.0.1"
                  and _hmac.compare_digest(self.headers.get("X-Key", ""), key))
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if ok else b""
            sent = time.time()
            if ok:
                try:
                    sent = float(json.loads(body).get("sent_at") or sent)
                except (ValueError, TypeError, AttributeError):
                    ok = False
            if ok:
                lines.put(json.dumps({"event": "message", "time": sent,
                                      "message": body.decode("utf-8"), "via": "local"}))
            self.send_response(200 if ok else 403)
            self.end_headers()

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Intake)
    except OSError as e:
        LOG.warning("local hold intake not started on port %s (%s); ntfy only", port, e)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    LOG.info("local hold intake on 127.0.0.1:%s", port)


def warm_qty(venue, default):
    """Seats the website's waiting auto-hold at this venue asks for, so its warm tab
    is loaded with that ticket count (an in-place switch can't change it)."""
    try:
        return int(json.loads(WARM_QTY.read_text(encoding="utf-8")).get(venue) or default)
    except (OSError, ValueError, AttributeError, TypeError):
        return default


def warm_sources(cfg):
    """venue -> cinema day-page url, from the website's approved auto-holds, the
    watcher's cinema_page targets and any extra holder.warm_cinema_urls.
    Seat-API targets have no day-page url, so holds for those load cold (~1.5s slower)."""
    try:
        urls = [v["url"] if isinstance(v, dict) else v
                for v in json.loads(WARM_FILE.read_text(encoding="utf-8")).values()]
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        urls = []
    urls += [t.get("url") for t in cfg.get("targets", []) if t.get("type") == "cinema_page"]
    urls += list((cfg.get("holder") or {}).get("warm_cinema_urls", []))
    out = {}
    for u in urls:
        m = re.search(r"/buytickets/([^/]+)/", u or "")
        if m and m.group(1).upper() not in out:
            out[m.group(1).upper()] = u
    return dict(list(out.items())[:MAX_WARM])


def listen(cfg_path):
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    topic = cfg.get("hold_topic")
    if not topic:
        raise SystemExit('set "hold_topic" in the config')
    server = cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    chrome = Chrome(cfg.get("cdp_port", 9222))
    from bms_seat_watch import HttpClient
    http = HttpClient(cfg)
    warm = {}                  # venue -> {"page", "at", "qty"}
    synced = [0.0]

    def sync(force=False):
        """Re-read the config (targets added from the phone, holder edits) and keep
        one warm seat page per tracked venue, refreshed every 10 minutes."""
        nonlocal cfg
        if not force and time.time() - synced[0] < 60:
            return
        synced[0] = time.time()
        try:
            cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass                              # mid-write by the watcher; keep the last good one
        qty = int((cfg.get("holder") or {}).get("qty", 2))
        want = warm_sources(cfg)
        for v in [v for v in warm if v not in want]:
            try:
                warm.pop(v)["page"].close()
            except Exception:
                pass
            LOG.info("stopped warming %s (no longer a target)", v)
        for v, u in want.items():
            w = warm.get(v)
            vq = warm_qty(v, qty)
            stale = (not w or w["page"].is_closed() or time.time() - w["at"] > 600 or w["qty"] != vq)
            if stale:
                page = w["page"] if w and not w["page"].is_closed() else None
                warm[v] = {"page": make_warm(chrome, http, u, vq, page), "at": time.time(), "qty": vq}
        if not want and not warm:
            LOG.info("no cinema_page targets to warm: holds will load their seat page cold (~1.5s slower)")

    PERSONAL.update(cfg.get("personal") or {})
    site_beat = [0.0]

    def beat():
        try:
            chrome_ok = chrome.browser.is_connected()
        except Exception:
            chrome_ok = False
        heartbeat(True, chrome=chrome_ok, armed=bool((cfg.get("holder") or {}).get("armed")),
                  warm=sorted(warm))
        if PERSONAL and time.time() - site_beat[0] > 45:
            site_beat[0] = time.time()
            report_to_site("/api/admin/holder-beat", {"chrome": chrome_ok,
                                                "armed": bool((cfg.get("holder") or {}).get("armed"))})

    sync(force=True)
    beat()
    h = cfg.get("holder") or {}
    LOG.info("listening on %s/%s  (%s; warm: %s)", server, topic,
             "ARMED: will click Pay" if h.get("armed") else "not armed: select only",
             ", ".join(warm) or "none")
    # The ntfy stream is read on its own thread and handed over through a queue, so
    # this thread (the only one allowed to touch Playwright) wakes every second:
    # payment flows waiting on someone's answer move on without blocking new holds.
    lines = queue.Queue()

    def reader():
        while True:
            try:
                with requests.get(f"{server}/{topic}/json", stream=True, timeout=(10, 90),
                                  headers=ntfy_auth(cfg)) as r:
                    r.raise_for_status()
                    for line in r.iter_lines():
                        if line:
                            lines.put(line)
            except requests.RequestException as e:
                LOG.warning("ntfy stream dropped (%s), reconnecting", str(e)[:80])
                time.sleep(3)

    threading.Thread(target=reader, daemon=True).start()
    start_local_intake(lines, int(cfg.get("holder_port", 8799)))

    def pump(ms):
        """Wait while letting Playwright handle Chrome's messages. Playwright pauses
        every new tab/navigation in this Chrome until its client resumes it, and a
        sync client only does that inside a Playwright call: a plain sleep here
        froze tabs opened by others (the website's seat maps) until the next hold."""
        for p in chrome.ctx.pages:
            try:
                if not p.is_closed():
                    p.wait_for_timeout(ms)
                    return
            except Exception:
                continue
        time.sleep(ms / 1000)

    flows, last_beat = [], 0.0
    try:
        while True:
            line, end = None, time.time() + 1
            while time.time() < end:       # up to 1s, in 50ms slices so holds still start at once
                try:
                    line = lines.get_nowait()
                    break
                except queue.Empty:
                    pump(50)
            sync()
            if time.time() - last_beat > 15:
                beat()
                last_beat = time.time()
            flows = [f for f in flows if f.tick()]
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("event") != "message":
                continue
            max_age = int((cfg.get("holder") or {}).get("max_age_seconds", 120))
            if time.time() - float(msg.get("time", 0)) > max_age:
                LOG.info("stale hold request ignored")
                continue
            try:
                req = json.loads(msg.get("message", ""))
                req["event"], req["venue"], req["session"], req["date"]
            except (json.JSONDecodeError, KeyError, TypeError):
                LOG.warning("unreadable hold request: %s", msg.get("message", "")[:120])
                continue
            LOG.info("hold request received %.2fs after it was sent (%s)",
                     time.time() - float(msg.get("time", 0)), msg.get("via") or "ntfy")
            venue = str(req["venue"]).upper()
            w = warm.pop(venue, None)
            page = w["page"] if w and not w["page"].is_closed() else None
            LOG.info("  %s seat page for %s", "warm" if page else "cold", venue)
            try:
                used = handle_request(chrome, cfg, req, page=page)
            except Exception as e:
                LOG.exception("hold attempt crashed: %s", e)
                used = True
            if isinstance(used, PayFlow):
                flows.append(used)
            if w and not used:
                warm[venue] = w
            if used:           # that tab now shows a payment page (or was closed)
                sync(force=True)
    finally:
        heartbeat(False)
        chrome.close()


def watch_release(cfg, cinema_url, qty, categories=(), rows=(), pay=False, max_total=None,
                  accept_terms=False, movie="", show_time="", screen="", poll=2.0):
    """
    Poll a venue day page until a matching show opens for booking, then hold
    seats on it straight away. No seat preferences needed: with no categories
    or rows given it takes whatever adjacent block is on screen first.

    The day page is served uncached (cf DYNAMIC), unlike the seat-layout API
    whose answers were seen up to ~2 minutes stale, so it's the fastest signal.
    """
    import random
    from bms_seat_watch import CLOSED, HttpClient, page_date, parse_cinema_page

    m = re.search(r"/cinemas/([^/]+)/[^/]+/buytickets/([^/]+)/(\d{8})", cinema_url)
    if not m:
        raise SystemExit("need a cinema buytickets url: .../cinemas/<region>/<slug>/buytickets/<VENUE>/<YYYYMMDD>")
    region, venue, date = m.groups()
    http = HttpClient(cfg)
    chrome = Chrome(cfg.get("cdp_port", 9222))

    def rewarm(page=None):
        # never warm with a show we're waiting for: switching to the same URL
        # wouldn't refetch anything
        return make_warm(chrome, http, cinema_url, qty, page,
                         skip=lambda s, d: d == date and wanted(s))

    def wanted(s):
        return ((not movie or movie.upper() in str(s.get("movie") or "").upper())
                and (not show_time or show_time.upper() == str(s.get("show_time") or "").upper())
                and (not screen or screen.upper() in " ".join(
                    str(s.get(k) or "") for k in ("screen", "attributes")).upper()))

    def is_open(s):
        cats = s.get("categories") or {}
        return any(v not in CLOSED for v in cats.values()) if cats else s.get("avail") not in CLOSED

    LOG.info("waiting for %s %s to open (movie=%r show=%r screen=%r) every ~%.1fs — %s",
             venue, date, movie or "any", show_time or "any", screen or "any", poll,
             "WILL HOLD" if pay else "select only")
    failures, polls, t_start = {}, 0, time.time()
    warm = rewarm()
    warmed_at = time.time()
    try:
        while True:
            polls += 1
            if time.time() - warmed_at > 600:     # keep the warm map fresh (session, scripts)
                warm = rewarm(warm)
                warmed_at = time.time()
            t_fetch = time.time()
            html = http.fetch(cinema_url, as_json=False)
            fetch_ms = int((time.time() - t_fetch) * 1000)
            served = page_date(html) if html else ""
            shows = parse_cinema_page(html) if html and (not served or served == date) else []
            ready = [s for s in shows if wanted(s) and is_open(s) and s.get("session_id")
                     and failures.get(s["session_id"], 0) < 3]
            if polls % 30 == 1 or ready:
                LOG.info("poll %d: %d show(s) listed for %s, %d open & matching (fetch %dms%s)",
                         polls, len(shows), date, len(ready), fetch_ms,
                         f", site served {served}" if served and served != date else "")
            for s in ready:
                url = SEAT_LAYOUT_PAGE.format(region=region, event=s["event_code"], venue=venue,
                                              session=s["session_id"], date=date)
                title = f"{s.get('movie')} {s.get('show_time')}"
                LOG.info("OPEN: %s  -> holding", title)
                send_ntfy(cfg, f"RELEASED: {title}", f"{venue} {date} is open, grabbing seats\n{url}", url, "high")
                res = hold(chrome, url, qty, categories, rows, pay=pay, max_total=max_total,
                           accept_terms=accept_terms, page=warm)
                LOG.info("  %s: %s", res["stage"], res["detail"])
                if res["stage"] in ("held", "selected"):
                    send_ntfy(cfg, f"{'HELD' if res['stage'] == 'held' else 'WOULD HOLD'}: {title}",
                              f"{res['seats']}  Rs {res['total']}\n"
                              + ("Payment page is open in Chrome on the PC." if res["stage"] == "held"
                                 else "(select only, nothing reserved)")
                              + f"\nwaited {time.time() - t_start:.0f}s for release, "
                                f"hold took {max(res['timings'].values())}s",
                              url, "urgent")
                    return res
                if res["stage"] == "pay_failed":   # Pay was clicked; a person has to take it from here
                    send_ntfy(cfg, f"NEEDS YOU: {title}", f"{res['detail']}\nChrome on the PC.", url, "urgent")
                    return res
                failures[s["session_id"]] = failures.get(s["session_id"], 0) + 1
                send_ntfy(cfg, f"Hold failed: {title}", f"{res['stage']}: {res['detail']}\n{url}", url, "high")
                warm = rewarm()                # hold() closed the failed tab
                warmed_at = time.time()
            time.sleep(max(0.3, poll + random.uniform(-0.3, 0.3) - (time.time() - t_fetch)))
    finally:
        chrome.close()
        http.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="watch_config.json")
    ap.add_argument("--url", help="seat-layout url for a one-off attempt")
    ap.add_argument("--qty", type=int, default=2)
    ap.add_argument("--rows", default="", help="comma list, in order of preference")
    ap.add_argument("--categories", default="", help="comma list, in order of preference")
    ap.add_argument("--max-total", type=int, default=None)
    ap.add_argument("--hold", action="store_true", help="actually click Pay (reserves seats)")
    ap.add_argument("--accept-terms", action="store_true",
                    help="click Accept on the venue's Terms & Conditions popup after Pay")
    ap.add_argument("--listen", action="store_true")
    ap.add_argument("--release", metavar="CINEMA_URL",
                    help="wait for this venue/date to open, then hold at once")
    ap.add_argument("--movie", default="", help="release mode: only this movie (substring)")
    ap.add_argument("--show-time", default="", help='release mode: only this show, e.g. "06:15 PM"')
    ap.add_argument("--screen", default="", help="release mode: screen/format substring, e.g. DOLBY")
    ap.add_argument("--poll", type=float, default=2.0, help="release mode: seconds between checks")
    args = ap.parse_args()
    # log to holder.log as well as the window, so a stalled start can be diagnosed
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(HERE / "holder.log", encoding="utf-8")])
    LOG.info("holder starting (pid %s): %s", os.getpid(), " ".join(sys.argv[1:]))
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    split = lambda s: [x.strip() for x in s.split(",") if x.strip()]

    if args.listen:
        listen(args.config)
        return 0
    if args.release:
        res = watch_release(cfg, args.release, args.qty, split(args.categories), split(args.rows),
                            pay=args.hold, max_total=args.max_total, accept_terms=args.accept_terms,
                            movie=args.movie, show_time=args.show_time, screen=args.screen, poll=args.poll)
        print(json.dumps({k: v for k, v in (res or {}).items() if k != "_page"}, indent=2, ensure_ascii=False))
        flush_outbox()
        return 0 if res and res["ok"] else 1
    if not args.url:
        ap.error("give --url, --listen or --release")
    chrome = Chrome(cfg.get("cdp_port", 9222))
    try:
        res = hold(chrome, args.url, args.qty, split(args.categories), split(args.rows),
                   pay=args.hold, max_total=args.max_total, accept_terms=args.accept_terms)
    finally:
        chrome.close()
    print(json.dumps({k: v for k, v in (res or {}).items() if k != "_page"}, indent=2, ensure_ascii=False))
    flush_outbox()
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
