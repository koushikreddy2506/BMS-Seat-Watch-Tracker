#!/usr/bin/env python3
"""
seat_layout.py — per-seat view of a show.

The seat page gets its grid from doTrans.aspx (strCommand=GETSEATLAYOUT) as an
encrypted strData blob and decrypts it with crypto.subtle. We let the page do
that in your logged-in Chrome and read the decrypted text (see
seat_decode_probe.py for how this was found).

Decrypted format:
    <categories> || <row>|<row>|...
    category :  NAME:areaLetter:areaCode:?:N:0          (joined by '|')
    row      :  gridRow:ROWLABEL:seat:seat:...
    seat     :  <area><status><col>+<num>   e.g. D205+05
                 area   A/B/C/D  -> category letter
                 status 1 = available, 2 = sold/unavailable
                 col    grid column (gaps have num 00)
                 num    printed seat number

Stops at the seat map. Books nothing.

Usage:
    python seat_layout.py --file captures/seat_decode/hit_01.txt
    python seat_layout.py --url "<seat-layout url>" [--together 2]
"""

import argparse
import re
import sys
from pathlib import Path

SEAT_RE = re.compile(r"([A-Z])(\d)(\d+)\+(\d+)")
AVAILABLE = "1"


def parse_layout(text: str) -> dict:
    head, _, body = text.partition("||")
    cats = {}
    for c in head.split("|"):
        f = c.split(":")
        if len(f) >= 3:
            cats[f[1]] = {"name": f[0], "code": f[2], "raw": f[3:]}

    rows = []
    for r in body.split("|"):
        f = r.split(":")
        if len(f) < 3:
            continue
        toks = []
        for tok in f[2:]:
            m = SEAT_RE.fullmatch(tok)
            if not m or int(m.group(4)) == 0:
                continue  # aisle / gap
            toks.append(m.groups())
        if not toks:
            continue
        # Two encodings seen: D205+05 (column "05") and, at AMB, A2011+1 / A20110+10
        # (row "01" then column "1" / "10"). A shared 2-digit prefix on every seat of
        # the row, with more digits after it, is that row number.
        grid_row = int(f[0]) if f[0].isdigit() else 0
        pre = {c[:2] for _, _, c, _ in toks}
        strip = len(pre) == 1 and all(len(c) > 2 for _, _, c, _ in toks)
        if strip:
            grid_row = int(next(iter(pre)))
        seats = [{"area": a, "status": st, "col": int(c[2:] if strip else c), "num": int(n)}
                 for a, st, c, n in toks]
        area = seats[0]["area"]
        rows.append({"grid_row": grid_row, "label": f[1], "area": area,
                     "category": cats.get(area, {}).get("name", area), "seats": seats})
    return {"categories": cats, "rows": rows}


def free_blocks(row: dict, n: int):
    """Runs of >= n available seats in adjacent grid columns (an aisle breaks a run)."""
    out, run = [], []
    for s in row["seats"]:
        if s["status"] == AVAILABLE and (not run or s["col"] == run[-1]["col"] + 1):
            run.append(s)
            continue
        if len(run) >= n:
            out.append(run)
        run = [s] if s["status"] == AVAILABLE else []
    if len(run) >= n:
        out.append(run)
    return out


def matching_blocks(layout: dict, together: int = 2, categories=(), rows=()):
    """[(category, row_label, [seat, ...]), ...] for free runs that pass the filter.
    categories / rows are case-insensitive; empty means any."""
    cats = {c.upper() for c in categories}
    want_rows = {r.upper() for r in rows}
    out = []
    for r in layout["rows"]:
        if cats and r["category"].upper() not in cats:
            continue
        if want_rows and r["label"].upper() not in want_rows:
            continue
        for b in free_blocks(r, together):
            out.append((r["category"], r["label"], b))
    return out


def block_name(row_label: str, block: list) -> str:
    if len(block) == 1:
        return f"{row_label}{block[0]['num']}"
    return f"{row_label}{block[0]['num']}-{row_label}{block[-1]['num']}"


def summary(layout: dict, together: int = 2) -> dict:
    """{category: {available, total, rows: {label: [ 'F1-F9', ... ]}}}"""
    res = {}
    for r in layout["rows"]:
        c = res.setdefault(r["category"], {"available": 0, "total": 0, "rows": {}})
        c["total"] += len(r["seats"])
        c["available"] += sum(s["status"] == AVAILABLE for s in r["seats"])
        blocks = free_blocks(r, together)
        if blocks:
            c["rows"][r["label"]] = [block_name(r["label"], b) for b in blocks]
    return res


_hooked = set()   # contexts that already carry the init script


def live_layout(chrome, url: str, wait_s: int = 30, qty: int = 2):
    """Open the seat page in the attached Chrome and return the decrypted layout text."""
    from seat_decode_probe import HOOK_JS
    from seat_capture import click_text

    ctx = chrome.ctx
    if id(ctx) not in _hooked:
        ctx.add_init_script(HOOK_JS)
        _hooked.add(id(ctx))
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        clicked = False
        for _ in range(wait_s * 2):
            page.wait_for_timeout(500)
            hits = page.evaluate(
                "(window.__seatHook ? window.__seatHook.hits : [])"
                ".filter(h => h.src === 'crypto.decrypt' && h.text.includes('||'))"
                ".map(h => h.text)")
            if hits:
                return hits[-1]
            if not clicked:  # quantity popup can gate the layout request
                try:
                    page.evaluate("""(q) => {
                        for (const el of document.querySelectorAll('div,span,button,li,a')) {
                            if (el.children.length === 0 && (el.innerText||'').trim() === String(q)) {
                                const r = el.getBoundingClientRect();
                                if (r.width > 8 && r.height > 8) { el.click(); return; }
                            }
                        }}""", qty)
                    clicked = click_text(page, "Select Seats", timeout=500)
                except Exception:
                    pass
        return None
    finally:
        page.close()


def print_summary(layout: dict, together: int):
    for cat, c in summary(layout, together).items():
        print(f"{cat:<16} {c['available']:>4} / {c['total']:<4} available")
        for label, blocks in c["rows"].items():
            print(f"    row {label:<3} {together}+ together: {', '.join(blocks)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="decrypted layout text (from seat_decode_probe)")
    ap.add_argument("--url", help="seat-layout url, fetched live through Chrome")
    ap.add_argument("--together", type=int, default=2, help="adjacent seats wanted")
    ap.add_argument("--port", type=int, default=9222)
    args = ap.parse_args()

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.url:
        sys.path.insert(0, str(Path(__file__).parent))
        from bms_seat_watch import Chrome
        chrome = Chrome(args.port)
        try:
            text = live_layout(chrome, args.url)
        finally:
            chrome.close()
        if not text:
            print("seat layout never decoded (page stuck, or layout request not made)")
            return 1
    else:
        ap.error("give --file or --url")

    print_summary(parse_layout(text), args.together)
    return 0


if __name__ == "__main__":
    sys.exit(main())
