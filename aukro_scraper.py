#!/usr/bin/env python3
"""Extract Aukro listings via CDP, then fetch and clean their offer details.

The script attaches to an existing Chrome instance; it never starts or closes Chrome.
It deliberately scopes extraction to Aukro's organic listing component and excludes
recommendation sliders, widgets, and banners.
Use --list-only to skip the detail step or --limit 10 for a small detail preview.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, Literal, TypeAlias, cast
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import (
    JavascriptException,
    SessionNotCreatedException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

LOGGER = logging.getLogger("aukro_scraper")

DEFAULT_DEBUGGER_ADDRESS: Final = "127.0.0.1:9222"
DEFAULT_OUTPUT: Final = Path("aukro_mince.json")
DEFAULT_DETAILS_OUTPUT: Final = Path("aukro_mince_detail.json")
DEFAULT_TIMEOUT_SECONDS: Final = 20.0

LISTING_ROOT_SELECTOR: Final = (
    "main auk-items-listing auk-listing-items-list, "
    "[role='main'] auk-items-listing auk-listing-items-list"
)

# Aukro offer URLs end in a numeric offer ID. Requiring >= 7 digits rejects
# category, navigation, filter, and pagination URLs.
OFFER_ID_RE: Final = re.compile(r"-(?P<id>\d{7,})(?:[/?#]|$)")
PRICE_RE: Final = re.compile(
    r"(?P<whole>\d{1,3}(?:[\s.\u00a0\u202f]\d{3})*|\d+)"
    r"(?:,(?P<decimal>\d{1,2}))?\s*Kč\b",
    re.IGNORECASE,
)
BIDS_RE: Final = re.compile(
    r"(?P<count>\d[\d\s\u00a0\u202f]*)\s+"
    r"(?:přihazuj\w*|příhoz\w*)",
    re.IGNORECASE,
)
BUY_NOW_RE: Final = re.compile(r"\bKup\s*teď!?\b", re.IGNORECASE)

BidValue: TypeAlias = int | Literal["KUP_TED"] | None
PriceValue: TypeAlias = int | float


@dataclass(frozen=True, slots=True)
class CoinListing:
    id: str
    title: str
    price_czk: PriceValue
    bids_count: BidValue
    url: str


class ExtractionError(RuntimeError):
    """Raised when the page is reachable but does not contain valid listings."""


# This JavaScript executes atomically against a fresh DOM snapshot. Selenium does
# not retain card WebElements, so Angular rerenders cannot cause stale-element errors.
EXTRACT_CARDS_JS: Final = r"""
const rootSelector = arguments[0];
const root = document.querySelector(rootSelector);
if (!root) return {rootFound: false, rows: [], url: location.href};

const excludedAncestorSelector = [
  'auk-item-scroll-slider',
  'auk-aukro-widget',
  'auk-banner',
  'auk-third-party-banner'
].join(',');

const rows = [];
for (const card of root.querySelectorAll(
  'auk-advanced-item-card, auk-basic-item-card'
)) {
  if (card.closest(excludedAncestorSelector)) continue;

  // A result card must belong to the actual list/grid view. This excludes
  // category tiles, header modules, sidebars, and unrelated custom elements.
  if (!card.closest('auk-listing-items-list-view, auk-listing-items-grid-view')) {
    continue;
  }

  const anchor = card.querySelector(
    'a.item-card-main-container[href], a.item-card[href], a[href]'
  );
  if (!anchor) continue;

  const titleNode = card.querySelector(
    'auk-item-card-title [auktestidentification="item-card-title"], ' +
    'auk-item-card-title h2, auk-item-card-title h3, auk-item-card-title'
  );
  const priceNode = card.querySelector('auk-item-card-price');
  const image = card.querySelector('auk-item-card-image img[alt], img[alt]');

  rows.push({
    elementId: card.id || card.querySelector('[id^="item-"]')?.id || '',
    dataId: card.getAttribute('data-item-id') ||
            card.getAttribute('data-offer-id') || '',
    href: anchor.href,
    titleText: titleNode?.textContent || image?.alt || '',
    priceText: priceNode?.textContent || '',
    cardText: card.innerText || ''
  });
}
return {rootFound: true, rows, url: location.href};
"""


WAIT_FOR_DOM_QUIET_JS: Final = r"""
const rootSelector = arguments[0];
const quietMs = arguments[1];
const maximumMs = arguments[2];
const done = arguments[arguments.length - 1];
const root = document.querySelector(rootSelector);
if (!root) { done(false); return; }

