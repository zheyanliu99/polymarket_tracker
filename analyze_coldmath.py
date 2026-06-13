#!/usr/bin/env python3
"""Pull and analyze ColdMath's Polymarket trading history."""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


WALLET = "0x594edb9112f526fa6a80b8f858a6379c8a2c1c11"
DATA_API = "https://data-api.polymarket.com"
LOCAL_TZ = ZoneInfo("America/New_York")
MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
WEATHER_PATTERNS = (
    re.compile(r"\b(highest|lowest) temperature\b", re.I),
    re.compile(r"\btemperature\b", re.I),
    re.compile(r"\b(rain|rainfall|snow|hurricane|tropical storm|wind speed|weather)\b", re.I),
)


@dataclass(frozen=True)
class ClassifiedTrade:
    raw: dict[str, Any]
    timestamp: int
    dt: datetime
    date: str
    month: str
    title: str
    slug: str
    event_slug: str
    side: str
    outcome: str
    price: float
    size: float
    notional: float
    market_url: str
    is_weather: bool
    category: str
    city: str
    weather_metric: str
    market_date: str
    lead_days: int | None


def request_json(path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    url = f"{DATA_API}{path}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "coldmath-weather-analysis/1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach {url}: {exc.reason}") from exc

    data = json.loads(payload)
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected API response from {url}: {data}")
    return data


def fetch_paginated(
    path: str,
    wallet: str,
    cutoff_ts: int | None,
    limit: int = 1000,
    sleep_seconds: float = 0.12,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch = request_json(path, {"user": wallet, "limit": limit, "offset": offset})
        if not batch:
            break
        rows.extend(batch)
        oldest = min(int(item.get("timestamp") or 0) for item in batch)
        print(f"{path} offset={offset} rows={len(batch)} oldest={oldest}")
        if cutoff_ts is not None and oldest < cutoff_ts:
            break
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(sleep_seconds)
    if cutoff_ts is None:
        return rows
    return [item for item in rows if int(item.get("timestamp") or 0) >= cutoff_ts]


def fetch_current_positions(wallet: str, limit: int = 500) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch = request_json(
            "/positions",
            {"user": wallet, "limit": limit, "offset": offset, "sizeThreshold": 0},
        )
        if not batch:
            break
        rows.extend(batch)
        print(f"/positions offset={offset} rows={len(batch)}")
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(0.12)
    return rows


def fetch_closed_positions(wallet: str, cutoff_ts: int, limit: int = 50) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch = request_json(
            "/closed-positions",
            {
                "user": wallet,
                "limit": limit,
                "offset": offset,
                "sortBy": "TIMESTAMP",
                "sortDirection": "DESC",
            },
        )
        if not batch:
            break
        rows.extend(batch)
        timestamps = [int(item.get("timestamp") or 0) for item in batch]
        oldest = min(timestamps) if timestamps else 0
        print(f"/closed-positions offset={offset} rows={len(batch)} oldest={oldest}")
        if oldest < cutoff_ts:
            break
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(0.12)
    return [item for item in rows if int(item.get("timestamp") or 0) >= cutoff_ts]


def should_include_position(item: dict[str, Any], cutoff: datetime) -> bool:
    timestamp = int(item.get("timestamp") or 0)
    if timestamp:
        return timestamp >= int(cutoff.timestamp())
    end_date = str(item.get("endDate") or "")[:10]
    if end_date:
        try:
            return datetime.strptime(end_date, "%Y-%m-%d").date() >= cutoff.date()
        except ValueError:
            return True
    return True


def discover_condition_ids(
    current_positions: list[dict[str, Any]],
    closed_positions: list[dict[str, Any]],
    cutoff: datetime,
) -> list[str]:
    condition_ids = {
        str(item.get("conditionId") or "")
        for item in [*current_positions, *closed_positions]
        if should_include_position(item, cutoff)
        and is_weather_market(item)
        and item.get("conditionId")
    }
    return sorted(condition_ids)


def dedupe_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    deduped = []
    for row in rows:
        key = (
            row.get("transactionHash"),
            row.get("conditionId"),
            row.get("asset"),
            row.get("side"),
            row.get("outcome"),
            row.get("type"),
            row.get("timestamp"),
            row.get("size"),
            row.get("price"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def fetch_market_rows(
    path: str,
    wallet: str,
    condition_ids: list[str],
    cutoff_ts: int,
    *,
    batch_size: int = 8,
    limit: int = 500,
    extra_params: dict[str, Any] | None = None,
    checkpoint_path: Path | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    extra_params = extra_params or {}
    processed_ids: set[str] = set()

    if checkpoint_path and checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        rows = checkpoint.get("rows", [])
        processed_ids = set(checkpoint.get("processed_condition_ids", []))
        print(
            f"Resuming {path}: processed={len(processed_ids)} rows={len(rows)}",
            flush=True,
        )

    def fetch_batch(batch_ids: list[str]) -> list[dict[str, Any]]:
        batch_rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            params = {
                "user": wallet,
                "market": ",".join(batch_ids),
                "limit": limit,
                "offset": offset,
                **extra_params,
            }
            try:
                page = request_json(path, params)
            except RuntimeError:
                if len(batch_ids) == 1:
                    raise
                midpoint = len(batch_ids) // 2
                return fetch_batch(batch_ids[:midpoint]) + fetch_batch(batch_ids[midpoint:])
            if not page:
                break
            batch_rows.extend(page)
            timestamps = [int(item.get("timestamp") or 0) for item in page]
            oldest = min(timestamps) if timestamps else 0
            if oldest < cutoff_ts or len(page) < limit:
                break
            offset += limit
            time.sleep(0.08)
        return [item for item in batch_rows if int(item.get("timestamp") or 0) >= cutoff_ts]

    for index in range(0, len(condition_ids), batch_size):
        batch_ids = condition_ids[index:index + batch_size]
        if all(condition_id in processed_ids for condition_id in batch_ids):
            continue
        batch_rows = fetch_batch(batch_ids)
        rows.extend(batch_rows)
        processed_ids.update(batch_ids)
        print(
            f"{path} markets={index + len(batch_ids)}/{len(condition_ids)} "
            f"rows={len(batch_rows)} total={len(rows)}",
            flush=True,
        )
        if checkpoint_path and ((index // batch_size) % 25 == 0):
            write_json(
                checkpoint_path,
                {
                    "processed_condition_ids": sorted(processed_ids),
                    "rows": rows,
                },
            )
        time.sleep(0.08)
    deduped = dedupe_rows(rows)
    if checkpoint_path:
        write_json(
            checkpoint_path,
            {
                "processed_condition_ids": sorted(processed_ids),
                "rows": deduped,
            },
        )
    return deduped


def as_float(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def is_weather_market(item: dict[str, Any]) -> bool:
    text = " ".join(str(item.get(key) or "") for key in ("title", "slug", "eventSlug")).lower()
    return any(pattern.search(text) for pattern in WEATHER_PATTERNS)


def category_for(title: str, slug: str) -> tuple[str, str]:
    text = f"{title} {slug}".lower()
    if "highest temperature" in text:
        metric = "high"
        category = "temperature_high"
    elif "lowest temperature" in text:
        metric = "low"
        category = "temperature_low"
    elif any(word in text for word in ("rain", "rainfall")):
        metric = "precipitation"
        category = "precipitation"
    elif any(word in text for word in ("hurricane", "tropical storm", "wind speed")):
        metric = "storm"
        category = "storm"
    else:
        metric = "other"
        category = "other_weather"

    if "between" in text:
        category += "_range"
    elif "or higher" in text or "or lower" in text:
        category += "_threshold"
    elif category.startswith("temperature"):
        category += "_exact"
    return category, metric


def city_for(title: str) -> str:
    match = re.search(r"temperature in (.+?) be\b", title, flags=re.I)
    if match:
        return match.group(1).strip()
    match = re.search(r"\bin ([A-Z][A-Za-z .'-]+?) on\b", title)
    if match:
        return match.group(1).strip()
    return "Unknown"


def market_date_for(slug: str, title: str) -> str:
    text = f"{slug} {title}".lower()
    match = re.search(
        r"on-(january|february|march|april|may|june|july|august|september|october|november|december)-(\d{1,2})-(\d{4})",
        text,
    )
    if match:
        month, day, year = match.groups()
        return f"{int(year):04d}-{MONTHS[month]:02d}-{int(day):02d}"

    match = re.search(
        r"on (january|february|march|april|may|june|july|august|september|october|november|december) (\d{1,2})",
        text,
    )
    if match:
        month, day = match.groups()
        return f"????-{MONTHS[month]:02d}-{int(day):02d}"
    return ""


def lead_days_for(trade_dt: datetime, market_date: str) -> int | None:
    if not re.match(r"\d{4}-\d{2}-\d{2}$", market_date):
        return None
    target = datetime.strptime(market_date, "%Y-%m-%d").date()
    return (target - trade_dt.date()).days


def classify_trade(item: dict[str, Any]) -> ClassifiedTrade:
    timestamp = int(item.get("timestamp") or 0)
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(LOCAL_TZ)
    title = str(item.get("title") or "")
    slug = str(item.get("slug") or "")
    event_slug = str(item.get("eventSlug") or "")
    price = as_float(item.get("price"))
    size = as_float(item.get("size"))
    notional = as_float(item.get("usdcSize"))
    if not notional and price and size:
        notional = price * size
    is_weather = is_weather_market(item)
    category, metric = category_for(title, slug) if is_weather else ("non_weather", "non_weather")
    market_date = market_date_for(event_slug or slug, title)
    return ClassifiedTrade(
        raw=item,
        timestamp=timestamp,
        dt=dt,
        date=dt.strftime("%Y-%m-%d"),
        month=dt.strftime("%Y-%m"),
        title=title,
        slug=slug,
        event_slug=event_slug,
        side=str(item.get("side") or ""),
        outcome=str(item.get("outcome") or ""),
        price=price,
        size=size,
        notional=notional,
        market_url=f"https://polymarket.com/event/{event_slug or slug}" if event_slug or slug else "",
        is_weather=is_weather,
        category=category,
        city=city_for(title) if is_weather else "Non-weather",
        weather_metric=metric,
        market_date=market_date,
        lead_days=lead_days_for(dt, market_date),
    )


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def write_trades_csv(path: Path, trades: Iterable[ClassifiedTrade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "datetime_et",
        "date_et",
        "month_et",
        "side",
        "outcome",
        "price",
        "size",
        "notional",
        "title",
        "slug",
        "event_slug",
        "market_url",
        "category",
        "city",
        "weather_metric",
        "market_date",
        "lead_days",
        "transaction_hash",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for trade in trades:
            writer.writerow(
                {
                    "datetime_et": trade.dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "date_et": trade.date,
                    "month_et": trade.month,
                    "side": trade.side,
                    "outcome": trade.outcome,
                    "price": f"{trade.price:.8g}",
                    "size": f"{trade.size:.8g}",
                    "notional": f"{trade.notional:.8g}",
                    "title": trade.title,
                    "slug": trade.slug,
                    "event_slug": trade.event_slug,
                    "market_url": trade.market_url,
                    "category": trade.category,
                    "city": trade.city,
                    "weather_metric": trade.weather_metric,
                    "market_date": trade.market_date,
                    "lead_days": "" if trade.lead_days is None else trade.lead_days,
                    "transaction_hash": trade.raw.get("transactionHash") or "",
                }
            )


def weighted_average(values: Iterable[tuple[float, float]]) -> float:
    total_weight = 0.0
    total = 0.0
    for value, weight in values:
        total += value * weight
        total_weight += weight
    return total / total_weight if total_weight else 0.0


def median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def pct(part: float, whole: float) -> float:
    return 100 * part / whole if whole else 0.0


def price_bucket(price: float) -> str:
    if price < 0.01:
        return "<1c"
    if price < 0.05:
        return "1-5c"
    if price < 0.20:
        return "5-20c"
    if price < 0.80:
        return "20-80c"
    if price < 0.95:
        return "80-95c"
    if price < 0.99:
        return "95-99c"
    return "99-100c"


def lead_bucket(lead_days: int | None) -> str:
    if lead_days is None:
        return "unknown"
    if lead_days < 0:
        return "after market date"
    if lead_days == 0:
        return "same day"
    if lead_days == 1:
        return "1 day before"
    if 2 <= lead_days <= 3:
        return "2-3 days before"
    if 4 <= lead_days <= 7:
        return "4-7 days before"
    return "8+ days before"


def aggregate_counter(
    trades: list[ClassifiedTrade],
    key_fn: Any,
    value_fn: Any = lambda trade: trade.notional,
) -> dict[str, float]:
    totals: defaultdict[str, float] = defaultdict(float)
    for trade in trades:
        totals[str(key_fn(trade))] += float(value_fn(trade))
    return dict(sorted(totals.items(), key=lambda item: item[1], reverse=True))


def top_rows(counter: dict[str, float], limit: int = 10, money_values: bool = True) -> str:
    lines = []
    for key, value in list(counter.items())[:limit]:
        rendered = f"${value:,.2f}" if money_values else f"{value:,.0f}"
        lines.append(f"| {key} | {rendered} |")
    return "\n".join(lines) if lines else "| none | 0 |"


def build_market_side_stats(trades: list[ClassifiedTrade]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[ClassifiedTrade]] = defaultdict(list)
    for trade in trades:
        if trade.side.upper() == "BUY":
            grouped[(trade.slug, trade.outcome)].append(trade)

    by_market: dict[str, dict[str, Any]] = defaultdict(lambda: {"outcomes": {}, "title": "", "event_slug": ""})
    for (slug, outcome), items in grouped.items():
        size = sum(item.size for item in items)
        notional = sum(item.notional for item in items)
        by_market[slug]["title"] = items[0].title
        by_market[slug]["event_slug"] = items[0].event_slug
        by_market[slug]["outcomes"][outcome or ""] = {
            "size": size,
            "notional": notional,
            "avg_price": notional / size if size else 0.0,
            "trades": len(items),
        }

    rows = []
    for slug, data in by_market.items():
        outcomes = data["outcomes"]
        if "Yes" in outcomes and "No" in outcomes:
            yes = outcomes["Yes"]
            no = outcomes["No"]
            matched = min(yes["size"], no["size"])
            complete_set_cost = yes["avg_price"] + no["avg_price"]
            rows.append(
                {
                    "slug": slug,
                    "event_slug": data["event_slug"],
                    "title": data["title"],
                    "yes_size": yes["size"],
                    "no_size": no["size"],
                    "matched_size": matched,
                    "yes_avg": yes["avg_price"],
                    "no_avg": no["avg_price"],
                    "complete_set_cost": complete_set_cost,
                    "edge_per_set": 1 - complete_set_cost,
                }
            )
    return sorted(rows, key=lambda row: row["matched_size"], reverse=True)


def build_event_stats(trades: list[ClassifiedTrade]) -> list[dict[str, Any]]:
    grouped: dict[str, list[ClassifiedTrade]] = defaultdict(list)
    for trade in trades:
        grouped[trade.event_slug or trade.slug].append(trade)
    rows = []
    for event_slug, items in grouped.items():
        rows.append(
            {
                "event_slug": event_slug,
                "notional": sum(item.notional for item in items),
                "trades": len(items),
                "markets": len({item.slug for item in items}),
                "outcomes": len({(item.slug, item.outcome) for item in items}),
                "first_trade": min(item.dt for item in items),
                "last_trade": max(item.dt for item in items),
                "title_example": items[0].title,
            }
        )
    return sorted(rows, key=lambda row: row["notional"], reverse=True)


def activity_summary(activity: list[dict[str, Any]]) -> dict[str, Any]:
    weather = [item for item in activity if is_weather_market(item)]
    by_type = Counter(str(item.get("type") or "") for item in weather)
    by_type_notional: defaultdict[str, float] = defaultdict(float)
    for item in weather:
        by_type_notional[str(item.get("type") or "")] += as_float(item.get("usdcSize"))
    return {
        "weather_activity_count": len(weather),
        "weather_activity_by_type": dict(by_type),
        "weather_activity_notional_by_type": dict(by_type_notional),
    }


def positions_summary(positions: list[dict[str, Any]], cutoff_date: datetime) -> dict[str, Any]:
    weather = [item for item in positions if is_weather_market(item)]
    recent_weather = []
    cutoff_d = cutoff_date.date()
    for item in weather:
        end_date = str(item.get("endDate") or "")
        try:
            include = datetime.strptime(end_date[:10], "%Y-%m-%d").date() >= cutoff_d
        except ValueError:
            include = True
        if include:
            recent_weather.append(item)
    return {
        "weather_positions_count": len(weather),
        "recent_weather_positions_count": len(recent_weather),
        "recent_initial_value": sum(as_float(item.get("initialValue")) for item in recent_weather),
        "recent_current_value": sum(as_float(item.get("currentValue")) for item in recent_weather),
        "recent_cash_pnl": sum(as_float(item.get("cashPnl")) for item in recent_weather),
        "recent_realized_pnl": sum(as_float(item.get("realizedPnl")) for item in recent_weather),
        "recent_redeemable_count": sum(1 for item in recent_weather if item.get("redeemable")),
    }


def closed_positions_summary(positions: list[dict[str, Any]]) -> dict[str, Any]:
    weather = [item for item in positions if is_weather_market(item)]
    non_weather = [item for item in positions if not is_weather_market(item)]
    negative_risk_known = sum(1 for item in weather if "negativeRisk" in item)
    negative_risk = sum(1 for item in weather if item.get("negativeRisk"))
    return {
        "weather_closed_positions": len(weather),
        "non_weather_closed_positions": len(non_weather),
        "weather_closed_markets": len({item.get("conditionId") for item in weather if item.get("conditionId")}),
        "weather_total_bought": sum(as_float(item.get("totalBought")) for item in weather),
        "non_weather_total_bought": sum(as_float(item.get("totalBought")) for item in non_weather),
        "weather_realized_pnl": sum(as_float(item.get("realizedPnl")) for item in weather),
        "weather_negative_risk_rows": negative_risk if negative_risk_known else None,
        "weather_non_negative_risk_rows": len(weather) - negative_risk if negative_risk_known else None,
    }


def write_report(
    path: Path,
    *,
    wallet: str,
    generated_at: datetime,
    cutoff: datetime,
    trades: list[ClassifiedTrade],
    activity: list[dict[str, Any]],
    current_positions: list[dict[str, Any]],
    closed_positions: list[dict[str, Any]],
    condition_ids: list[str],
    output_files: dict[str, Path],
) -> None:
    weather = [trade for trade in trades if trade.is_weather]
    buys = [trade for trade in weather if trade.side.upper() == "BUY"]
    sells = [trade for trade in weather if trade.side.upper() == "SELL"]
    weather_notional = sum(trade.notional for trade in weather)
    buy_notional = sum(trade.notional for trade in buys)
    sell_notional = sum(trade.notional for trade in sells)
    prices = [trade.price for trade in weather]
    notionals = [trade.notional for trade in weather]
    days = {trade.date for trade in weather}
    months = {trade.month for trade in weather}
    unique_markets = {trade.slug for trade in weather}
    unique_events = {trade.event_slug or trade.slug for trade in weather}

    by_price_bucket = aggregate_counter(weather, lambda trade: price_bucket(trade.price))
    by_price_bucket_count = aggregate_counter(weather, lambda trade: price_bucket(trade.price), lambda trade: 1)
    by_lead_bucket = aggregate_counter(weather, lambda trade: lead_bucket(trade.lead_days))
    by_lead_bucket_count = aggregate_counter(weather, lambda trade: lead_bucket(trade.lead_days), lambda trade: 1)
    by_category = aggregate_counter(weather, lambda trade: trade.category)
    by_city = aggregate_counter(weather, lambda trade: trade.city)
    by_month = dict(sorted(aggregate_counter(weather, lambda trade: trade.month).items()))
    by_outcome = aggregate_counter(weather, lambda trade: trade.outcome or "unknown")
    by_side = aggregate_counter(weather, lambda trade: trade.side or "unknown")

    both_sides = build_market_side_stats(weather)
    both_sides_positive = [row for row in both_sides if row["complete_set_cost"] < 1]
    event_stats = build_event_stats(weather)
    multi_market_events = [row for row in event_stats if row["markets"] >= 2]
    activity_stats = activity_summary(activity)
    pos_stats = positions_summary(current_positions, cutoff)
    closed_stats = closed_positions_summary(closed_positions)

    avg_price_weighted = weighted_average((trade.price, trade.notional) for trade in weather)
    median_price = median(prices)
    median_notional = median(notionals)
    avg_notional = statistics.mean(notionals) if notionals else 0.0

    high_conviction = [trade for trade in weather if trade.price >= 0.95]
    tail = [trade for trade in weather if trade.price <= 0.05]
    middle = [trade for trade in weather if 0.20 <= trade.price < 0.80]
    same_or_one_day = [
        trade
        for trade in weather
        if trade.lead_days is not None and 0 <= trade.lead_days <= 1
    ]

    report = []
    report.append("# ColdMath Polymarket Weather Trading Analysis\n")
    report.append(f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
    report.append(f"Wallet: `{wallet}`\n")
    report.append(
        f"Window: {cutoff.strftime('%Y-%m-%d %H:%M:%S %Z')} through "
        f"{generated_at.strftime('%Y-%m-%d %H:%M:%S %Z')}.\n"
    )
    report.append(
        "Data source: public Polymarket Data API endpoints `/trades`, `/activity`, and `/positions`. "
        "I used `/closed-positions` and `/positions` to discover weather condition IDs, then hydrated those markets with "
        "`/trades?market=...&takerOnly=false` and `/activity?market=...`. The API shows executed public fills and "
        "wallet-level events, not hidden orders, forecast inputs, or intent.\n"
    )

    report.append("## Executive Takeaways\n")
    report.append(
        f"- In the one-year weather pull, I discovered **{len(condition_ids):,} weather condition IDs** and hydrated "
        f"**{len(weather):,} weather trades** across **{len(unique_markets):,} markets**, with about "
        f"**${weather_notional:,.2f}** of executed notional.\n"
    )
    report.append(
        f"- ColdMath is overwhelmingly a **temperature-market specialist** in this data: "
        f"the top weather categories are dominated by highest/lowest temperature exact, range, and threshold contracts.\n"
    )
    report.append(
        f"- His style is barbell-like: **cheap tail tickets** plus **near-certain late fills**. "
        f"Trades priced at <=5c account for **{len(tail):,} fills / ${sum(t.notional for t in tail):,.2f}**, "
        f"while trades at >=95c account for **{len(high_conviction):,} fills / ${sum(t.notional for t in high_conviction):,.2f}**.\n"
    )
    report.append(
        f"- He rarely behaves like a discretionary mid-price bettor: 20-80c weather trades were "
        f"**{len(middle):,} fills / ${sum(t.notional for t in middle):,.2f}**.\n"
    )
    report.append(
        f"- Timing is short-horizon. Trades on the market date or one day before account for "
        f"**{len(same_or_one_day):,} fills / ${sum(t.notional for t in same_or_one_day):,.2f}**.\n"
    )
    report.append(
        f"- I found **{len(both_sides):,} weather markets where he bought both Yes and No**, including "
        f"**{len(both_sides_positive):,}** where the aggregate average complete-set cost was below $1 before fees. "
        "That is the clearest evidence of conversion/negative-risk/settlement-aware behavior rather than simple directional copying.\n"
    )
    report.append(
        f"- The one-year closed-position snapshot also shows **{closed_stats['weather_closed_positions']:,} weather outcome rows** "
        f"with **${closed_stats['weather_total_bought']:,.2f} totalBought** and "
        f"**${closed_stats['weather_realized_pnl']:,.2f} realizedPnl** in the API fields. Treat that as directional accounting context, "
        "not audited net income.\n"
    )
    if closed_stats["non_weather_closed_positions"]:
        closed_total_bought = closed_stats["weather_total_bought"] + closed_stats["non_weather_total_bought"]
        report.append(
            f"- He is **not literally weather-only** in the one-year position data: I found "
            f"**{closed_stats['non_weather_closed_positions']:,} non-weather closed-position rows**. "
            f"That said, weather was **{pct(closed_stats['weather_total_bought'], closed_total_bought):.1f}%** "
            "of closed-position `totalBought`, so the repeatable specialization is still clearly weather.\n"
        )

    report.append("\n## Core Metrics\n")
    report.append("| Metric | Value |\n|---|---:|\n")
    report.append(f"| Weather trades | {len(weather):,} |\n")
    report.append(f"| Weather condition IDs discovered | {len(condition_ids):,} |\n")
    report.append(f"| Weather notional | ${weather_notional:,.2f} |\n")
    report.append(f"| Unique weather markets | {len(unique_markets):,} |\n")
    report.append(f"| Unique weather event groups | {len(unique_events):,} |\n")
    report.append(f"| Active weather trading days | {len(days):,} |\n")
    report.append(f"| Active weather trading months | {len(months):,} |\n")
    report.append(f"| Buy notional | ${buy_notional:,.2f} |\n")
    report.append(f"| Sell notional | ${sell_notional:,.2f} |\n")
    report.append(f"| Median weather price | {median_price:.4f} |\n")
    report.append(f"| Notional-weighted avg weather price | {avg_price_weighted:.4f} |\n")
    report.append(f"| Median weather trade notional | ${median_notional:,.2f} |\n")
    report.append(f"| Average weather trade notional | ${avg_notional:,.2f} |\n")

    report.append("\n## Price Behavior\n")
    report.append("| Price bucket | Notional |\n|---|---:|\n")
    report.append(top_rows(by_price_bucket, limit=10))
    report.append("\n\n| Price bucket | Fills |\n|---|---:|\n")
    report.append(top_rows(by_price_bucket_count, limit=10, money_values=False))
    report.append("\n")

    report.append("\n## Timing vs Weather Date\n")
    report.append("| Lead bucket | Notional |\n|---|---:|\n")
    report.append(top_rows(by_lead_bucket, limit=10))
    report.append("\n\n| Lead bucket | Fills |\n|---|---:|\n")
    report.append(top_rows(by_lead_bucket_count, limit=10, money_values=False))
    report.append("\n")

    report.append("\n## Market Selection\n")
    report.append("| Category | Notional |\n|---|---:|\n")
    report.append(top_rows(by_category, limit=12))
    report.append("\n\n| City / location | Notional |\n|---|---:|\n")
    report.append(top_rows(by_city, limit=15))
    report.append("\n\n| Outcome | Notional |\n|---|---:|\n")
    report.append(top_rows(by_outcome, limit=10))
    report.append("\n\n| Side | Notional |\n|---|---:|\n")
    report.append(top_rows(by_side, limit=10))
    report.append("\n")

    report.append("\n## Monthly Weather Notional\n")
    report.append("| Month | Notional |\n|---|---:|\n")
    for month, value in by_month.items():
        report.append(f"| {month} | ${value:,.2f} |\n")

    report.append("\n## Multi-Bucket / Both-Side Behavior\n")
    report.append(
        f"Weather event groups with two or more market buckets traded: **{len(multi_market_events):,}**. "
        "This matters because temperature markets often come as mutually related buckets. Trading multiple buckets is closer to pricing a distribution than making a single yes/no call.\n"
    )
    report.append("\nTop multi-market event groups by notional:\n")
    report.append("| Event | Notional | Fills | Markets |\n|---|---:|---:|---:|\n")
    for row in multi_market_events[:10]:
        report.append(
            f"| {row['event_slug']} | ${row['notional']:,.2f} | {row['trades']:,} | {row['markets']:,} |\n"
        )

    report.append("\nTop markets where he bought both Yes and No:\n")
    report.append("| Market | Matched size | Yes avg | No avg | Complete-set cost | Edge before fees |\n|---|---:|---:|---:|---:|---:|\n")
    for row in both_sides[:12]:
        report.append(
            f"| {row['slug']} | {row['matched_size']:,.2f} | {row['yes_avg']:.4f} | "
            f"{row['no_avg']:.4f} | {row['complete_set_cost']:.4f} | {row['edge_per_set']:.4f} |\n"
        )

    report.append("\n## Activity And Settlement Mechanics\n")
    report.append("| Activity type | Count | USDC size |\n|---|---:|---:|\n")
    for activity_type, count in sorted(activity_stats["weather_activity_by_type"].items()):
        notional = activity_stats["weather_activity_notional_by_type"].get(activity_type, 0.0)
        report.append(f"| {activity_type or 'unknown'} | {count:,} | ${notional:,.2f} |\n")
    report.append(
        "\nThe presence of large MERGE and REDEEM activity is important: it means some of his edge may come from correctly handling complete sets, resolution, and negative-risk mechanics, not only from choosing the right weather outcome.\n"
    )

    report.append("\n## Positions Snapshot\n")
    report.append("Closed/current positions are supporting context and can differ from a pure trade ledger because of merges, redeems, and open inventory.\n")
    report.append("| Metric | Value |\n|---|---:|\n")
    report.append(f"| Closed weather outcome rows | {closed_stats['weather_closed_positions']:,} |\n")
    report.append(f"| Closed weather markets | {closed_stats['weather_closed_markets']:,} |\n")
    report.append(f"| Closed-position totalBought | ${closed_stats['weather_total_bought']:,.2f} |\n")
    report.append(f"| Closed-position realizedPnl | ${closed_stats['weather_realized_pnl']:,.2f} |\n")
    if closed_stats["weather_negative_risk_rows"] is None:
        report.append("| Closed-position negative-risk rows | not reported by endpoint |\n")
    else:
        report.append(f"| Closed-position negative-risk rows | {closed_stats['weather_negative_risk_rows']:,} |\n")
    report.append(f"| Weather positions returned | {pos_stats['weather_positions_count']:,} |\n")
    report.append(f"| Weather positions ending after cutoff | {pos_stats['recent_weather_positions_count']:,} |\n")
    report.append(f"| Initial value | ${pos_stats['recent_initial_value']:,.2f} |\n")
    report.append(f"| Current value | ${pos_stats['recent_current_value']:,.2f} |\n")
    report.append(f"| Cash PnL field | ${pos_stats['recent_cash_pnl']:,.2f} |\n")
    report.append(f"| Realized PnL field | ${pos_stats['recent_realized_pnl']:,.2f} |\n")
    report.append(f"| Redeemable weather positions | {pos_stats['recent_redeemable_count']:,} |\n")

    report.append("\n## Inferred Trading Logic\n")
    report.append(
        "1. **He prices weather as a distribution.** The repeated use of adjacent exact/range/threshold temperature buckets suggests he is not just deciding whether one binary proposition is attractive. He is mapping forecast uncertainty into bucket probabilities.\n"
    )
    report.append(
        "2. **He likes short time-to-resolution markets.** Same-day and one-day-before activity dominates, which is when public forecast models, observed intraday temperatures, and exchange liquidity can diverge from stale market prices.\n"
    )
    report.append(
        "3. **He uses a barbell of tails and certainties.** Cheap <=5c buys look like mispriced tail/event-bucket optionality. >=95c buys look like late certainty harvesting or complete-set construction when the remaining uncertainty is tiny.\n"
    )
    report.append(
        "4. **He appears settlement-aware.** Buying both Yes and No in the same market, trading multiple related buckets in one event, then showing MERGE/REDEEM activity is consistent with negative-risk conversion or complete-set management. Blindly copying only the visible buy leg can miss the hedge.\n"
    )
    report.append(
        "5. **He usually does not need to sell to monetize.** Low sell notional relative to buy notional points toward holding through resolution, redeeming winners, merging complete sets, or treating contracts as settlement instruments.\n"
    )

    report.append("\n## How To Follow Without Overpaying\n")
    report.append(
        "- Separate trade types: copy-candidates are fresh `TRADE` events; `MERGE` and `REDEEM` are not new predictions.\n"
    )
    report.append(
        "- Do not chase after price movement. If he buys a 2c bucket and the market moves to 8c, your expected value may be completely different from his.\n"
    )
    report.append(
        "- Watch whether he is buying a single side or assembling both sides / multiple buckets. The latter may be an arb/conversion structure, not a directional bet.\n"
    )
    report.append(
        "- For weather markets, compare against the exact resolution source and time convention. A one-degree or one-hour rule detail can flip the edge.\n"
    )

    report.append("\n## Files Produced\n")
    for label, file_path in output_files.items():
        report.append(f"- {label}: `{file_path}`\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(report), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wallet", default=WALLET)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--out-dir", type=Path, default=Path("data"))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--refresh", action="store_true", help="Fetch fresh data even if cached files exist")
    args = parser.parse_args()

    generated_at = datetime.now(tz=LOCAL_TZ)
    cutoff = generated_at - timedelta(days=args.days)
    cutoff_ts = int(cutoff.timestamp())
    suffix = f"{args.days}d"
    raw_dir = args.out_dir / "raw"
    processed_dir = args.out_dir / "processed"

    raw_trades_path = raw_dir / f"coldmath_weather_market_trades_{suffix}.json"
    raw_activity_path = raw_dir / f"coldmath_weather_market_activity_{suffix}.json"
    raw_positions_path = raw_dir / "coldmath_current_positions.json"
    raw_closed_positions_path = raw_dir / f"coldmath_closed_positions_{suffix}.json"
    condition_ids_path = processed_dir / f"coldmath_weather_condition_ids_{suffix}.json"
    trades_checkpoint_path = raw_dir / f"coldmath_weather_market_trades_{suffix}.partial.json"
    activity_checkpoint_path = raw_dir / f"coldmath_weather_market_activity_{suffix}.partial.json"
    classified_csv = processed_dir / f"coldmath_classified_trades_{suffix}.csv"
    weather_csv = processed_dir / f"coldmath_weather_trades_{suffix}.csv"
    report_path = args.reports_dir / f"coldmath_weather_analysis_{suffix}.md"

    if args.refresh or not raw_positions_path.exists():
        current_positions_raw = fetch_current_positions(args.wallet)
        write_json(raw_positions_path, current_positions_raw)
    else:
        current_positions_raw = json.loads(raw_positions_path.read_text(encoding="utf-8"))

    if args.refresh or not raw_closed_positions_path.exists():
        closed_positions_raw = fetch_closed_positions(args.wallet, cutoff_ts)
        write_json(raw_closed_positions_path, closed_positions_raw)
    else:
        closed_positions_raw = json.loads(raw_closed_positions_path.read_text(encoding="utf-8"))

    condition_ids = discover_condition_ids(current_positions_raw, closed_positions_raw, cutoff)
    write_json(condition_ids_path, condition_ids)

    if args.refresh or not raw_trades_path.exists():
        trades_raw = fetch_market_rows(
            "/trades",
            args.wallet,
            condition_ids,
            cutoff_ts,
            extra_params={"takerOnly": "false"},
            checkpoint_path=trades_checkpoint_path,
        )
        write_json(raw_trades_path, trades_raw)
    else:
        trades_raw = json.loads(raw_trades_path.read_text(encoding="utf-8"))

    if args.refresh or not raw_activity_path.exists():
        activity_raw = fetch_market_rows(
            "/activity",
            args.wallet,
            condition_ids,
            cutoff_ts,
            checkpoint_path=activity_checkpoint_path,
        )
        write_json(raw_activity_path, activity_raw)
    else:
        activity_raw = json.loads(raw_activity_path.read_text(encoding="utf-8"))

    trades = sorted((classify_trade(item) for item in trades_raw), key=lambda trade: trade.timestamp, reverse=True)
    weather_trades = [trade for trade in trades if trade.is_weather]
    write_trades_csv(classified_csv, trades)
    write_trades_csv(weather_csv, weather_trades)
    write_report(
        report_path,
        wallet=args.wallet,
        generated_at=generated_at,
        cutoff=cutoff,
        trades=trades,
        activity=activity_raw,
        current_positions=current_positions_raw,
        closed_positions=closed_positions_raw,
        condition_ids=condition_ids,
        output_files={
            "raw hydrated weather trades": raw_trades_path,
            "raw hydrated weather activity": raw_activity_path,
            "raw current positions": raw_positions_path,
            "raw closed positions": raw_closed_positions_path,
            "weather condition ids": condition_ids_path,
            "classified trades CSV": classified_csv,
            "weather trades CSV": weather_csv,
            "analysis report": report_path,
        },
    )
    print(f"Wrote {report_path}")
    print(f"Classified {len(trades):,} trades; weather={len(weather_trades):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
