#!/usr/bin/env python3
"""Monitor the 3rrood Amazon-resale deals page and notify a Telegram chat.

Fetch modes:
  USE_BROWSER=false  -> requests (default)
  USE_BROWSER=true   -> Camoufox with a persistent local browser profile

For a first local browser run, set CAMOUFOX_HEADLESS=false. If the site asks
for a verification step, complete it manually in the visible browser. This
script does not attempt to solve or circumvent CAPTCHAs automatically.
"""

from __future__ import annotations

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
URL = os.environ.get("SCRAPE_URL", "https://deals.3rrood.com/store/amazon_resale")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

RUN_MINUTES = max(0.1, float(os.environ.get("RUN_MINUTES", "4.5")))
CHECK_EVERY_SECONDS = max(10, int(os.environ.get("CHECK_EVERY_SECONDS", "60")))
ELECTRONICS_ONLY = os.environ.get("ELECTRONICS_ONLY", "false").lower() == "true"
USE_BROWSER = os.environ.get("USE_BROWSER", "false").lower() == "true"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
CAMOUFOX_HEADLESS = os.environ.get("CAMOUFOX_HEADLESS", "false").lower() == "true"
MANUAL_CHALLENGE_WAIT = max(0, int(os.environ.get("MANUAL_CHALLENGE_WAIT", "120")))

MAX_FIRST_RUN = 30
MAX_SEEN = 3000
SEEN_FILE = Path(os.environ.get("SEEN_FILE", "seen_deals.json"))
PROFILE_DIR = Path(os.environ.get("CAMOUFOX_PROFILE_DIR", "user_data"))
REQUEST_TIMEOUT = 30

KEYWORDS = [
    "phone", "iphone", "samsung", "laptop", "macbook", "tablet", "ipad",
    "tv", "monitor", "headphone", "earbud", "airpods", "speaker", "camera",
    "watch", "console", "playstation", "xbox", "nintendo", "switch", "router",
    "ssd", "keyboard", "mouse", "charger", "drone", "kindle", "echo", "alexa",
    "gpu", "processor", "printer", "electronic",
]

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}

CHALLENGE_MARKERS = (
    "checking your browser",
    "verify you are human",
    "verify you are a human",
    "just a moment",
    "attention required",
    "enable javascript and cookies",
    "performing security verification",
    "security check",
)


# ---------------- Fetching ----------------
def _page_text(page) -> str:
    try:
        title = page.title()
    except Exception:
        title = ""
    try:
        body = page.locator("body").inner_text(timeout=8000)
    except Exception:
        body = ""
    return f"{title}\n{body}".lower()


def _looks_like_challenge(page) -> bool:
    text = _page_text(page)
    return any(marker in text for marker in CHALLENGE_MARKERS)


def fetch_with_camoufox() -> str:
    try:
        from camoufox.sync_api import Camoufox
    except ImportError as exc:
        raise RuntimeError(
            'Camoufox is not installed. Run: pip install -r requirements.txt && camoufox fetch'
        ) from exc

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Starting Camoufox (headless={CAMOUFOX_HEADLESS})", flush=True)
    print(f"Persistent profile: {PROFILE_DIR.resolve()}", flush=True)

    with Camoufox(
        headless=CAMOUFOX_HEADLESS,
        humanize=True,
        persistent_context=True,
        user_data_dir=str(PROFILE_DIR.resolve()),
    ) as context:
        page = context.new_page()
        response = page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        if _looks_like_challenge(page):
            if CAMOUFOX_HEADLESS or MANUAL_CHALLENGE_WAIT == 0:
                raise RuntimeError(
                    "The site is showing a browser verification page. "
                    "Try a visible local run with CAMOUFOX_HEADLESS=false and "
                    "complete the check manually if you are authorized."
                )

            print(
                f"Verification page detected. If a check is visible, complete it "
                f"in the browser. Waiting up to {MANUAL_CHALLENGE_WAIT}s...",
                flush=True,
            )
            deadline = time.monotonic() + MANUAL_CHALLENGE_WAIT
            while time.monotonic() < deadline and _looks_like_challenge(page):
                page.wait_for_timeout(1000)

            if _looks_like_challenge(page):
                raise RuntimeError(
                    "The verification page did not clear before the timeout. "
                    "No automatic CAPTCHA solving was attempted."
                )

        if response is not None and response.status >= 400:
            raise RuntimeError(f"Page returned HTTP {response.status}")

        # Wait briefly for client-rendered product cards after navigation/check.
        page.wait_for_timeout(2000)
        return page.content()