let quietTimer;
let finished = false;
const finish = () => {
  if (finished) return;
  finished = true;
  observer.disconnect();
  clearTimeout(quietTimer);
  clearTimeout(maximumTimer);
  done(true);
};
const resetQuietTimer = () => {
  clearTimeout(quietTimer);
  quietTimer = setTimeout(finish, quietMs);
};
const observer = new MutationObserver(resetQuietTimer);
observer.observe(root, {childList: true, subtree: true, characterData: true});
const maximumTimer = setTimeout(finish, maximumMs);
resetQuietTimer();
"""


DEBUG_CARD_JS: Final = r"""
const root = document.querySelector(arguments[0]);
if (!root) return null;
const cards = [...root.querySelectorAll(
  'auk-advanced-item-card, auk-basic-item-card'
)];
const card = cards.find(candidate =>
  !candidate.closest(
    'auk-item-scroll-slider, auk-aukro-widget, auk-banner, auk-third-party-banner'
  ) && candidate.closest(
    'auk-listing-items-list-view, auk-listing-items-grid-view'
  )
);
if (!card) return null;

const ancestors = [];
for (let node = card; node && ancestors.length < 8; node = node.parentElement) {
  ancestors.push({tag: node.tagName.toLowerCase(), id: node.id, class: node.className});
}
return {
  url: location.href,
  ancestors,
  outerHTML: card.outerHTML
};
"""


def normalize_space(value: str) -> str:
    """Collapse regular and non-breaking whitespace into one ASCII space."""
    return " ".join(value.replace("\u00a0", " ").replace("\u202f", " ").split())


def parse_offer_id(*values: str) -> str:
    """Read a numeric offer ID from a card attribute or canonical offer URL."""
    for value in values:
        if not value:
            continue
        attribute_match = re.search(r"(?:^|[-_])(\d{7,})$", value)
        if attribute_match:
            return attribute_match.group(1)
        url_match = OFFER_ID_RE.search(value)
        if url_match:
            return url_match.group("id")
    raise ValueError("card has no valid Aukro offer ID")


def validate_offer_url(value: str) -> str:
    """Accept only HTTPS Aukro offer URLs and strip fragments."""
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in {"aukro.cz", "www.aukro.cz"}:
        raise ValueError(f"unexpected offer host: {parsed.hostname!r}")
    if not OFFER_ID_RE.search(parsed.path):
        raise ValueError("URL path does not end in an Aukro offer ID")
    return parsed._replace(fragment="").geturl()


def parse_price_czk(value: str) -> PriceValue:
    """Convert '1 234 Kč' or '1.234,50 Kč' to a JSON number."""
    match = PRICE_RE.search(normalize_space(value))
    if not match:
        raise ValueError(f"unrecognized CZK price: {value!r}")
    whole = int(re.sub(r"[^0-9]", "", match.group("whole")))
    decimal = match.group("decimal")
    if decimal and int(decimal) != 0:
        return whole + int(decimal.ljust(2, "0")) / 100
    return whole


def parse_bid_value(value: str) -> BidValue:
    """Return an integer bid count, KUP_TED, or None for an unknown sale mode."""
    text = normalize_space(value)
    if BUY_NOW_RE.search(text):
        return "KUP_TED"
    match = BIDS_RE.search(text)
    if not match:
        return None
    return int(re.sub(r"\D", "", match.group("count")))


def attach_to_chrome(debugger_address: str, timeout: float) -> webdriver.Chrome:
    options = Options()
    options.debugger_address = debugger_address
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(timeout)
        driver.set_script_timeout(timeout)
        return driver
    except SessionNotCreatedException as exc:
        raise ExtractionError(
            "Could not attach to Chrome. Ensure Chrome was started with "
            f"--remote-debugging-port={debugger_address.rsplit(':', 1)[-1]} and "
            "that ChromeDriver/Selenium Manager is compatible with Chrome."
        ) from exc
    except WebDriverException as exc:
        raise ExtractionError(
            f"CDP/Selenium connection to {debugger_address} failed: {exc.msg}"
        ) from exc


def ensure_page_is_active(driver: webdriver.Chrome, timeout: float) -> None:
    """Activate the attached tab so Aukro's IntersectionObservers can hydrate cards."""
    try:
        window_info = cast(
            dict[str, object],
            driver.execute_cdp_cmd("Browser.getWindowForTarget", {}),
        )
        bounds = cast(dict[str, object], window_info.get("bounds", {}))
        if bounds.get("windowState") == "minimized":
            driver.execute_cdp_cmd(
                "Browser.setWindowBounds",
                {
                    "windowId": window_info["windowId"],
                    "bounds": {"windowState": "normal"},
                },
            )
        driver.execute_cdp_cmd("Page.bringToFront", {})
        driver.execute_script("window.focus();")
        WebDriverWait(driver, timeout, poll_frequency=0.1).until(
            lambda current: current.execute_script(
                "return document.visibilityState === 'visible';"
            )
        )
    except TimeoutException as exc:
        raise ExtractionError(
            "The attached Aukro tab is hidden. Aukro defers result-card rendering "
            "through IntersectionObserver while the tab/window is hidden. Keep the "
            "Chrome window open and not minimized, then run the scraper again."
        ) from exc
    except WebDriverException as exc:
        raise ExtractionError(f"Could not activate the attached Aukro tab: {exc.msg}") from exc


