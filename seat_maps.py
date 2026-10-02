#!/usr/bin/env python3
"""
seat_maps.py - seat maps for the website (row picker and seat-level alerts).

BookMyShow sends each show's seat grid encrypted and only the seat page can
decrypt it, so maps come from the tracker's own Chrome (debug port, logged in):
one extra tab, used by one worker thread (Playwright is single-threaded).
After the first map the tab switches show in place (~1s instead of ~3s).

Views only. Never clicks a seat, never holds anything.
"""

import queue
import threading
import time
from concurrent.futures import Future

from pathlib import Path

from seat_layout import AVAILABLE, free_blocks, parse_layout

# id of the tab this service opened, so a restarted website can close its old tab
# (a tab left behind by a dead connection can stall every new attach to Chrome,
# the holder's included). Only this one tab is ever closed.
TAB_FILE = Path(__file__).resolve().parent / "seatmaps_tab.txt"

CACHE_SECONDS = 20          # one map serves every visitor looking at that show for this long
MAX_QUEUE = 6               # more waiting than this and new asks are turned away


def compact(layout):
    """What the browser draws: categories in screen order and each row's seats
    as [grid column, seat number, available]."""
    rows = [{"label": r["label"], "cat": r["category"].rstrip(". "), "y": r["grid_row"],
             "seats": [[s["col"], s["num"], s["status"] == AVAILABLE] for s in r["seats"]]}
            for r in layout["rows"]]
    cats = []
    for r in rows:
        if r["cat"] not in cats:
            cats.append(r["cat"])
    free = sum(s[2] for r in rows for s in r["seats"])
    total = sum(len(r["seats"]) for r in rows)
    return {"cats": cats, "rows": rows, "free": free, "total": total}


def blocks_for(layout, together, categories=(), rows=()):
    """[("GOLD", "F", "F5-F6", ["GOLD:F5", "GOLD:F6"]), ...]: free runs that fit.
    Categories compare without the trailing dot BMS puts on split blocks."""
    cats = {c.upper().rstrip(". ") for c in categories}
    want = {r.upper() for r in rows}
    out = []
    for r in layout["rows"]:
        cat = r["category"].rstrip(". ")
        if cats and cat.upper() not in cats:
            continue
        if want and r["label"].upper() not in want:
            continue
        for b in free_blocks(r, together):
            nums = [st["num"] for st in b]     # some cinemas number right to left
            name = f"{r['label']}{min(nums)}" + (f"-{r['label']}{max(nums)}" if len(b) > 1 else "")
            out.append((cat, r["label"], name,
                        [f"{cat}:{r['label']}{s['num']}" for s in b]))
    return out


