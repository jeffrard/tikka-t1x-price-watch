#!/usr/bin/env python3
"""
Tikka price watcher.

Runs on GitHub Actions every ~30 minutes. Each run:
  1. Pulls listings from Gun Made (a price aggregator) and a few retailer pages.
  2. Keeps only listings that match the rifle in config.json.
  3. Pushes an ALERT to your phone (via ntfy) for any new listing under the
     target price.
  4. Once a day, after the digest hour (noon Central), pushes a DAILY summary
     with the current cheapest listings and the health of each source.

State (what's already been alerted, when the last digest went out) lives in
state.json, which the workflow commits back to the repo.

Environment variables (set as GitHub secrets):
  NTFY_TOPIC          the ntfy topic your phone is subscribed to
Optional:
  NTFY_SERVER         defaults to https://ntfy.sh
  FORCE_DIGEST=1      send the daily summary now regardless of time
  DRY_RUN=1           print notifications instead of sending them
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
STATE_PATH = ROOT / "state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)")


@dataclass
class Listing:
    title: str
    price: float
    store: str
    url: str
    condition: str
    source: str

    @property
    def key(self) -> str:
        return f"{self.store}|{self.url}|{self.price:.2f}"


# ---------------------------------------------------------------- helpers

def fetch(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_price(text: str) -> float | None:
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def matches_product(text: str) -> bool:
    t = " " + (text or "").lower() + " "
    if any(x.lower() in t for x in CONFIG["exclude_any"]):
        return False
    if not all(x.lower() in t for x in CONFIG["must_match_all"]):
        return False
    return any(x.lower() in t for x in CONFIG["must_match_any"])


# ------------------------------------------------------- Gun Made parsing

def parse_gunmade(html: str, page_url: str) -> list[Listing]:
    """Each listing on Gun Made is a card with a title link to /go/..., a price
    (or 'MAP' when the store hides it), a condition, a UPC and a store name.
    We find every /go/ title link and climb to the smallest ancestor that holds
    exactly one such link: that ancestor is the card."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[Listing] = []
    seen: set[str] = set()

    for a in soup.select('a[href*="/go/"]'):
        title = a.get("title") or a.get_text(" ", strip=True)
        if not title or len(title) < 8:
            continue
        href = urljoin(page_url, a["href"])
        if href in seen:
            continue

        card = a
        for _ in range(8):
            parent = card.parent
            if parent is None:
                break
            go_links = {x["href"] for x in parent.select('a[href*="/go/"]')}
            if len(go_links) > 1:
                break
            card = parent
        text = card.get_text(" ", strip=True)

        # Ignore anything without a visible dollar price ("MAP" = hidden).
        price = parse_price(text)
        if price is None:
            continue
        if not title_has_no_excludes(title):
            continue
        # If the card shows a UPC, trust it: keep only our exact UPC(s).
        # Otherwise fall back to matching the title text.
        upcs_on_card = set(re.findall(r"\b\d{12}\b", text))
        if upcs_on_card:
            if not upcs_on_card & set(CONFIG["upcs"]):
                continue
        elif not matches_product(title):
            continue

        condition = "Used" if re.search(r"\bused\b", text, re.I) else "New"
        store = guess_store(card)
        seen.add(href)
        out.append(Listing(title, price, store, href, condition, "Gun Made"))
    return out


def title_has_no_excludes(title: str) -> bool:
    t = " " + title.lower() + " "
    return not any(x.lower() in t for x in CONFIG["exclude_any"])