def wait_for_listing_root(driver: webdriver.Chrome, timeout: float) -> None:
    try:
        WebDriverWait(driver, timeout, poll_frequency=0.25).until(
            lambda current: current.execute_script(
                "return document.readyState === 'complete' && "
                "Boolean(document.querySelector(arguments[0]));",
                LISTING_ROOT_SELECTOR,
            )
        )
    except TimeoutException as exc:
        raise ExtractionError(
            "The main Aukro listing container was not found. Current URL: "
            f"{driver.current_url!r}. Open a current category/search result page "
            "(for numismatics: https://aukro.cz/numismatika)."
        ) from exc


def wait_for_dom_quiet(driver: webdriver.Chrome) -> None:
    """Wait for 350 ms without a listing mutation, capped at 2.5 seconds."""
    try:
        driver.execute_async_script(
            WAIT_FOR_DOM_QUIET_JS,
            LISTING_ROOT_SELECTOR,
            350,
            2_500,
        )
    except (JavascriptException, TimeoutException):
        # A mutation wait is an optimization. The next atomic snapshot remains safe.
        LOGGER.debug("DOM quiet wait timed out; continuing with a fresh snapshot")


def _raw_card_rows(driver: webdriver.Chrome) -> list[dict[str, object]]:
    result = cast(
        dict[str, object],
        driver.execute_script(EXTRACT_CARDS_JS, LISTING_ROOT_SELECTOR),
    )
    if not bool(result.get("rootFound")):
        raise ExtractionError("The listing container disappeared during extraction")
    rows = result.get("rows")
    if not isinstance(rows, list):
        raise ExtractionError("Aukro extraction JavaScript returned malformed data")
    return cast(list[dict[str, object]], rows)


def parse_card(row: dict[str, object]) -> CoinListing:
    href = validate_offer_url(str(row.get("href", "")))
    offer_id = parse_offer_id(
        str(row.get("dataId", "")),
        str(row.get("elementId", "")),
        href,
    )
    title = normalize_space(str(row.get("titleText", "")))
    if not title:
        raise ValueError("empty listing title")

    return CoinListing(
        id=offer_id,
        title=title,
        price_czk=parse_price_czk(str(row.get("priceText", ""))),
        bids_count=parse_bid_value(str(row.get("cardText", ""))),
        url=href,
    )


def extract_listings(
    driver: webdriver.Chrome,
    *,
    max_scroll_passes: int,
) -> list[CoinListing]:
    """Collect organic cards, including cards rendered lazily while scrolling."""
    original_scroll_y = int(driver.execute_script("return window.scrollY") or 0)
    driver.execute_script(
        "document.querySelector(arguments[0]).scrollIntoView({block: 'start'});",
        LISTING_ROOT_SELECTOR,
    )

    collected: dict[str, CoinListing] = {}
    rejected: set[str] = set()
    unchanged_passes = 0

    try:
        for pass_number in range(max_scroll_passes):
            wait_for_dom_quiet(driver)
            before = len(collected)

            for row in _raw_card_rows(driver):
                try:
                    listing = parse_card(row)
                except (TypeError, ValueError) as exc:
                    rejection_key = f"{row.get('href', '')}: {exc}"
                    if rejection_key not in rejected:
                        rejected.add(rejection_key)
                        LOGGER.warning("Rejected a result-card candidate: %s", rejection_key)
                    continue
                collected[listing.id] = listing

            unchanged_passes = unchanged_passes + 1 if len(collected) == before else 0
            metrics = cast(
                dict[str, object],
                driver.execute_script(
                    """
                    const root = document.querySelector(arguments[0]);
                    const rect = root.getBoundingClientRect();
                    const bottom = window.scrollY + rect.bottom;
                    return {
                      y: window.scrollY,
                      viewport: window.innerHeight,
                      rootBottom: bottom
                    };
                    """,
                    LISTING_ROOT_SELECTOR,
                ),
            )
            reached_bottom = (
                float(metrics["y"]) + float(metrics["viewport"])
                >= float(metrics["rootBottom"]) - 8
            )
            if reached_bottom and unchanged_passes >= 2:
                break

            driver.execute_script(
                "window.scrollBy({top: Math.max(400, window.innerHeight * 0.8), "
                "behavior: 'instant'});"
            )
            LOGGER.debug(
                "Scroll pass %d: %d unique listings", pass_number + 1, len(collected)
            )
    finally:
        # Avoid leaving the user's attached interactive browser at the page bottom.
        try:
            driver.execute_script("window.scrollTo(0, arguments[0]);", original_scroll_y)
        except WebDriverException:
            LOGGER.debug("Could not restore the original scroll position")

    # dict preserves the visual discovery order across lazy-render passes.
    return list(collected.values())


