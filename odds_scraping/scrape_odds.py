"""
scrape_odds.py
--------------
Scrape NHL award trophy odds from FanDuel Sportsbook using zendriver.

Markets scraped (top 5 per market):
    - Art Ross Trophy   (most regular-season points)
    - Hart Trophy       (most valuable player)
    - Vezina Trophy     (best goaltender)
    - Norris Trophy     (best defenseman)

Data is appended to a JSONL file (one JSON object per line) so that
running the script daily builds a time-series of odds.

Usage:
    python scrape_odds.py                # scrape, print, and append to JSONL
    python scrape_odds.py --no-headless  # run with visible browser (debugging)
    python scrape_odds.py --dry-run      # scrape and print only, don't save

Requires: zendriver, Chrome browser
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

import zendriver as zd


# ── Configuration ────────────────────────────────────────────────────────────
FANDUEL_NHL_AWARDS_URL = "https://md.sportsbook.fanduel.com/navigation/nhl?tab=awards"
FANDUEL_NHL_STANLEY_CUP_URL = "https://md.sportsbook.fanduel.com/navigation/nhl?tab=stanley-cup"

MARKETS = {
    "Art Ross Trophy":  {"marker": "Art Ross Trophy 2025-26 - Winner",  "subtitle_skip": ["Regular Season", "Points Leader"]},
    "Hart Trophy":      {"marker": "Hart Trophy 2025-26 - Winner",      "subtitle_skip": ["Most Valuable", "MVP"]},
    "Vezina Trophy":    {"marker": "Vezina Trophy 2025-26 - Winner",    "subtitle_skip": ["Best Goaltender", "Goaltender"]},
    "Norris Trophy":    {"marker": "Norris Trophy 2025-26 - Winner",    "subtitle_skip": ["Best Defenseman", "Defenseman"]},
}

TOP_N = 5
TOP_N_STANLEY_CUP = 16

STANLEY_CUP_MARKET_NAME = "Stanley Cup"
STANLEY_CUP_MARKET_MARKER = "Stanley Cup 2025-26 - Winner"

JSONL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "odds_history.jsonl")


# ── Change detection helpers ─────────────────────────────────────────────────

def _normalize_markets(markets: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Normalize scraped markets so comparisons are stable across runs."""
    normalized: dict[str, list[dict]] = {}
    for market_name, players in (markets or {}).items():
        norm_players = []
        for p in players or []:
            norm_players.append(
                {
                    "player": p.get("player"),
                    "odds": p.get("odds"),
                    "odds_int": p.get("odds_int"),
                }
            )
        norm_players.sort(key=lambda x: (x.get("player") or ""))
        normalized[market_name] = norm_players
    return dict(sorted(normalized.items(), key=lambda x: x[0]))


def _load_last_markets_from_jsonl(path: str = JSONL_FILE) -> dict[str, list[dict]] | None:
    """Load the most recent JSONL record's markets, if available."""
    if not os.path.exists(path):
        return None

    last_line = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                last_line = line

    if not last_line:
        return None

    try:
        rec = json.loads(last_line)
    except json.JSONDecodeError:
        return None

    markets = rec.get("markets")
    if not isinstance(markets, dict):
        return None
    return markets


# ── Odds math ────────────────────────────────────────────────────────────────

def american_odds_to_implied_prob(odds: int) -> float:
    """Convert American odds to implied probability (no-vig not applied)."""
    if odds > 0:
        return 100.0 / (odds + 100.0)
    else:
        return abs(odds) / (abs(odds) + 100.0)


# ── Page-level text extraction helpers ───────────────────────────────────────

def _extract_market_block(lines: list[str], marker: str) -> list[str]:
    """
    Given all page lines, extract the block belonging to one market.

    The block starts at the line containing `marker` and ends at the first
    date footer (e.g. "APR 17, 12:00AM ET") which marks the end of each
    FanDuel market section.
    """
    start = None
    for i, line in enumerate(lines):
        if marker in line:
            start = i
            break

    if start is None:
        return []

    # Find end: date footer marks end of each market section
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if any(line.startswith(month) for month in ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                                                     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]):
            end = i
            break

    return lines[start:end]


def _parse_players_from_block(block: list[str], subtitle_skip: list[str], top_n: int) -> list[dict]:
    """
    Parse player name / odds pairs from a market text block.

    Returns up to `top_n` entries:
        [{"player": str, "odds": str, "odds_int": int, "implied_prob": float}, ...]
    """
    results = []

    for line in block:
        # Skip the trophy heading itself
        if "Trophy" in line:
            continue
        # Skip market heading lines like "... - Winner" (e.g. Stanley Cup)
        if " - Winner" in line:
            continue
        # Skip subtitle / description lines
        if any(skip in line for skip in subtitle_skip):
            continue
        # Skip "Show More" links
        if line.lower().startswith("show"):
            continue

        # Check if line is an odds value
        cleaned = line.replace(",", "")
        if cleaned.startswith("+") or cleaned.startswith("-"):
            try:
                odds_val = int(cleaned)
                if results and "odds_int" not in results[-1]:
                    results[-1]["odds"] = line
                    results[-1]["odds_int"] = odds_val
                    results[-1]["implied_prob"] = round(
                        american_odds_to_implied_prob(odds_val), 4
                    )
                continue
            except ValueError:
                pass

        # Otherwise treat as player name
        if len(line) > 3 and any(c.isalpha() for c in line):
            results.append({"player": line})

    # Keep only entries that have odds attached, limit to top_n
    results = [r for r in results if "odds_int" in r]
    return results[:top_n]


# ── Main scraping logic ─────────────────────────────────────────────────────