def fetch_page() -> str:
    if USE_BROWSER:
        return fetch_with_camoufox()

    response = requests.get(URL, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


# ---------------- Parsing ----------------
def parse_deals(page_html: str) -> list[dict[str, str]]:
    """Extract likely deal cards from the returned HTML.

    The page's exact markup may change; selectors are deliberately broad, so
    inspect the debug output if the page loads but zero deals are extracted.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    deals: list[dict[str, str]] = []
    seen_links: set[str] = set()

    cards = soup.select(
        "article, li, div[class*='deal'], div[class*='card'], div[class*='product']"
    )
    for card in cards:
        anchor = card.find("a", href=True)
        if not anchor:
            continue

        link = urljoin(URL, anchor["href"].strip())
        if not link.startswith(("http://", "https://")) or link in seen_links:
            continue

        heading = card.find(["h1", "h2", "h3", "h4"])
        title = (heading or anchor).get_text(" ", strip=True)
        if len(title) < 12:
            continue

        card_text = card.get_text(" ", strip=True)
        price_match = re.search(
            r"(?:EGP|E£|L\.E|\$|£|€)\s?[\d,]+(?:\.\d+)?|"
            r"[\d,]+(?:\.\d+)?\s?(?:EGP|جنيه)",
            card_text,
            flags=re.IGNORECASE,
        )

        deals.append({
            "id": link,
            "title": title[:200],
            "link": link,
            "price": price_match.group(0) if price_match else "",
        })
        seen_links.add(link)

    return deals


def is_electronic(deal: dict[str, str]) -> bool:
    title = deal["title"].lower()
    return any(keyword in title for keyword in KEYWORDS)


# ---------------- Telegram ----------------
def send_telegram(deal: dict[str, str]) -> None:
    lines = [f"<b>{html.escape(deal['title'])}</b>"]
    if deal.get("price"):
        lines.append(f"Price: {html.escape(deal['price'])}")
    lines.append(html.escape(deal["link"]))

    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        api_url,
        data={
            "chat_id": CHAT_ID,
            "text": "\n".join(lines),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("Telegram returned a non-JSON response") from exc
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result.get('description', 'unknown error')}")

    time.sleep(1.1)  # Stay below Telegram's usual per-chat message rate.


# ---------------- State ----------------
def load_seen() -> list[str]:
    if not SEEN_FILE.exists():
        return []
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {SEEN_FILE}: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError(f"{SEEN_FILE} must contain a JSON list")
    return [str(item) for item in data]


def save_seen(seen: list[str]) -> None:
    # Preserve order while removing duplicates, then cap the state file size.
    unique = list(dict.fromkeys(seen))[-MAX_SEEN:]
    temp_file = SEEN_FILE.with_suffix(SEEN_FILE.suffix + ".tmp")
    temp_file.write_text(json.dumps(unique, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_file.replace(SEEN_FILE)


# ---------------- Main ----------------
def check_once() -> None:
    first_run = not SEEN_FILE.exists()
    seen = load_seen()
    seen_set = set(seen)

    page_html = fetch_page()
    soup = BeautifulSoup(page_html, "html.parser")
    page_title = soup.title.get_text(" ", strip=True) if soup.title else "(no title)"
    links_count = len(soup.find_all("a", href=True))
    print(f"Page title: {page_title}", flush=True)
    print(f"HTML size: {len(page_html)} bytes; links: {links_count}", flush=True)
    print(f"Page text sample: {soup.get_text(' ', strip=True)[:300]}", flush=True)

    deals = parse_deals(page_html)
    if ELECTRONICS_ONLY:
        deals = [deal for deal in deals if is_electronic(deal)]

    new_deals = [deal for deal in deals if deal["id"] not in seen_set]
    print(f"Found {len(deals)} deals; {len(new_deals)} new.", flush=True)

    if DRY_RUN:
        for deal in new_deals[:MAX_FIRST_RUN]:
            print(f"DRY RUN: {deal['title']} | {deal['price']} | {deal['link']}", flush=True)
        print("DRY_RUN=true: Telegram was not contacted and seen_deals.json was not changed.", flush=True)
        return

    # On the first run, avoid flooding Telegram with the entire catalogue.
    # Older deals beyond MAX_FIRST_RUN are marked seen; failed sends are not.
    to_send = new_deals[:MAX_FIRST_RUN] if first_run else new_deals
    skipped_on_first_run = new_deals[MAX_FIRST_RUN:] if first_run else []
    updated_seen = list(seen)
    updated_seen.extend(deal["id"] for deal in skipped_on_first_run)

    for deal in to_send:
        try:
            send_telegram(deal)
            updated_seen.append(deal["id"])
            print(f"Sent: {deal['title'][:100]}", flush=True)
        except Exception as exc:
            print(f"Telegram send failed for {deal['link']}: {exc}", flush=True)

    save_seen(updated_seen)


def main() -> None:
    if not DRY_RUN and not BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN, or set DRY_RUN=true for a scrape-only test.")
    if not DRY_RUN and not CHAT_ID:
        raise SystemExit("Set TELEGRAM_CHAT_ID, or set DRY_RUN=true for a scrape-only test.")
    if USE_BROWSER:
        print("Fetch mode: Camoufox", flush=True)
        print("For first local verification, use CAMOUFOX_HEADLESS=false.", flush=True)
    else:
        print("Fetch mode: requests", flush=True)

    end_time = time.monotonic() + RUN_MINUTES * 60
    while True:
        started = time.monotonic()
        try:
            check_once()
        except Exception as exc:
            print(f"Check failed: {type(exc).__name__}: {exc}", flush=True)

        remaining = end_time - time.monotonic()
        if remaining <= 0:
            break
        sleep_for = min(max(0, CHECK_EVERY_SECONDS - (time.monotonic() - started)), remaining)
        if sleep_for > 0:
            time.sleep(sleep_for)


if __name__ == "__main__":
    main()
