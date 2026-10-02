#!/usr/bin/env python3
"""
seat_decode_probe.py — find the per-seat data AFTER the page decodes it.

What we know from earlier captures:
  * the seat page calls  services-in.bookmyshow.com/doTrans.aspx
  * its reply carries  BookMyShow.strData  — ~13 KB of encrypted bytes
    (entropy 7.99, length a multiple of 16), not readable as-is
  * a blob: worker with an inflate routine is loaded next to it

Rather than reverse-engineer the encryption, this lets the page decode the
seats as normal and records the decoded result, in your own logged-in Chrome.
An init script watches where decoded data surfaces:
  crypto.subtle.decrypt, Worker messages, TextDecoder.decode, JSON.parse
and keeps anything that looks like a seat grid. It also records the
doTrans request body (which command/params fetch the layout).

Stops at the seat map. Books nothing.

Usage:
    python seat_decode_probe.py --url "<seat-layout url>"
    python seat_decode_probe.py --cinema "<cinema buytickets url>" --show 0
    python seat_decode_probe.py --cinema "<cinema buytickets url>" --list
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bms_seat_watch import Chrome, parse_cinema_page  # noqa: E402
from seat_capture import click_text, live_pages  # noqa: E402

DUMP = Path("captures") / "seat_decode"

SEAT_PAGE = "https://in.bookmyshow.com/movies/{region}/seat-layout/{event}/{venue}/{session}/{date}"

HOOK_JS = r"""
(() => {
  if (window.__seatHook) return;
  const H = window.__seatHook = {hits: [], reqs: [], errors: 0};
  // row letter + seat number tokens: A1, A-1, A:1, AA12 ...
  const TOK = /(?:^|[^A-Za-z0-9])[A-Z]{1,2}[-:_]?\d{1,3}(?=[^0-9]|$)/g;
  const score = s => { const m = s.slice(0, 400000).match(TOK); return m ? m.length : 0; };
  const asText = v => {
    if (v == null) return '';
    if (typeof v === 'string') return v;
    if (v instanceof ArrayBuffer) return new TextDecoder().decode(v);
    if (ArrayBuffer.isView(v)) return new TextDecoder().decode(v);
    return JSON.stringify(v);
  };
  const keep = (src, v, always) => {
    try {
      const s = asText(v);
      if (!s || s.length < 200) return;
      const sc = score(s);
      if (!always && sc < 30) return;
      H.hits.push({src, score: sc, len: s.length, text: s.slice(0, 2000000)});
      if (H.hits.length > 40) { H.hits.sort((a, b) => b.score - a.score); H.hits.length = 30; }
    } catch (e) { H.errors++; }
  };

  // 1. WebCrypto decryption output
  try {
    const sub = crypto.subtle, dec = sub.decrypt.bind(sub);
    sub.decrypt = async (...a) => { const r = await dec(...a); keep('crypto.decrypt', r, true); return r; };
  } catch (e) {}

  // 2. messages coming back from workers (the inflate worker)
  try {
    const W = window.Worker;
    window.Worker = function (...a) {
      const w = new W(...a);
      w.addEventListener('message', ev => keep('worker:' + String(a[0]).slice(0, 60), ev.data, true));
      return w;
    };
    window.Worker.prototype = W.prototype;
  } catch (e) {}

  // 3. bytes -> text
  try {
    const td = TextDecoder.prototype.decode;
    TextDecoder.prototype.decode = function (...a) {
      const r = td.apply(this, a);
      if (r && r.length > 2000) keep('TextDecoder', r, false);
      return r;
    };
  } catch (e) {}

  // 4. text -> objects
  try {
    const jp = JSON.parse;
    JSON.parse = function (s, ...rest) {
      if (typeof s === 'string' && s.length > 2000) keep('JSON.parse', s, false);
      return jp.call(this, s, ...rest);
    };
  } catch (e) {}
})();
"""


def build_seat_url(cinema_url: str, show: dict) -> str:
    m = re.search(r"/cinemas/([^/]+)/[^/]+/buytickets/([^/]+)/(\d{8})", cinema_url)
    if not m:
        raise SystemExit("can't read region/venue/date from the cinema url")
    region, venue, date = m.groups()
    return SEAT_PAGE.format(region=region, event=show["event_code"], venue=venue,
                            session=show["session_id"], date=date)


def pick_show(chrome: Chrome, cinema_url: str, which: int, list_only: bool):
    html = chrome.fetch(cinema_url, as_json=False) or ""
    shows = parse_cinema_page(html)
    if not shows:
        raise SystemExit("no shows parsed from that cinema page")
    for i, s in enumerate(shows):
        print(f"  [{i}] {s['show_time']:<9} sess={s['session_id']:<6} avail={s['avail']} "
              f"{s['movie']} | {s['screen']}")
    if list_only:
        return None
    return shows[min(which, len(shows) - 1)]


def redact(headers: dict) -> dict:
    return {k: ("<redacted>" if k.lower() in ("cookie", "authorization") else v)
            for k, v in headers.items()}


def run(chrome: Chrome, url: str, qty: int, wait_s: int):
    ctx = chrome.ctx
    ctx.add_init_script(HOOK_JS)
    dotrans = []

    def on_request(req):
        if "dotrans" in req.url.lower():
            dotrans.append({"url": req.url, "method": req.method,
                            "headers": redact(req.headers), "post": req.post_data})
            print(f"   doTrans request: {(req.post_data or '')[:140]}")

    page = ctx.new_page()
    page.on("request", on_request)
    print(f"-> {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    # quantity popup, if shown
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
    page.wait_for_timeout(800)
    for p in live_pages(ctx):
        if click_text(p, "Select Seats", timeout=2000):
            print("-> clicked 'Select Seats'")
            break

    print(f"-> waiting up to {wait_s}s for decoded seat data")
    hook = {"hits": []}
    for sec in range(wait_s):
        page.wait_for_timeout(1000)
        try:
            hook = page.evaluate("window.__seatHook || {hits: []}")
        except Exception:
            continue
        best = max((h["score"] for h in hook["hits"]), default=0)
        if best >= 50 and sec >= 5:
            break

    try:
        body = (page.inner_text("body") or "")[:400]
    except Exception:
        body = ""
    page.close()

    DUMP.mkdir(parents=True, exist_ok=True)
    hits = sorted(hook.get("hits", []), key=lambda h: -h["score"])
    index = []
    for i, h in enumerate(hits):
        name = f"hit_{i:02d}.txt"
        text = h["text"]
        try:
            text = json.dumps(json.loads(text), indent=2)
        except (json.JSONDecodeError, TypeError):
            pass
        (DUMP / name).write_text(text, encoding="utf-8")
        index.append({"file": name, "src": h["src"], "score": h["score"], "len": h["len"]})
    (DUMP / "index.json").write_text(json.dumps(
        {"url": url, "page_text": body, "hits": index, "doTrans": dotrans}, indent=2), encoding="utf-8")

    print("\n" + "=" * 74)
    print(f"{len(hits)} decoded candidate(s), {len(dotrans)} doTrans request(s) -> {DUMP.resolve()}")
    print("=" * 74)
    for e, h in zip(index[:6], hits[:6]):
        print(f"\n  {e['file']}  src={e['src']}  seat-tokens={e['score']}  {e['len']:,} chars")
        print("    " + h["text"][:300].replace("\n", " "))
    if not hits:
        print(f"\nnothing decoded. page said: {body[:200]!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="seat-layout url")
    ap.add_argument("--cinema", help="cinema buytickets url (picks a show from it)")
    ap.add_argument("--show", type=int, default=0)
    ap.add_argument("--list", action="store_true", help="just list the cinema's shows")
    ap.add_argument("--qty", type=int, default=2)
    ap.add_argument("--wait", type=int, default=30)
    ap.add_argument("--port", type=int, default=9222)
    args = ap.parse_args()
    if not (args.url or args.cinema):
        ap.error("give --url or --cinema")

    chrome = Chrome(args.port)
    try:
        url = args.url
        if not url:
            show = pick_show(chrome, args.cinema, args.show, args.list)
            if show is None:
                return 0
            url = build_seat_url(args.cinema, show)
        run(chrome, url, args.qty, args.wait)
    finally:
        chrome.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