async def scrape_all_markets(headless: bool = True) -> dict[str, list[dict]]:
    """
    Load the FanDuel NHL Awards page once and scrape all configured markets.

    Returns: {"Art Ross Trophy": [...], "Hart Trophy": [...], ...}
    """
    print(f"  Launching {'headless ' if headless else ''}Chrome browser...")
    browser = await zd.start(headless=headless)

    try:
        scraped: dict[str, list[dict]] = {}

        # ── Awards page ─────────────────────────────────────────────────────
        print(f"  Loading {FANDUEL_NHL_AWARDS_URL} ...")
        page = await browser.get(FANDUEL_NHL_AWARDS_URL)

        print("  Waiting for page to render...")
        await asyncio.sleep(10)

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(3)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(3)

        full_text = await page.evaluate("document.body.innerText")
        if not any(cfg["marker"] in full_text for cfg in MARKETS.values()):
            print("  ERROR: Page loaded but no trophy markets found.")
            print("  The page may require login, geo-restriction, or the layout may have changed.")
            return {}

        lines = [line.strip() for line in full_text.split("\n") if line.strip()]
        for market_name, cfg in MARKETS.items():
            block = _extract_market_block(lines, cfg["marker"])
            if not block:
                print(f"    ⚠ Could not find section for {market_name}")
                scraped[market_name] = []
                continue
            players = _parse_players_from_block(block, cfg["subtitle_skip"], TOP_N)
            scraped[market_name] = players
            print(f"    ✓ {market_name}: {len(players)} players")

        # ── Stanley Cup page ────────────────────────────────────────────────
        print(f"  Loading {FANDUEL_NHL_STANLEY_CUP_URL} ...")
        cup_page = await browser.get(FANDUEL_NHL_STANLEY_CUP_URL)
        await cup_page
        await asyncio.sleep(10)
        await cup_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(2)

        # Expand the Winner market (FanDuel collapses after a few teams)
        try:
            show_more = await cup_page.find("Show more", best_match=True)
            await show_more.click()
            await asyncio.sleep(2)
            await cup_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)
        except Exception:
            pass

        cup_text = await cup_page.evaluate("document.body.innerText")
        cup_lines = [line.strip() for line in cup_text.split("\n") if line.strip()]

        cup_block = _extract_market_block(cup_lines, STANLEY_CUP_MARKET_MARKER)
        if not cup_block:
            print(f"    ⚠ Could not find section for {STANLEY_CUP_MARKET_NAME}")
            scraped[STANLEY_CUP_MARKET_NAME] = []
        else:
            cup_teams = _parse_players_from_block(cup_block, subtitle_skip=[], top_n=TOP_N_STANLEY_CUP)
            scraped[STANLEY_CUP_MARKET_NAME] = cup_teams
            print(f"    ✓ {STANLEY_CUP_MARKET_NAME}: {len(cup_teams)} teams")

        return scraped

    except Exception as e:
        print(f"  ERROR: {e}")
        return {}
    finally:
        await browser.stop()
        print("  Browser closed.")


# ── Storage ──────────────────────────────────────────────────────────────────

def append_to_jsonl(scraped: dict[str, list[dict]], path: str = JSONL_FILE):
    """Append a timestamped snapshot to the JSONL history file."""
    record = {
        "scraped_at": datetime.now().isoformat(),
        "source": "FanDuel Sportsbook",
        "url": FANDUEL_NHL_AWARDS_URL,
        "markets": scraped,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    print(f"  ✓ Appended snapshot to {path}")


def append_to_jsonl_if_changed(scraped: dict[str, list[dict]], path: str = JSONL_FILE) -> bool:
    """Append snapshot only if the odds payload differs from the most recent saved snapshot."""
    last_markets = _load_last_markets_from_jsonl(path)

    if last_markets is not None:
        if _normalize_markets(last_markets) == _normalize_markets(scraped):
            print("  ✓ No change detected vs last snapshot — skipping write.")
            return False

    append_to_jsonl(scraped, path)
    return True


# ── Display ──────────────────────────────────────────────────────────────────

def print_market(market_name: str, players: list[dict]):
    """Pretty-print a single market's odds."""
    if not players:
        print(f"\n  {market_name}: no data")
        return
    print(f"\n  {market_name}")
    print(f"  {'Player':<24} {'Odds':>8}  {'Implied %':>9}")
    print("  " + "─" * 46)
    for r in players:
        print(f"  {r['player']:<24} {r['odds']:>8}  {r['implied_prob']*100:>8.2f}%")


def print_all_results(scraped: dict[str, list[dict]]):
    """Pretty-print all scraped markets."""
    if not scraped:
        print("\n  No odds data found.")
        return

    print()
    print("=" * 56)
    print("  NHL TROPHY ODDS — FanDuel Sportsbook")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 56)

    for market_name, players in scraped.items():
        print_market(market_name, players)

    print()


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape NHL trophy odds from FanDuel Sportsbook."
    )
    parser.add_argument(
        "--no-headless", action="store_true",
        help="Run Chrome with a visible window (useful for debugging)."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scrape and print only — do not append to JSONL history."
    )
    return parser.parse_args()


def main():
    args = parse_args()
    headless = not args.no_headless

    print()
    print("=" * 56)
    print("  SCRAPING NHL TROPHY ODDS — FanDuel")
    print("=" * 56)
    print()

    scraped = asyncio.run(scrape_all_markets(headless=headless))
    print_all_results(scraped)

    total = sum(len(v) for v in scraped.values())
    if total == 0:
        print("  WARNING: No data scraped. Try --no-headless to debug visually.")
        return

    if not args.dry_run:
        append_to_jsonl_if_changed(scraped)
    else:
        print("  (dry-run: not saving to history)")

    print(f"  Done — {total} players across {len(scraped)} markets.")
    print()


if __name__ == "__main__":
    main()
