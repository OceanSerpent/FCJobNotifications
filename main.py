"""
Amazon SPA4 Job Alert Bot
=========================
Monitors hiring.amazon.com for new job openings at the SPA4 warehouse
(3751 E Harrisburg Pike, Middletown, PA 17057) and sends an SMS
notification whenever new listings appear.

Setup instructions are printed when you run: python amazon_job_alert.py --setup
"""

import json
import os
import smtplib
import time
import argparse
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIGURATION  ← Edit these values
# ─────────────────────────────────────────────
GMAIL_SENDER   = os.environ.get("GMAIL_SENDER", "YOUR_GMAIL_ADDRESS@gmail.com")  # Gmail you send FROM
GMAIL_APP_PW   = os.environ.get("GMAIL_APP_PW", "xxxx xxxx xxxx xxxx")           # Gmail App Password (16 chars)
NOTIFY_SMS     = "6092716429@tmomail.net"         # SMS via Mint Mobile gateway
CHECK_INTERVAL = 30 * 60                          # 30 minutes in seconds

# Target warehouse details
TARGET_SITE_NAME = "SPA4"
TARGET_ADDRESS   = "3751 E Harrisburg Pike"
TARGET_CITY      = "Middletown"
TARGET_STATE     = "PA"

# Search URL on hiring.amazon.com
SEARCH_URL = (
    "https://hiring.amazon.com/app#/jobSearch"
    "?jobTitle=Amazon%20Fulfillment%20Center%20Warehouse%20Associate"
    "&radius=30&zipCode=17112"
)

# File to persist seen job IDs between runs
STATE_FILE = Path("seen_jobs.json")
# ─────────────────────────────────────────────


def load_seen_jobs() -> set:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        return set(data)
    return set()


def save_seen_jobs(job_ids: set):
    STATE_FILE.write_text(json.dumps(list(job_ids)))


def is_spa4_job(job: dict) -> bool:
    """Return True if the job is located at the SPA4 / Middletown facility."""
    address  = (job.get("address", "") or "").lower()
    city     = (job.get("city", "")    or "").lower()
    state    = (job.get("state", "")   or "").upper()
    location = (job.get("location", "") or "").lower()
    label    = (job.get("label", "")   or "").lower()

    address_match = TARGET_ADDRESS.lower() in address or TARGET_ADDRESS.lower() in location
    city_match    = TARGET_CITY.lower() in city or TARGET_CITY.lower() in location
    state_match   = TARGET_STATE.upper() == state or TARGET_STATE.lower() in location
    site_match    = TARGET_SITE_NAME.lower() in label or TARGET_SITE_NAME.lower() in location

    return (address_match or site_match) and (city_match or state_match)


def scrape_jobs() -> list[dict]:
    """Use Playwright to load the hiring page and extract job listings."""
    from playwright.sync_api import sync_playwright

    jobs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Intercept API responses that carry job data
        captured = []

        def handle_response(response):
            url = response.url
            if "jobSearch" in url or "search" in url.lower():
                try:
                    body = response.json()
                    captured.append(body)
                except Exception:
                    pass

        page.on("response", handle_response)

        print(f"[{now()}] Loading hiring.amazon.com ...")
        page.goto(SEARCH_URL, timeout=60_000, wait_until="networkidle")
        page.wait_for_timeout(5000)  # Extra wait for dynamic content

        # Try to read rendered HTML job cards as fallback
        cards = page.query_selector_all(
            "[data-test-component='StencilReact'] li, .job-card, [class*='jobCard'], [class*='job-tile']"
        )
        for card in cards:
            try:
                title_el  = card.query_selector("[class*='title'], h2, h3")
                loc_el    = card.query_selector("[class*='location'], [class*='address']")
                link_el   = card.query_selector("a")
                job_id_el = card.get_attribute("data-job-id") or card.get_attribute("id") or ""

                title    = title_el.inner_text().strip() if title_el else ""
                location = loc_el.inner_text().strip()   if loc_el  else ""
                href     = link_el.get_attribute("href") if link_el else ""
                job_id   = job_id_el or href or title

                if title:
                    jobs.append({
                        "id":       job_id,
                        "title":    title,
                        "location": location,
                        "city":     TARGET_CITY  if TARGET_CITY.lower()  in location.lower() else "",
                        "state":    TARGET_STATE if TARGET_STATE          in location         else "",
                        "address":  TARGET_ADDRESS if TARGET_ADDRESS.lower() in location.lower() else "",
                        "url":      f"https://hiring.amazon.com{href}" if href.startswith("/") else href,
                    })
            except Exception:
                continue

        # Also parse any intercepted JSON payloads
        for payload in captured:
            items = []
            if isinstance(payload, list):
                items = payload
            elif isinstance(payload, dict):
                for key in ("jobs", "results", "data", "items", "jobResults"):
                    if isinstance(payload.get(key), list):
                        items = payload[key]
                        break

            for item in items:
                job_id = str(item.get("jobId") or item.get("id") or item.get("uuid") or "")
                if job_id:
                    jobs.append({
                        "id":       job_id,
                        "title":    item.get("title")    or item.get("jobTitle")      or "",
                        "location": item.get("location") or item.get("locationName")  or "",
                        "city":     item.get("city")     or "",
                        "state":    item.get("state")    or item.get("stateCode")     or "",
                        "address":  item.get("address")  or item.get("streetAddress") or "",
                        "label":    item.get("label")    or item.get("facilityName")  or "",
                        "url":      "https://hiring.amazon.com/app#/jobSearch",
                        "pay":      item.get("basePay")  or item.get("hourlyPay")     or "",
                        "shift":    item.get("shiftType") or item.get("scheduleType") or "",
                    })

        browser.close()

    # De-duplicate by id
    seen = {}
    for j in jobs:
        if j["id"] and j["id"] not in seen:
            seen[j["id"]] = j
    return list(seen.values())


