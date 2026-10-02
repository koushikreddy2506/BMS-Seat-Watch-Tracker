# 🎟️ BMS Seat Watch Tracker

<h3 align="center"><i>Automated BookMyShow seat alerts and auto-hold for Hyderabad cinemas</i></h3>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-blue?style=for-the-badge&logo=python" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/Platform-Windows-0078D6?style=for-the-badge&logo=windows" alt="Windows Platform">
  <img src="https://img.shields.io/badge/Notifications-ntfy.sh-orange?style=for-the-badge" alt="ntfy notifications">
</p>

> **Note:** *Housefull isn't the end.* When a show sells out on BookMyShow, held-back blocks, unpaid bookings, and cancellations frequently reappear. Seat Watch continuously monitors shows, sends instant push alerts to your phone, and can auto-hold seats up to the payment screen.

---

## 📌 Table of Contents

- [Overview](#-overview)
- [Key Features](#-key-features)
- [Repository Architecture](#-repository-architecture)
- [Prerequisites](#-prerequisites)
- [Setup & Installation](#-setup--installation)
- [Usage Guide](#-usage-guide)
- [Ground Rules & Safety](#-ground-rules--safety)
- [License](#-license)

---

## 🔍 Overview

Seat Watch is a private utility built specifically for movie enthusiasts tracking cinema seats in **Hyderabad**. It consists of a local web server interface, automated polling mechanisms, and a browser automation runner to secure ticket holds.

#### Why Seat Watch?
1. **Instant Notifications:** Get alerted the second seat blocks open up.
2. **Auto-Hold Capability:** Hold seats automatically in a real browser session so you never miss out while navigating.
3. **Privacy First:** Payment and sensitive credentials remain strictly under manual human control.

---

## ⚡ Key Features

##### 1. Intelligent Seat Monitoring
* Continuously polls BookMyShow layout APIs within rate limits.
* Parses seat categories, price bands, and contiguous group availability.

##### 2. Instant Phone Alerts
* Integrates seamlessly with [ntfy](https://ntfy.sh) for instant push notifications on iOS and Android.

##### 3. Automated Hold Engine
* Powered by Playwright and logged-in Chrome instances.
* Selects optimal seats together and pauses at the final payment gateway.

##### 4. Self-Hosted Dashboard & Tunnel
* Web management dashboard (`bms_server.py`) paired with Cloudflare Tunnels (`cloudflared`) for secure remote access.

---

## 📁 Repository Architecture

| File / Component | Purpose & Description |
| :--- | :--- |
| `bms_server.py` | Core web server: UI landing page, show selector, group booking rules, and admin interface. |
| `bms_seat_watch.py` | Background watcher engine & shared parsing logic (cinemas, dates, show changes). |
| `seat_holder.py` | Playwright browser runner for holding seats in a logged-in Chrome profile. |
| `seat_maps.py` / `seat_layout.py` | Layout decoding, coordinate mapping, and seat grid processing logic. |
| `seat_capture.py` / `seat_decode_probe.py` | Low-level layout capture and API response probe utilities. |
| `start_site.py` | Watchdog script maintaining web server and Cloudflare Tunnel runtime. |
| `start_all.bat` / `stop_all.bat` | Windows batch control scripts for multi-process management. |
| `watch_config.example.json` | Configuration blueprint for topics, domain hosts, and thresholds. |

---

## 🛠️ Prerequisites

###### Mandatory System Requirements:
* **Operating System:** Windows 10/11
* **Runtime:** Python `3.12+`
* **Browser:** Google Chrome (installed at standard paths)
* **Tunnel Client:** [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/) CLI tool

---

## 🚀 Setup & Installation

###### Step 1: Clone & Navigate
```bash
git clone [https://github.com/koushikreddy2506/BMS-Seat-Watch-Tracker.git](https://github.com/koushikreddy2506/BMS-Seat-Watch-Tracker.git)
cd BMS-Seat-Watch-Tracker
Step 2: Create Environment & Install Dependencies
DOS
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\playwright install chromium
Step 3: Configure Settings
Copy watch_config.example.json to watch_config.json and edit your preferences:

DOS
copy watch_config.example.json watch_config.json
Step 4: Configure Push Notifications
Install the free ntfy app on your phone (Android / iOS).

Subscribe to your unique topic name configured in watch_config.json.

🎮 Usage Guide
Starting the Services
Run the bundled batch file to launch the server, watcher, and Cloudflare tunnel simultaneously:

DOS
start_all.bat
The first launch will output an Admin Access Key in the website console logs.

Open the local or tunneled URL to set up target movies and cinema screens.

Stopping the Services
To terminate all active processes cleanly:

DOS
stop_all.bat
🛡️ Ground Rules & Safety
📄 License
This project is for private educational and personal use only. Not affiliated with, maintained by, or endorsed by BookMyShow.

WORKING LINK :
https://bmstickethelper.dpdns.org/
