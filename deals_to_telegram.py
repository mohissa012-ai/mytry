#!/usr/bin/env python3
"""
Checks https://deals.3rrood.com/store/amazon_resale every minute for about
RUN_MINUTES minutes, sends new deals to Telegram, then exits.
GitHub Actions starts a new run every 5 minutes, so coverage is ~continuous.
"""

import html
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------- Settings ----------------
URL = "https://deals.3rrood.com/store/amazon_resale"
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "4.5"))
CHECK_EVERY_SECONDS = int(os.environ.get("CHECK_EVERY_SECONDS", "60"))
ELECTRONICS_ONLY = os.environ.get("ELECTRONICS_ONLY", "false").lower() == "true"
USE_BROWSER = os.environ.get("USE_BROWSER", "false").lower() == "true"
MAX_FIRST_RUN = 30
MAX_SEEN = 3000
SEEN_FILE = Path("seen_deals.json")

KEYWORDS = [
    "phone", "iphone", "samsung", "laptop", "macbook", "tablet", "ipad",
    "tv", "monitor", "headphone", "earbud", "airpods", "speaker", "camera",
    "watch", "console", "playstation", "xbox", "nintendo", "switch", "router",
    "ssd", "keyboard", "mouse", "charger", "drone", "kindle", "echo", "alexa",
    "gpu", "processor", "printer", "electronic",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}


# ---------------- Fetching ----------------
def fetch_page() -> str:
    if USE_BROWSER:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(URL, wait_until="networkidle", timeout=60000)
            content = page.content()
            browser.close()
            return content

    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


# ---------------- Parsing ----------------
def parse_deals(page_html: str) -> list[dict]:
    """Generic parser (page structure unknown). Tweak selectors if needed."""
    soup = BeautifulSoup(page_html, "html.parser")
    deals, seen_links = [], set()

    cards = soup.select(
        "article, li, div[class*='deal'], div[class*='card'], div[class*='product']"
    )
    for card in cards:
        a = card.find("a", href=True)
        if not a:
            continue
        link = urljoin(URL, a["href"])
        title = (card.find(["h1", "h2", "h3", "h4"]) or a).get_text(" ", strip=True)
        if len(title) < 12 or link in seen_links:
            continue

        text = card.get_text(" ", strip=True)
        price_match = re.search(
            r"(?:EGP|E£|L\.E|\$|£|€)\s?[\d,]+(?:\.\d+)?|[\d,]+(?:\.\d+)?\s?(?:EGP|جنيه)",
            text,
        )
        deals.append(
            {
                "id": link,
                "title": title[:200],
                "link": link,
                "price": price_match.group(0) if price_match else "",
            }
        )
        seen_links.add(link)
    return deals


def is_electronic(deal: dict) -> bool:
    t = deal["title"].lower()
    return any(k in t for k in KEYWORDS)


# ---------------- Telegram ----------------
def send_telegram(deal: dict) -> None:
    lines = [f"<b>{html.escape(deal['title'])}</b>"]
    if deal["price"]:
        lines.append(f"Price: {html.escape(deal['price'])}")
    lines.append(html.escape(deal["link"]))
    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": "\n".join(lines),
            "parse_mode": "HTML",
        },
        timeout=30,
    )
    resp.raise_for_status()
    time.sleep(1.1)


# ---------------- State ----------------
def load_seen() -> list:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return []


def save_seen(seen: list) -> None:
    SEEN_FILE.write_text(json.dumps(seen[-MAX_SEEN:]))


# ---------------- Main ----------------
def check_once() -> None:
    first_run = not SEEN_FILE.exists()
    seen = load_seen()
    seen_set = set(seen)

    deals = parse_deals(fetch_page())
    if ELECTRONICS_ONLY:
        deals = [d for d in deals if is_electronic(d)]

    new = [d for d in deals if d["id"] not in seen_set]
    print(f"Found {len(deals)} deals, {len(new)} new.", flush=True)

    to_send = new[:MAX_FIRST_RUN] if first_run else new
    for d in to_send:
        try:
            send_telegram(d)
        except Exception as e:
            print("Telegram error:", e, flush=True)
            continue

    # mark everything on the page as seen (prevents floods and retries)
    for d in deals:
        if d["id"] not in seen_set:
            seen.append(d["id"])
            seen_set.add(d["id"])
    save_seen(seen)


def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID secrets.")
    end = time.time() + RUN_MINUTES * 60
    while True:
        started = time.time()
        try:
            check_once()
        except Exception as e:
            print("Check failed:", e, flush=True)
        sleep_for = CHECK_EVERY_SECONDS - (time.time() - started)
        if time.time() + max(sleep_for, 0) >= end:
            break
        time.sleep(max(sleep_for, 0))


if __name__ == "__main__":
    main()