class SeatMaps:
    def __init__(self, port=9222):
        self.port = port
        self.q = queue.Queue()
        self.cache = {}              # url -> (at, layout)
        self.last_error = ""
        self.last_ok = 0.0
        self._thread = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ public
    def get(self, url, max_age=CACHE_SECONDS, timeout=40):
        """Parsed layout for a seat-layout url, or None (self.last_error says why)."""
        hit = self.cache.get(url)
        if hit and time.time() - hit[0] <= max_age:
            return hit[1]
        if self.q.qsize() >= MAX_QUEUE:
            self.last_error = "busy, try again in a few seconds"
            return None
        self._start()
        fut = Future()
        self.q.put((url, max_age, fut))
        try:
            return fut.result(timeout=timeout)
        except Exception as e:
            self.last_error = str(e)[:120] or "timed out"
            return None

    def cached_at(self, url):
        hit = self.cache.get(url)
        return hit[0] if hit else None

    # ------------------------------------------------------------ worker
    def _start(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._safe_run, name="seatmaps", daemon=True)
                self._thread.start()

    def _log(self, what):
        print(f"  [seatmaps {time.strftime('%H:%M:%S')}] {what}", flush=True)

    def _safe_run(self):
        try:
            self._run()
        except Exception:
            import traceback
            self._log("worker crashed: " + traceback.format_exc())

    def _run(self):
        self._log("worker starting")
        from playwright.sync_api import sync_playwright
        from seat_decode_probe import HOOK_JS
        pw = sync_playwright().start()
        browser = page = None
        idle_since = time.time()
        while True:
            # Playwright pauses new tabs in this Chrome (the holder's too) until each
            # attached client resumes them, and a sync client only does that inside a
            # Playwright call. So while attached, wait by pumping; after 2 idle
            # minutes close the tab and detach, leaving Chrome to the holder alone.
            try:
                url, max_age, fut = self.q.get_nowait()
            except queue.Empty:
                if page is None:
                    url, max_age, fut = self.q.get()
                else:
                    try:
                        page.wait_for_timeout(50)
                    except Exception:
                        page = None
                    if page is not None and time.time() - idle_since > 120:
                        try:
                            page.close()
                            browser.close()        # over CDP this only disconnects
                        except Exception:
                            pass
                        browser = page = None
                        TAB_FILE.unlink(missing_ok=True)
                        self._log("idle: closed the seat-map tab and detached")
                    continue
            t0 = time.time()
            self._log("load " + url.split("/seat-layout/")[-1])
            hit = self.cache.get(url)          # someone else's request may have filled it
            if hit and time.time() - hit[0] <= max_age:
                fut.set_result(hit[1])
                continue
            try:
                if page is None or page.is_closed() or not browser.is_connected():
                    self._close_old_tab()
                    try:
                        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}", timeout=20000)
                    except Exception as e:
                        self._log(f"attach failed: {str(e)[:300]}")
                        raise RuntimeError("couldn't attach to the tracker's Chrome")
                    self._log("attached to Chrome")
                    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                    page = ctx.new_page()
                    try:
                        cdp = ctx.new_cdp_session(page)
                        TAB_FILE.write_text(cdp.send("Target.getTargetInfo")["targetInfo"]["targetId"])
                        cdp.detach()
                    except Exception as e:
                        self._log(f"couldn't note the tab id: {e}")
                    self._log("tab opened")
                    page.add_init_script(HOOK_JS)
                    self._log("hook added")
                    try:
                        from seat_holder import bypass_service_worker
                        bypass_service_worker(ctx, page)
                        self._log("service worker bypassed")
                    except Exception as e:
                        self._log(f"no bypass: {e}")
                text = self._load(page, url)
                if not text:
                    raise RuntimeError("the seat map didn't load")
                layout = parse_layout(text)
                if not layout["rows"]:
                    self._log(f"no rows in {len(text)} chars: {text[:300]!r}")
                    raise RuntimeError("the seat map was empty")
                self.cache[url] = (time.time(), layout)
                self.last_ok, self.last_error = time.time(), ""
                self._log(f"ok in {time.time() - t0:.2f}s ({len(layout['rows'])} rows)")
                fut.set_result(layout)
            except Exception as e:
                self.last_error = str(e).splitlines()[0][:120] if str(e) else "failed"
                self._log(f"failed after {time.time() - t0:.2f}s: {self.last_error}")
                fut.set_result(None)
                try:                               # start clean next time
                    if page is not None:
                        page.close()
                    if browser is not None:
                        browser.close()
                    TAB_FILE.unlink(missing_ok=True)
                except Exception:
                    pass
                browser = page = None
            idle_since = time.time()
            # trim old entries
            now = time.time()
            for k in [k for k, (at, _) in self.cache.items() if now - at > 300]:
                self.cache.pop(k, None)

    def _close_old_tab(self):
        import requests
        try:
            tabs = requests.get(f"http://127.0.0.1:{self.port}/json/list", timeout=3).json()
            self._log("Chrome tabs: " + "; ".join(f"{t.get('id', '')} {t.get('url', '')[:60]}"
                                                  for t in tabs if t.get("type") == "page"))
        except Exception as e:
            self._log(f"tab list failed: {e}")
            return
        try:
            old = TAB_FILE.read_text().strip()
        except OSError:
            return
        try:
            if any(t.get("id") == old for t in tabs):
                requests.get(f"http://127.0.0.1:{self.port}/json/close/{old}", timeout=3)
                self._log("closed the previous seat-map tab")
                time.sleep(0.5)
        except Exception as e:
            self._log(f"old tab check failed: {e}")
        TAB_FILE.unlink(missing_ok=True)

    @staticmethod
    def _hits(page):
        return page.evaluate("(window.__seatHook ? window.__seatHook.hits : [])"
                             ".filter(h => h.src === 'crypto.decrypt' && h.text.includes('||'))"
                             ".map(h => h.text)")

    def _load(self, page, url, wait_s=25):
        from seat_holder import PAGE_STATE_JS
        # fast path: the tab already shows a seat page, so let BMS's router move
        # to the new show and wait for the next decrypted layout
        try:
            if page.url.rstrip("/") == url.rstrip("/"):
                # the same show again (a seat alert's repeat check): reload for a fresh layout
                page.reload(wait_until="commit", timeout=15000)   # a new document: its hook starts empty
                end = time.time() + 8
                while time.time() < end:
                    page.wait_for_timeout(100)
                    hits = self._hits(page)
                    if hits:
                        return hits[-1]
                    try:
                        page.evaluate(PAGE_STATE_JS, 2)
                    except Exception:
                        pass
            elif "/seat-layout/" in page.url:
                before = len(self._hits(page))
                page.evaluate("(u) => { history.pushState({}, '', u);"
                              " dispatchEvent(new PopStateEvent('popstate', {state: {}})); }", url)
                end = time.time() + 4
                while time.time() < end:
                    page.wait_for_timeout(100)
                    hits = self._hits(page)
                    if len(hits) > before:
                        return hits[-1]
        except Exception:
            pass
        self._log("goto")
        page.goto(url, wait_until="commit", timeout=30000)
        self._log("page committed")
        end = time.time() + wait_s
        while time.time() < end:
            page.wait_for_timeout(150)
            try:
                hits = self._hits(page)
                if hits:
                    return hits[-1]
                page.evaluate(PAGE_STATE_JS, 2)      # answers the quantity popup if it gates the layout
            except Exception:
                continue                             # still navigating
        return None
