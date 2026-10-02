#!/usr/bin/env python3
"""
seat_capture.py — capture the network traffic of a seat page that ACTUALLY renders.

The grid is drawn on a <canvas>, so seats are not DOM elements and cannot be
read from the page. But the browser has to receive the seat data from
somewhere in order to draw it.

Every previous capture was taken on a page that never rendered, so the seat
request was never made. This runs the click-through flow that we know works,
with a network listener attached the whole time, and scores every first-party
response for seat-shaped content.

Stops at the seat map. Books nothing.

Usage:
    python seat_capture.py --url "<cinema buytickets url>"
    python seat_capture.py --url "<cinema url>" --show 1 --qty 2
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bms_seat_watch import Chrome  # noqa: E402

DUMP = Path("captures") / "seat_capture"

JUNK = ("googletagmanager", "google-analytics", "doubleclick", "facebook",
        "branch.io", "clevertap", "cloudfront", "youtube", "googleads",
        "app.link", "bmscdn.com", "gstatic", "googleapis", "/cdn-cgi/")

FIND_TIMES = """() => {
    const re = /^\\s*\\d{1,2}:\\d{2}\\s*(AM|PM)\\s*$/i;
    const out = []; let idx = 0;
    for (const el of document.querySelectorAll('div,span,a,button,li')) {
        if (el.children.length > 2) continue;
        const t = (el.innerText || '').trim();
        if (!re.test(t)) continue;
        if (out.some(o => o.time === t)) continue;
        el.setAttribute('data-probe-idx', String(idx));
        out.push({idx: idx, time: t});
        idx++;
    }
    return out;
}"""

# seat data should mention rows, seat numbers and status codes
SIGNALS = [
    (re.compile(r'"[A-Q]-?\d{1,2}"'), 4),          # seat ids like "A1", "Q-12"
    (re.compile(r'\b(?:seatNo|seatId|seatLabel)\b', re.I), 5),
    (re.compile(r'\b(?:rowId|rowLabel|rowName|strRow)\b', re.I), 5),
    (re.compile(r'\bUPPER BALCONY|LOWER BALCONY|FIRST CLASS|SECOND CLASS\b', re.I), 3),
    (re.compile(r'"(?:status|seatStatus)"\s*:\s*"?[0-9]"?'), 3),
    (re.compile(r'\bavail|blocked|sold\b', re.I), 1),
]


def score(text: str):
    total, hits = 0, {}
    for pat, weight in SIGNALS:
        n = len(pat.findall(text[:300000]))
        if n:
            hits[pat.pattern[:28]] = n
            total += min(n, 300) * weight
    return total, hits


def is_junk(u: str) -> bool:
    low = u.lower()
    if any(d in low for d in JUNK):
        return True
    return bool(re.search(r"\.(js|css|png|jpe?g|webp|svg|woff2?|ico|gif)(\?|$)", low))


def click_text(page, label, timeout=4000):
    for sel in (f"text={label}", f"button:has-text('{label}')", f"//*[normalize-space()='{label}']"):
        try:
            page.click(sel, timeout=timeout)
            return True
        except Exception:
            continue
    return False


def live_pages(ctx):
    out = []
    for p in list(ctx.pages):
        try:
            p.evaluate("1")
            out.append(p)
        except Exception:
            continue
    return out


def run(chrome: Chrome, url: str, which: int, qty: int, wait_s: int):
    ctx = chrome.ctx
    captured, seen = [], set()

    def on_response(resp):
        u = resp.url
        if is_junk(u) or u in seen:
            return
        try:
            if resp.status >= 400:
                return
            body = resp.text()
        except Exception:
            return
        if len(body) < 40:
            return
        seen.add(u)
        sc, hits = score(body)
        captured.append({"url": u, "body": body, "score": sc, "hits": hits})
        mark = f"   <<<<< SEATS score={sc}" if sc > 60 else ""
        print(f"   {len(body):>8,}b  {u[:78]}{mark}")

    def hook(p):
        try:
            p.on("response", on_response)
        except Exception:
            pass

    for p in ctx.pages:
        hook(p)
    ctx.on("page", hook)

    page = ctx.new_page()
    hook(page)

    print(f"-> {url}\n\nnetwork traffic:")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)

    direct_seat_url = "/seat-layout/" in url.lower()
    if direct_seat_url:
        print("\n-> direct seat-layout session")
        page.wait_for_timeout(3000)
    else:
        times = []
        for _ in range(30):
            page.wait_for_timeout(1000)
            try:
                times = page.evaluate(FIND_TIMES) or []
            except Exception:
                continue
            if times:
                break
        if not times:
            print("   no showtimes found")
            return
        idx = min(which, len(times) - 1)
        print(f"\n-> click {times[idx]['time']}")
        page.click(f'[data-probe-idx="{idx}"]', timeout=15000)
        page.wait_for_timeout(3000)

    for p in live_pages(ctx):
        try:
            if p.evaluate("""(q) => {
                for (const el of document.querySelectorAll('div,span,button,li,a')) {
                    if (el.children.length > 0) continue;
                    if ((el.innerText||'').trim() === String(q)) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 8 && r.height > 8) { el.click(); return true; }
                    }
                }
                return false;
            }""", qty):
                print(f"-> selected qty {qty}")
                break
        except Exception:
            continue
    page.wait_for_timeout(1000)
    for p in live_pages(ctx):
        if click_text(p, "Select Seats"):
            print("-> clicked 'Select Seats'")
            break

    print(f"\n-> letting the grid load and settle ({wait_s}s)")
    for _ in range(wait_s):
        try:
            page.wait_for_timeout(1000)
        except Exception:
            import time as _t
            _t.sleep(1)

    # ---- write out ----
    DUMP.mkdir(parents=True, exist_ok=True)
    index = []
    for i, r in enumerate(captured):
        name = f"{i:03d}{'_SEATS' if r['score'] > 60 else ''}.json"
        body = r["body"]
        try:
            body = json.dumps(json.loads(body), indent=2)
        except json.JSONDecodeError:
            pass
        (DUMP / name).write_text(body[:3_000_000], encoding="utf-8")
        index.append({"file": name, "url": r["url"], "score": r["score"],
                      "bytes": len(r["body"]), "hits": r["hits"]})
    (DUMP / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")

    print("\n" + "=" * 74)
    print(f"{len(captured)} first-party response(s) -> {DUMP.resolve()}")
    print("=" * 74)
    hot = sorted([e for e in index if e["score"] > 0], key=lambda x: -x["score"])
    if hot:
        print("\nranked by seat-likeness:\n")
        for e in hot[:8]:
            print(f"  {e['file']:<22} score={e['score']:<6} {e['bytes']:>9,}b")
            print(f"      {e['url'][:96]}")
            print(f"      {e['hits']}")
        if hot[0]["score"] > 60:
            print(f"\nVERDICT: {hot[0]['file']} looks like real seat data — send me that file")
        else:
            print("\nVERDICT: nothing strongly seat-shaped; send index.json and the top file")
    else:
        print("\nnothing scored. send index.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--show", type=int, default=0)
    ap.add_argument("--qty", type=int, default=2)
    ap.add_argument("--wait", type=int, default=20)
    ap.add_argument("--port", type=int, default=9222)
    args = ap.parse_args()

    chrome = Chrome(args.port)
    try:
        run(chrome, args.url, args.show, args.qty, args.wait)
    finally:
        chrome.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
