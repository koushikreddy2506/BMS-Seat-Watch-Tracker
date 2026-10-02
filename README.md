# Seat Watch

**Housefull isn't the end.** After a show sells out on BookMyShow, the best seats often come back: blocks the
cinema held back, bookings nobody paid for, cancellations. They're usually gone within minutes. Seat Watch keeps
checking, sends your phone an alert the moment seats open, and can hold them up to the payment page while you pay
from your own UPI app.

Built for Hyderabad cinemas. A private tool, not affiliated with BookMyShow.

## What's in here

| File | What it does |
| --- | --- |
| `bms_server.py` | The website: landing page, movie and cinema browser, seat-map picker, watches, auto-hold requests, group bookings, admin page. Checks BookMyShow over plain HTTP within a request budget. |
| `bms_seat_watch.py` | The watcher and shared parsing (cinema pages, date strips, show changes). |
| `seat_holder.py` | The holder: in a logged-in Chrome, picks the best seats together and stops at the payment page. |
| `seat_maps.py`, `seat_layout.py`, `seat_capture.py`, `seat_decode_probe.py` | Reading BookMyShow seat layouts. |
| `start_site.py` | Keeps the website and the Cloudflare tunnel running. |
| `start_all.bat` / `stop_all.bat` | Start or stop everything on Windows. |
| `watch_config.example.json` | Settings template: copy it to `watch_config.json` and fill in your own values. |

## Setup (Windows)

1. Install Python 3.12+, Google Chrome and [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/).
2. In the project folder:
   ```bat
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   .venv\Scripts\playwright install chromium
   ```
3. Copy `watch_config.example.json` to `watch_config.json`. Set your own private ntfy topics (long, unguessable
   names) and your tunnel's hostname.
4. Install the free [ntfy](https://ntfy.sh) app on your phone and subscribe to your topic.
5. Run `start_all.bat`. The admin link (with its key) is printed in the website's console the first time it starts.

`holder.armed` is `false` in the example: the holder then only finds seats and reports what it would hold.
Set it to `true` only when you want real holds on your BookMyShow account.

## Ground rules the code keeps

- **Gentle on BookMyShow.** Every request goes through one budget (at most 30 a minute, one at a time). A refusal
  pauses everything and alerts the owner. Posters and release dates come from TMDB or Wikipedia, never BookMyShow.
- **Nothing is booked without a person.** Holds stop at the payment page. Payment is always made by the person
  in their own UPI app; the code never enters card details or OTPs.
- **Secrets stay local.** `watch_config.json`, the database, tokens, keys, logs, captures and the Chrome profile
  are in `.gitignore`.
