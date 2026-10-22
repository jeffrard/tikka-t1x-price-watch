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

class Blocked(Exception):
    """The site refused us (rate-limit / bot protection)."""


class OutOfStock(Exception):
    """The page loaded fine but says the item is out of stock."""


def fetch(url: str) -> str:
    for attempt in range(2):
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code in (403, 429, 503) and attempt == 0:
            wait = resp.headers.get("Retry-After", "10")
            time.sleep(min(int(wait) if wait.isdigit() else 10, 20))
            continue
        if resp.status_code in (403, 429, 503):
            raise Blocked(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        return resp.text
    raise Blocked("retry failed")


def parse_price(text: str) -> float | None:
    """First dollar amount in text that is plausible for a rifle."""
    lo, hi = CONFIG.get("price_sanity", [250, 2000])
    for m in PRICE_RE.finditer(text or ""):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if lo <= v <= hi:
            return v
    return None


def matches_product(text: str) -> bool:
    t = " " + (text or "").lower() + " "
    if any(x.lower() in t for x in CONFIG["exclude_any"]):
        return False
    if not all(x.lower() in t for x in CONFIG["must_match_all"]):
        return False
    return any(x.lower() in t for x in CONFIG["must_match_any"])


def title_has_no_excludes(title: str) -> bool:
    t = " " + title.lower() + " "
    return not any(x.lower() in t for x in CONFIG["exclude_any"])


def host(url: str) -> str:
    return re.sub(r"^www\.", "", url.split("/")[2])


# ------------------------------------------- aggregator / search-result pages

def parse_listing_page(html: str, page_url: str) -> list[Listing]:
    """Works on Gun Made, gun.deals, GunPrime and similar search pages.

    Each listing is a 'card' holding a product-title link and a price. We find
    candidate title links, then climb to the smallest ancestor that holds only
    one of them: that ancestor is the card. Cards with no visible dollar price
    (e.g. 'MAP' / 'see price in cart') are skipped."""
    soup = BeautifulSoup(html, "html.parser")
    is_gunmade = "gunmade.com" in page_url

    def is_candidate(a) -> bool:
        if is_gunmade:
            return "/go/" in a.get("href", "")
        text = (a.get("title") or a.get_text(" ", strip=True)).lower()
        return len(text) >= 8 and "t1x" in text and "17" in text

    cands = [a for a in soup.find_all("a", href=True) if is_candidate(a)]
    cand_hrefs = lambda el: {x["href"] for x in el.find_all("a", href=True) if is_candidate(x)}

    out: list[Listing] = []
    seen: set[str] = set()
    for a in cands:
        title = a.get("title") or a.get_text(" ", strip=True)
        href = urljoin(page_url, a["href"])
        if href in seen or not title_has_no_excludes(title):
            continue

        card = a
        for _ in range(10):
            parent = card.parent
            if parent is None or len(cand_hrefs(parent)) > 1:
                break
            card = parent
        text = card.get_text(" ", strip=True)

        price = parse_price(text)
        if price is None:
            continue
        # If the card shows a UPC, trust it: keep only our exact UPC(s).
        # Otherwise fall back to matching the title text.
        upcs_on_card = set(re.findall(r"\b\d{12}\b", text))
        if upcs_on_card:
            if not upcs_on_card & set(CONFIG["upcs"]):
                continue
        elif not matches_product(title):
            continue
        if re.search(r"out of stock|sold out", text, re.I):
            continue

        condition = "Used" if re.search(r"\bused\b", text, re.I) else "New"
        seen.add(href)
        out.append(Listing(title, price, guess_store(card, page_url), href,
                           condition, host(page_url)))
    return out


def guess_store(card, page_url: str) -> str:
    # Gun Made: store names link to /stores/...; online-only stores are text
    # on the line after the UPC.
    a = card.select_one('a[href*="/stores/"]')
    if a:
        return a.get("title") or a.get_text(" ", strip=True)
    lines = [l for l in card.get_text("\n", strip=True).splitlines() if l.strip()]
    if "gunmade.com" in page_url:
        for i, line in enumerate(lines):
            if re.search(r"\b\d{12}\b", line):
                for nxt in lines[i + 1:i + 4]:
                    if nxt.upper() != "UPC" and not re.search(r"^\d|\$|reviews", nxt, re.I):
                        return nxt
    for line in lines:
        m = re.search(r"\b([a-z0-9-]+\.(?:com|net|us|store|shop))\b", line, re.I)
        if m and host(page_url) not in m.group(1).lower():
            return m.group(1).lower()
    return f"via {host(page_url)}"


# ------------------------------------------- retailer product pages

def parse_retailer(html: str, page_url: str) -> list[Listing]:
    """Single product page. Prefers the structured product data (JSON-LD) most
    stores embed for Google Shopping; falls back to price meta tags."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else page_url
    store = host(page_url)
    lo, hi = CONFIG.get("price_sanity", [250, 2000])

    out_of_stock = False
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in walk(data):
            if not isinstance(node, dict) or "Product" not in str(node.get("@type", "")):
                continue
            offers = node.get("offers")
            for offer in offers if isinstance(offers, list) else [offers]:
                if not isinstance(offer, dict):
                    continue
                raw = offer.get("price") or offer.get("lowPrice")
                try:
                    price = float(str(raw).replace(",", "").replace("$", ""))
                except (TypeError, ValueError):
                    continue
                if not lo <= price <= hi:
                    continue
                if re.search(r"OutOfStock|SoldOut|Discontinued", str(offer.get("availability", ""))):
                    out_of_stock = True
                    continue
                return [Listing(node.get("name") or title, price, store, page_url, "New", store)]
    if out_of_stock:
        raise OutOfStock()

    for attrs in ({"property": "product:price:amount"}, {"property": "og:price:amount"},
                  {"itemprop": "price"}):
        tag = soup.find(["meta", "span", "div"], attrs=attrs)
        val = tag and (tag.get("content") or tag.get_text(strip=True))
        price = parse_price("$" + str(val).lstrip("$")) if val else None
        if price:
            return [Listing(title, price, store, page_url, "New", store)]
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
    jobs = [(u, parse_listing_page) for u in CONFIG["aggregator_pages"]] + \
           [(u, parse_retailer) for u in CONFIG["retailer_pages"]]
    for url, parser in jobs:
        try:
            found = parser(fetch(url), url)
            listings += found
            health[url] = f"ok ({len(found)} listing{'s' if len(found) != 1 else ''})"
        except OutOfStock:
            health[url] = "ok (out of stock)"
        except Blocked as e:
            health[url] = f"BLOCKED: {e}"
        except Exception as e:  # noqa: BLE001 - report any failure, keep going
            health[url] = f"FAILED: {type(e).__name__}: {str(e)[:120]}"
        time.sleep(1.5)  # be polite

    # Keep the cheapest listing per store.
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
    state["source_failures"] = {
        url: (state["source_failures"].get(url, 0) + 1 if h.startswith("FAILED") else 0)
        for url, h in health.items()
    }

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
            top = "No in-stock listings with a visible price right now."
            title = "Tikka daily: no price found"
        broken = [re.sub(r"^https?://(www\.)?", "", u).split("/")[0]
                  for u, n in state["source_failures"].items() if n >= 6]
        ok = sum(1 for h in health.values() if h.startswith("ok"))
        priced = sum(1 for h in health.values() if re.match(r"ok \([1-9]", h))
        blocked = sorted({host(u) for u, h in health.items() if h.startswith("BLOCKED")})
        status = f"Sources checked: {ok}/{len(health)} ({priced} with a price)"
        if blocked:
            status += "\nBlocked today: " + ", ".join(blocked)
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