def guess_store(card) -> str:
    # Store names link to /stores/...; online-only stores are plain text.
    a = card.select_one('a[href*="/stores/"]')
    if a:
        return a.get("title") or a.get_text(" ", strip=True)
    # On Gun Made the store name follows the UPC line.
    lines = [l for l in card.get_text("\n", strip=True).splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if re.search(r"\b\d{12}\b", line):
            for nxt in lines[i + 1:i + 4]:
                if nxt.upper() != "UPC" and not re.search(r"^\d|\$|reviews", nxt, re.I):
                    return nxt
    for line in lines:
        if re.search(r"\.(com|net|us)\b", line, re.I):
            return line
    return "unknown store"


# ------------------------------------------- retailer pages (JSON-LD offers)

def parse_retailer(html: str, page_url: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(strip=True) if soup.title else page_url)
    store = re.sub(r"^www\.", "", page_url.split("/")[2])

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in walk(data):
            if not isinstance(node, dict):
                continue
            if "Product" not in str(node.get("@type", "")):
                continue
            offers = node.get("offers")
            for offer in offers if isinstance(offers, list) else [offers]:
                if not isinstance(offer, dict):
                    continue
                raw = offer.get("price") or offer.get("lowPrice")
                try:
                    price = float(str(raw).replace(",", ""))
                except (TypeError, ValueError):
                    continue
                if price <= 0:
                    continue
                avail = str(offer.get("availability", ""))
                if "OutOfStock" in avail:
                    return []
                name = node.get("name") or title
                return [Listing(name, price, store, page_url, "New", store)]

    # Fallback: first price near the top of the page (meta tags first).
    meta = soup.find("meta", attrs={"property": "product:price:amount"}) or \
        soup.find("meta", attrs={"itemprop": "price"})
    if meta and meta.get("content"):
        try:
            return [Listing(title, float(meta["content"]), store, page_url, "New", store)]
        except ValueError:
            pass
    return []


def walk(node):
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


# ------------------------------------------------------------------ ntfy

def notify(title: str, body: str, click: str | None = None,
           tags: str = "", priority: str = "default") -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    if os.environ.get("DRY_RUN") or not topic:
        print(f"\n===== NOTIFY (dry run) [{priority}] {title}\n{body}\n(click: {click})\n=====")
        return
    headers = {
        # Headers must be plain ASCII; emoji go in Tags (ntfy renders them).
        "Title": title.encode("ascii", "ignore").decode(),
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = tags
    if click:
        headers["Click"] = click
    resp = requests.post(f"{server}/{topic}", data=body.encode("utf-8"),
                         headers=headers, timeout=30)
    resp.raise_for_status()
    print(f"Sent: {title}")


def fmt(l: Listing) -> str:
    used = " (used)" if l.condition == "Used" else ""
    return f"${l.price:,.2f}{used} - {l.store}\n{l.url}"


# ------------------------------------------------------------------- main

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"alerted": {}, "last_digest_date": None, "source_failures": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def collect() -> tuple[list[Listing], dict[str, str]]:
    listings: list[Listing] = []
    health: dict[str, str] = {}
    jobs = [(u, parse_gunmade) for u in CONFIG["aggregator_pages"]] + \
           [(u, parse_retailer) for u in CONFIG["retailer_pages"]]
    for url, parser in jobs:
        try:
            found = parser(fetch(url), url)
            listings += found
            health[url] = f"ok ({len(found)} listing{'s' if len(found) != 1 else ''})"
        except Exception as e:  # noqa: BLE001 - report any failure, keep going
            health[url] = f"FAILED: {type(e).__name__}: {str(e)[:120]}"
        time.sleep(2)  # be polite

    # Deduplicate by store+price, keep cheapest per store.
    best: dict[str, Listing] = {}
    for l in listings:
        k = l.store.lower()
        if k not in best or l.price < best[k].price:
            best[k] = l
    return sorted(best.values(), key=lambda l: l.price), health


def main() -> int:
    tz = ZoneInfo(CONFIG["timezone"])
    now = datetime.now(tz)
    target = float(CONFIG["target_price"])
    state = load_state()
    before = json.dumps(state, sort_keys=True)

    listings, health = collect()
    print(f"{now:%Y-%m-%d %H:%M %Z}: {len(listings)} matching listings")
    for l in listings[:10]:
        print("  " + fmt(l).replace("\n", "\n  "))
    for url, h in health.items():
        print(f"  [{h}] {url}")

    # Track consecutive failures per source (shown in the digest).
    for url, h in health.items():
        fails = state["source_failures"].get(url, 0)
        state["source_failures"][url] = fails + 1 if h.startswith("FAILED") else 0

    # ---- instant alerts
    hits = [l for l in listings if l.price < target and l.key not in state["alerted"]]
    for l in hits[:5]:  # one notification per deal, so each one taps through
        used = " (used)" if l.condition == "Used" else ""
        notify(
            f"Tikka ${l.price:,.2f}{used} at {l.store}",
            f"{l.title}\nUnder your ${target:,.0f} target. Tap to open the listing.\n"
            "Price excludes shipping/FFL fees.",
            click=l.url, tags="dart,moneybag", priority="high",
        )
    for l in hits:
        state["alerted"][l.key] = now.isoformat()

    # Forget alerts older than 30 days so a re-listing can alert again.
    cutoff = now.timestamp() - 30 * 86400
    state["alerted"] = {k: v for k, v in state["alerted"].items()
                        if datetime.fromisoformat(v).timestamp() > cutoff}

    # ---- daily digest
    today = now.date().isoformat()
    due = now.hour >= CONFIG["digest_hour"] and state.get("last_digest_date") != today
    if due or os.environ.get("FORCE_DIGEST"):
        if listings:
            top = "\n\n".join(fmt(l) for l in listings[:3])
            title = f"Tikka daily: ${listings[0].price:,.2f} at {listings[0].store}"
        else:
            top = "No listings with a visible price today (most stores show 'MAP')."
            title = "Tikka daily: no price found"
        broken = [re.sub(r"^https?://(www\.)?", "", u).split("/")[0]
                  for u, n in state["source_failures"].items() if n >= 6]
        ok = sum(1 for h in health.values() if not h.startswith("FAILED"))
        status = f"Sources working: {ok}/{len(health)}"
        if broken:
            status += "\nBroken 3+ hrs: " + ", ".join(sorted(set(broken)))
        notify(title, f"{top}\n\n{status}\nTarget: under ${target:,.0f}",
               click=listings[0].url if listings else None,
               tags="warning" if broken else "", priority="low")
        state["last_digest_date"] = today

    if json.dumps(state, sort_keys=True) != before:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