def send_sms(new_jobs: list[dict]):
    """Send a brief SMS via Mint Mobile's email-to-text gateway."""
    count = len(new_jobs)
    titles = ", ".join(j["title"] for j in new_jobs[:2])
    if count > 2:
        titles += f" + {count - 2} more"
    body = f"Amazon SPA4 Alert: {count} new job(s)! {titles} — Apply: {SEARCH_URL}"

    msg = MIMEText(body)
    msg["From"]    = GMAIL_SENDER
    msg["To"]      = NOTIFY_SMS
    msg["Subject"] = ""  # SMS gateways ignore the subject

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_SENDER, GMAIL_APP_PW)
        server.sendmail(GMAIL_SENDER, NOTIFY_SMS, msg.as_string())

    print(f"[{now()}] ✅ SMS sent to {NOTIFY_SMS}")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def check_once():
    """Run a single check cycle."""
    print(f"[{now()}] Checking for SPA4 jobs at {TARGET_ADDRESS}, {TARGET_CITY} ...")
    seen_ids = load_seen_jobs()

    all_jobs  = scrape_jobs()
    spa4_jobs = [j for j in all_jobs if is_spa4_job(j)]

    print(f"[{now()}] Found {len(all_jobs)} total jobs, {len(spa4_jobs)} at SPA4.")

    new_jobs = [j for j in spa4_jobs if j["id"] not in seen_ids]

    if new_jobs:
        print(f"[{now()}] 🆕 {len(new_jobs)} NEW job(s)! Sending SMS ...")
        try:
            send_sms(new_jobs)
        except Exception as e:
            print(f"[{now()}] ❌ SMS failed: {e}")
    else:
        print(f"[{now()}] No new jobs since last check.")

    # Update seen IDs with ALL spa4 jobs (not just new ones)
    updated_ids = seen_ids | {j["id"] for j in spa4_jobs}
    save_seen_jobs(updated_ids)


def run_loop():
    """Run the bot in a continuous loop."""
    print(f"[{now()}] 🤖 Amazon SPA4 Job Alert Bot started.")
    print(f"         Target   : {TARGET_SITE_NAME} — {TARGET_ADDRESS}, {TARGET_CITY}, {TARGET_STATE}")
    print(f"         SMS to   : {NOTIFY_SMS}")
    print(f"         Interval : every {CHECK_INTERVAL // 60} minutes")
    print("         Press Ctrl+C to stop.\n")

    while True:
        try:
            check_once()
        except Exception as e:
            print(f"[{now()}] ⚠️  Error during check: {e}")
        print(f"[{now()}] Sleeping {CHECK_INTERVAL // 60} min until next check ...\n")
        time.sleep(CHECK_INTERVAL)


def print_setup():
    print("""
╔══════════════════════════════════════════════════════╗
║       Amazon SPA4 Job Alert — Setup Guide           ║
╚══════════════════════════════════════════════════════╝

STEP 1 ─ Install Python dependencies
─────────────────────────────────────
  pip install playwright
  playwright install chromium

STEP 2 ─ Create a Gmail App Password
──────────────────────────────────────
  Gmail is used to fire the SMS through Mint Mobile's free text gateway.
  You need an "App Password" so the script can log in:

  1. Go to https://myaccount.google.com/security
  2. Under "How you sign in to Google", click "2-Step Verification"
     (enable it if not already on)
  3. Scroll to the bottom → "App passwords"
  4. Name it "Amazon Job Bot", click Create
  5. Copy the 16-character password shown (e.g. "abcd efgh ijkl mnop")

STEP 3 ─ Edit this script
──────────────────────────
  Open amazon_job_alert.py and fill in the CONFIGURATION section at the top:

    GMAIL_SENDER = "your_gmail@gmail.com"   ← any Gmail account you own
    GMAIL_APP_PW = "abcd efgh ijkl mnop"    ← paste your App Password here

  Your SMS number (6092716429@tmomail.net) is already set!

STEP 4 ─ Run the bot
──────────────────────
  python amazon_job_alert.py

  It will check every 30 minutes and text you when new SPA4 jobs appear.

STEP 5 (optional) ─ Keep it running 24/7
──────────────────────────────────────────
  Option A — Leave your PC on with the script running in a terminal.

  Option B — Run in background on Mac/Linux:
    nohup python amazon_job_alert.py > job_alert.log 2>&1 &

  Option C — Schedule with cron (Mac/Linux):
    crontab -e
    Add this line:
    */30 * * * * /usr/bin/python3 /path/to/amazon_job_alert.py --once >> /path/to/job_alert.log 2>&1

  Option D — Windows Task Scheduler:
    Action → Start a program → python.exe
    Arguments: C:\\path\\to\\amazon_job_alert.py --once
    Trigger: Repeat every 30 minutes

──────────────────────────────────────────────────────
  TIP: Run with --once to do a single check (great for testing):
    python amazon_job_alert.py --once
──────────────────────────────────────────────────────
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Amazon SPA4 Job Alert Bot")
    parser.add_argument("--once",  action="store_true", help="Check once and exit")
    parser.add_argument("--setup", action="store_true", help="Print setup instructions")
    args = parser.parse_args()

    if args.setup:
        print_setup()
    elif args.once:
        check_once()
    else:
        run_loop()