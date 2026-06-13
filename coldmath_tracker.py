#!/usr/bin/env python3
"""Track ColdMath's public Polymarket weather activity."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_WALLET = "0x594edb9112f526fa6a80b8f858a6379c8a2c1c11"
DATA_API = "https://data-api.polymarket.com"
POLYMARKET_MARKET_BASE = "https://polymarket.com/event"
STATE_FILE = Path(".coldmath_seen.json")


@dataclass(frozen=True)
class Event:
    timestamp: int
    type: str
    side: str
    title: str
    outcome: str
    price: float
    size: float
    usdc_size: float
    slug: str
    event_slug: str
    transaction_hash: str

    @property
    def id(self) -> str:
        return f"{self.transaction_hash}:{self.type}:{self.slug}:{self.outcome}:{self.size}"


def request_json(path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    url = f"{DATA_API}{path}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "coldmath-weather-tracker/1.0"})
    try:
        with urlopen(request, timeout=20) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        raise SystemExit(f"Polymarket API returned HTTP {exc.code}: {url}") from exc
    except URLError as exc:
        raise SystemExit(f"Could not reach Polymarket API: {exc.reason}") from exc

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Polymarket API returned invalid JSON: {payload[:200]}") from exc

    if not isinstance(data, list):
        raise SystemExit(f"Polymarket API returned an unexpected response: {data}")
    return data


def money(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_event(item: dict[str, Any]) -> Event:
    size = money(item.get("size"))
    price = money(item.get("price"))
    usdc_size = money(item.get("usdcSize"))
    if not usdc_size and price and size:
        usdc_size = price * size
    return Event(
        timestamp=int(item.get("timestamp") or 0),
        type=str(item.get("type") or "TRADE"),
        side=str(item.get("side") or ""),
        title=str(item.get("title") or ""),
        outcome=str(item.get("outcome") or ""),
        price=price,
        size=size,
        usdc_size=usdc_size,
        slug=str(item.get("slug") or ""),
        event_slug=str(item.get("eventSlug") or ""),
        transaction_hash=str(item.get("transactionHash") or ""),
    )


def is_weather(item: dict[str, Any]) -> bool:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("title", "slug", "eventSlug")
    ).lower()
    markers = (
        "temperature",
        "weather",
        "rain",
        "snow",
        "hurricane",
        "tornado",
        "wind",
        "nyc",
        "moscow",
        "cape-town",
        "ankara",
    )
    return any(marker in text for marker in markers)


def fetch_activity(wallet: str, limit: int, offset: int, trades_only: bool) -> list[Event]:
    path = "/trades" if trades_only else "/activity"
    raw = request_json(path, {"user": wallet, "limit": limit, "offset": offset})
    return [parse_event(item) for item in raw if is_weather(item)]


def fetch_positions(wallet: str, limit: int, open_only: bool) -> list[dict[str, Any]]:
    raw = request_json("/positions", {"user": wallet, "limit": limit})
    positions = [item for item in raw if is_weather(item)]
    if open_only:
        positions = [
            item
            for item in positions
            if money(item.get("currentValue")) > 0
            and not item.get("redeemable")
        ]
    return positions


def format_time(timestamp: int) -> str:
    if not timestamp:
        return ""
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def market_url(event: Event) -> str:
    slug = event.event_slug or event.slug
    return f"{POLYMARKET_MARKET_BASE}/{slug}" if slug else ""


def print_events(events: list[Event]) -> None:
    if not events:
        print("No matching weather activity found.")
        return

    for event in sorted(events, key=lambda e: e.timestamp, reverse=True):
        action = " ".join(part for part in (event.side, event.outcome) if part).strip()
        if not action:
            action = event.type
        print(f"{format_time(event.timestamp)}  {event.type:<6} {action}")
        print(f"  {event.title}")
        print(f"  size={event.size:g}  price={event.price:g}  usdc={event.usdc_size:g}")
        url = market_url(event)
        if url:
            print(f"  {url}")
        print()


def write_csv(events: list[Event], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "time",
                "type",
                "side",
                "outcome",
                "price",
                "size",
                "usdc_size",
                "title",
                "market_url",
                "transaction_hash",
            ]
        )
        for event in sorted(events, key=lambda e: e.timestamp, reverse=True):
            writer.writerow(
                [
                    format_time(event.timestamp),
                    event.type,
                    event.side,
                    event.outcome,
                    event.price,
                    event.size,
                    event.usdc_size,
                    event.title,
                    market_url(event),
                    event.transaction_hash,
                ]
            )


def print_positions(positions: list[dict[str, Any]]) -> None:
    if not positions:
        print("No matching weather positions found.")
        return

    for position in positions:
        title = position.get("title") or ""
        outcome = position.get("outcome") or ""
        size = money(position.get("size"))
        avg_price = money(position.get("avgPrice"))
        cur_price = money(position.get("curPrice"))
        current_value = money(position.get("currentValue"))
        cash_pnl = money(position.get("cashPnl"))
        print(f"{title}")
        print(f"  outcome={outcome}  size={size:g}  avg={avg_price:g}  current={cur_price:g}")
        print(f"  value={current_value:g}  cash_pnl={cash_pnl:g}")
        slug = position.get("eventSlug") or position.get("slug")
        if slug:
            print(f"  {POLYMARKET_MARKET_BASE}/{slug}")
        print()


def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return set(data if isinstance(data, list) else [])


def save_seen(path: Path, ids: set[str]) -> None:
    path.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")


def watch(args: argparse.Namespace) -> None:
    seen = load_seen(args.state_file)
    print(f"Watching {args.wallet} every {args.interval}s. Press Ctrl-C to stop.")
    try:
        while True:
            events = fetch_activity(args.wallet, args.limit, 0, args.trades_only)
            new_events = [event for event in events if event.id not in seen]
            if new_events:
                print_events(new_events)
                seen.update(event.id for event in new_events)
                save_seen(args.state_file, seen)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Track ColdMath's Polymarket weather activity.",
    )
    parser.add_argument("--wallet", default=DEFAULT_WALLET)
    subparsers = parser.add_subparsers(dest="command", required=True)

    recent = subparsers.add_parser("recent", help="Print recent weather activity")
    recent.add_argument("--limit", type=int, default=25)
    recent.add_argument("--offset", type=int, default=0)
    recent.add_argument("--trades-only", action="store_true")
    recent.add_argument("--csv", type=Path, help="Write results to a CSV file")

    positions = subparsers.add_parser("positions", help="Print current weather positions")
    positions.add_argument("--limit", type=int, default=50)
    positions.add_argument("--open-only", action="store_true")

    watch_cmd = subparsers.add_parser("watch", help="Poll for new weather activity")
    watch_cmd.add_argument("--limit", type=int, default=25)
    watch_cmd.add_argument("--interval", type=int, default=60)
    watch_cmd.add_argument("--trades-only", action="store_true")
    watch_cmd.add_argument("--state-file", type=Path, default=STATE_FILE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "recent":
        events = fetch_activity(args.wallet, args.limit, args.offset, args.trades_only)
        print_events(events)
        if args.csv:
            write_csv(events, args.csv)
            print(f"Wrote {len(events)} rows to {args.csv}")
    elif args.command == "positions":
        print_positions(fetch_positions(args.wallet, args.limit, args.open_only))
    elif args.command == "watch":
        watch(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