def write_json_atomic(path: Path, listings: list[CoinListing]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = [asdict(listing) for listing in listings]
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def print_card_debug(driver: webdriver.Chrome, html_path: Path | None) -> None:
    driver.execute_script(
        "document.querySelector(arguments[0])?.scrollIntoView({block: 'start'});",
        LISTING_ROOT_SELECTOR,
    )
    wait_for_dom_quiet(driver)
    debug = driver.execute_script(DEBUG_CARD_JS, LISTING_ROOT_SELECTOR)
    if not debug:
        LOGGER.error("No organic result card is available for a DOM debug dump")
        return
    print("\n--- FIRST ORGANIC CARD: ANCESTOR CHAIN ---", file=sys.stderr)
    print(json.dumps(debug["ancestors"], ensure_ascii=False, indent=2), file=sys.stderr)
    print("\n--- FIRST ORGANIC CARD: OUTER HTML ---", file=sys.stderr)
    print(debug["outerHTML"], file=sys.stderr)
    if html_path is not None:
        html_path.write_text(str(debug["outerHTML"]), encoding="utf-8")
        LOGGER.info("Card HTML saved to %s", html_path.resolve())


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debugger-address", default=DEFAULT_DEBUGGER_ADDRESS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--details-output",
        type=Path,
        default=DEFAULT_DETAILS_OUTPUT,
        help="Output JSON for listings enriched with offer details",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Save only the listing page, without fetching offer details",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Fetch details for only the first N listings (default: all)",
    )
    parser.add_argument(
        "--detail-delay",
        type=float,
        default=1.0,
        help="Seconds between offer detail requests",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--max-scroll-passes",
        type=int,
        default=40,
        help="Maximum lazy-render scroll passes over the current results page",
    )
    parser.add_argument(
        "--debug-card",
        action="store_true",
        help="Print the first organic card's ancestors and outerHTML to stderr",
    )
    parser.add_argument(
        "--debug-html",
        type=Path,
        help="Additionally save the first organic card's outerHTML to this file",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def run_detail_scraper(args: argparse.Namespace) -> int:
    """Run the detail CLI with the same Python interpreter and propagate failures."""
    command = [
        sys.executable,
        str(Path(__file__).resolve().with_name("aukro_details.py")),
        "--input", str(args.output.resolve()),
        "--output", str(args.details_output.resolve()),
        "--timeout", str(args.timeout),
        "--delay", str(args.detail_delay),
    ]
    if args.limit is not None:
        command.extend(["--limit", str(args.limit)])
    LOGGER.info("Fetching offer details; output: %s", args.details_output.resolve())
    return subprocess.run(command, check=False).returncode


def main() -> int:
    args = build_argument_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    if args.timeout <= 0 or args.max_scroll_passes <= 0:
        LOGGER.error("--timeout and --max-scroll-passes must be positive")
        return 2
    if (args.limit is not None and args.limit <= 0) or args.detail_delay < 0:
        LOGGER.error("--limit must be positive and --detail-delay cannot be negative")
        return 2
    if not args.list_only and args.output.resolve() == args.details_output.resolve():
        LOGGER.error("--output and --details-output must be different files")
        return 2

    try:
        driver = attach_to_chrome(args.debugger_address, args.timeout)
        LOGGER.info("Attached to %s", driver.current_url)
        ensure_page_is_active(driver, args.timeout)
        wait_for_listing_root(driver, args.timeout)
        listings = extract_listings(
            driver,
            max_scroll_passes=args.max_scroll_passes,
        )
        if args.debug_card or args.debug_html:
            print_card_debug(driver, args.debug_html)
        if not listings:
            raise ExtractionError(
                "The main results component was found, but no valid organic cards "
                "were extracted. Re-run with --debug-card --debug-html "
                "aukro_card_debug.html and inspect the reported DOM."
            )
        write_json_atomic(args.output, listings)
        LOGGER.info("Saved %d listings to %s", len(listings), args.output.resolve())
        return 0 if args.list_only else run_detail_scraper(args)
    except (ExtractionError, WebDriverException, OSError) as exc:
        LOGGER.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.error("Interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
