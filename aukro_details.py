#!/usr/bin/env python3
"""Enrich saved Aukro listings with fields from each public offer detail page.

Run ``python aukro_details.py --limit 10`` for a small preview. The listing
scraper's input file is read only; enriched records go to a separate JSON file.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
import unicodedata
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from aukro_scraper import validate_offer_url


LOGGER = logging.getLogger("aukro_details")
OFFER_DETAIL_KEY = re.compile(r"/backend-web/api/offers/(\d+)/offerDetail")
EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0E\uFE0F\u200D\u20E3]")


class DetailError(ValueError):
    """An offer page cannot be read or does not contain the expected data."""


class NgStateParser(HTMLParser):
    """Read Angular's JSON state without depending on generated CSS classes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.in_state = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script" and attributes.get("id") == "ng-state":
            self.in_state = True

    def handle_data(self, data: str) -> None:
        if self.in_state:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.in_state = False


def clean_description(value: str) -> str:
    """Remove emoji and invisible formatting while keeping Czech/Slovak text."""
    without_emoji = EMOJI.sub("", unescape(value))
    without_controls = "".join(
        character
        for character in without_emoji
        if unicodedata.category(character) not in {"Cc", "Cf"}
        or character.isspace()
    )
    return " ".join(without_controls.split())


def parse_detail_page(page: str, offer_id: str) -> dict[str, Any]:
    parser = NgStateParser()
    parser.feed(page)
    if not parser.parts:
        raise DetailError("ng-state JSON is missing")
    try:
        state = json.loads("".join(parser.parts))
        cache = state["aukCache"]
    except (ValueError, KeyError, TypeError) as exc:
        raise DetailError("ng-state JSON is invalid") from exc

    for key, entry in cache.items():
        match = OFFER_DETAIL_KEY.search(key)
        if not match or match.group(1) != offer_id:
            continue
        detail = entry.get("b") if isinstance(entry, dict) else None
        if not isinstance(detail, dict):
            continue
        if str(detail.get("id")) != offer_id:
            raise DetailError(f"detail ID does not match {offer_id}")
        sale_type = detail.get("itemType")
        if sale_type not in {"BIDDING", "BUYNOW"}:
            raise DetailError(f"unknown sale type: {sale_type!r}")
        description = detail.get("descriptionStripped")
        if not isinstance(description, str):
            raise DetailError("descriptionStripped is missing")

        price_field = detail.get("price") if sale_type == "BIDDING" else detail.get("buyNowPrice")
        if not isinstance(price_field, dict) or price_field.get("currency") != "CZK":
            raise DetailError("CZK price is missing")
        price = price_field.get("amount")
        if not isinstance(price, (int, float)) or isinstance(price, bool) or price < 0:
            raise DetailError("CZK price is invalid")

        buy_now = detail.get("buyNowPrice")
        buy_now_price = None
        if detail.get("buyNowActive") and isinstance(buy_now, dict) and buy_now.get("currency") == "CZK":
            buy_now_price = buy_now.get("amount")

        return {
            "price_czk": price,
            "sale_type": sale_type,
            "buy_now_price_czk": buy_now_price,
            "bidders_count": detail.get("biddersCount"),
            "start_at": detail.get("startingTime"),
            "end_at": detail.get("endingTime"),
            "description_raw": description,
            "description_clean": clean_description(description),
        }
    raise DetailError(f"offer detail for {offer_id} is missing")


def fetch_detail(url: str, offer_id: str, timeout: float) -> dict[str, Any]:
    request = Request(
        validate_offer_url(url),
        headers={"User-Agent": "Mozilla/5.0 (compatible; BachelorThesisResearch/1.0)"},
    )
    with urlopen(request, timeout=timeout) as response:
        page = response.read().decode("utf-8")
    return parse_detail_page(page, offer_id)


def write_json_atomic(path: Path, records: list[dict[str, Any]]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("aukro_mince.json"))
    parser.add_argument("--output", type=Path, default=Path("aukro_mince_detail.json"))
    parser.add_argument("--limit", type=int, help="Enrich only the first N listings")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests")
    parser.add_argument("--timeout", type=float, default=20.0, help="Request timeout in seconds")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if (args.limit is not None and args.limit <= 0) or args.delay < 0 or args.timeout <= 0:
        parser.error("--limit and --timeout must be positive; --delay cannot be negative")
    if args.input.resolve() == args.output.resolve():
        parser.error("--input and --output must be different files")

    try:
        listings = json.loads(args.input.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOGGER.error("Could not read %s: %s", args.input, exc)
        return 1
    if not isinstance(listings, list):
        LOGGER.error("Input JSON must be a list of auction listings")
        return 1

    enriched: list[dict[str, Any]] = []
    failed = 0
    selected = listings[: args.limit] if args.limit is not None else listings
    for index, listing in enumerate(selected, 1):
        try:
            if not isinstance(listing, dict):
                raise DetailError("listing is not a JSON object")
            offer_id = str(listing["id"])
            url = str(listing["url"])
            if not url.rstrip("/").endswith("-" + offer_id):
                raise DetailError("listing ID does not match its URL")
            detail = fetch_detail(url, offer_id, args.timeout)
            record = dict(listing)
            record.pop("bids_count", None)  # Listing label can mean bidders, not bids.
            record.update(detail)
            enriched.append(record)
            LOGGER.info("%d/%d: %s (%s)", index, len(selected), offer_id, detail["sale_type"])
        except (KeyError, ValueError, HTTPError, URLError, TimeoutError, OSError) as exc:
            failed += 1
            LOGGER.error("%d/%d: could not enrich listing: %s", index, len(selected), exc)
        if index < len(selected):
            time.sleep(args.delay)

    if not failed and enriched:
        write_json_atomic(args.output, enriched)
        LOGGER.info("Saved %d details to %s", len(enriched), args.output)
    else:
        LOGGER.error("%d of %d listings failed; output was not replaced", failed, len(selected))
    return 1 if failed or not enriched else 0


if __name__ == "__main__":
    raise SystemExit(main())
