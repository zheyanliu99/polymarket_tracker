#!/usr/bin/env python3
"""Low-latency ColdMath Polymarket buy alerts with audit logs.

This script is intentionally alert-only. It never signs or places orders.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import random
import smtplib
import socket
import ssl
import struct
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


COLDMATH_WALLET = "0x594edb9112f526fa6a80b8f858a6379c8a2c1c11"
COLDMATH_TOPIC = "0x" + "0" * 24 + COLDMATH_WALLET[2:].lower()
ORDER_FILLED_TOPIC = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"
PUSD_SCALE = 1_000_000
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_MARKET_BASE = "https://polymarket.com/event"

EXCHANGES = {
    "ctf_exchange_v2": "0xE111180000d2663C0091e4f400237545B87B996B",
    "neg_risk_ctf_exchange_v2": "0xe2222d279d744050d28e00520010520000310F59",
}

LIVE_DIR = Path("data/live")
BUY_EVENTS_JSONL = LIVE_DIR / "coldmath_buy_events.jsonl"
LATENCY_CSV = LIVE_DIR / "coldmath_latency.csv"
DAEMON_LOG = LIVE_DIR / "coldmath_daemon.log"
STATE_FILE = LIVE_DIR / "coldmath_event_state.json"
DEFAULT_LOCAL_CACHE = Path("data/raw/coldmath_weather_market_trades_365d.json")

CSV_FIELDS = [
    "event_id",
    "tx_hash",
    "log_index",
    "exchange_contract",
    "coldmath_role",
    "inferred_action",
    "asset_id",
    "condition_id",
    "outcome",
    "title",
    "market_url",
    "price",
    "coldmath_paid_price",
    "size",
    "coldmath_paid_usdc",
    "estimated_usdc_notional",
    "block_number",
    "block_timestamp_utc",
    "ws_received_at_utc",
    "decoded_at_utc",
    "metadata_resolved_at_utc",
    "mac_notification_at_utc",
    "email_sent_at_utc",
    "data_api_seen_at_utc",
    "detect_lag_ms",
    "decode_lag_ms",
    "metadata_lag_ms",
    "mac_alert_lag_ms",
    "email_lag_ms",
    "data_api_lag_ms",
    "total_alert_lag_ms",
    "source",
    "metadata_status",
    "notification_status",
]


class WebSocketError(RuntimeError):
    pass


class WebSocketClosed(WebSocketError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_from_unix(seconds: int | float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def ms_between(later_iso: str, earlier_iso: str) -> int | None:
    later = parse_iso(later_iso)
    earlier = parse_iso(earlier_iso)
    if not later or not earlier:
        return None
    return int((later - earlier).total_seconds() * 1000)


def hex_to_int(value: str | None) -> int:
    if not value:
        return 0
    return int(value, 16)


def int_to_float_amount(value: int) -> float:
    return value / PUSD_SCALE


def topic_to_address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def normalize_hex(value: str) -> str:
    if not value:
        return ""
    return value.lower() if value.startswith("0x") else f"0x{value}".lower()


def is_weather_text(*parts: str) -> bool:
    text = " ".join(part or "" for part in parts).lower()
    markers = (
        "temperature",
        "weather",
        "rain",
        "rainfall",
        "snow",
        "hurricane",
        "tropical storm",
        "wind speed",
        "wind",
        "nyc",
        "cape-town",
    )
    return any(marker in text for marker in markers)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


class DaemonLogger:
    def __init__(self, path: Path = DAEMON_LOG) -> None:
        self.path = path

    def log(self, event: str, **fields: Any) -> None:
        record = {"ts_utc": utc_now_iso(), "event": event, **fields}
        append_jsonl(self.path, record)


class MinimalWebSocket:
    """Tiny RFC 6455 client for JSON-RPC WSS, implemented with stdlib only."""

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self.url = url
        self.timeout = timeout
        self.sock: socket.socket | ssl.SSLSocket | None = None

    def connect(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"ws", "wss"}:
            raise WebSocketError("POLYGON_WSS_URL must start with ws:// or wss://")
        host = parsed.hostname
        if not host:
            raise WebSocketError("POLYGON_WSS_URL is missing a host")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        raw = socket.create_connection((host, port), timeout=self.timeout)
        if parsed.scheme == "wss":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        raw.settimeout(self.timeout)
        self.sock = raw

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: coldmath-live-alert/1.0\r\n"
            "\r\n"
        )
        raw.sendall(request.encode("ascii"))
        response = self._recv_until(b"\r\n\r\n")
        header = response.decode("iso-8859-1", errors="replace")
        if " 101 " not in header.split("\r\n", 1)[0]:
            raise WebSocketError(f"WebSocket handshake failed: {header.splitlines()[0]}")

        expected = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        if expected.lower() not in header.lower():
            raise WebSocketError("WebSocket handshake accept key mismatch")

    def close(self) -> None:
        if self.sock:
            try:
                self._send_frame(b"", opcode=0x8)
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def send_json(self, payload: dict[str, Any]) -> None:
        self._send_frame(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def recv_json(self, timeout: float | None = None) -> dict[str, Any]:
        old_timeout = None
        if timeout is not None and self.sock:
            old_timeout = self.sock.gettimeout()
            self.sock.settimeout(timeout)
        try:
            while True:
                opcode, payload = self._recv_frame()
                if opcode == 0x1:
                    return json.loads(payload.decode("utf-8"))
                if opcode == 0x2:
                    return json.loads(payload.decode("utf-8"))
                if opcode == 0x8:
                    raise WebSocketClosed("server closed websocket")
                if opcode == 0x9:
                    self._send_frame(payload, opcode=0xA)
                if opcode == 0xA:
                    continue
        finally:
            if old_timeout is not None and self.sock:
                self.sock.settimeout(old_timeout)

    def _recv_until(self, marker: bytes) -> bytes:
        data = b""
        while marker not in data:
            chunk = self._recv_exact(1)
            data += chunk
            if len(data) > 65536:
                raise WebSocketError("WebSocket handshake response too large")
        return data

    def _recv_exact(self, length: int) -> bytes:
        if not self.sock:
            raise WebSocketClosed("websocket is not connected")
        chunks = []
        remaining = length
        while remaining:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise WebSocketClosed("socket closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        if not self.sock:
            raise WebSocketClosed("websocket is not connected")
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", first, 0x80 | length)
        elif length < (1 << 16):
            header = struct.pack("!BBH", first, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", first, 0x80 | 127, length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _recv_frame(self) -> tuple[int, bytes]:
        header = self._recv_exact(2)
        first, second = header[0], header[1]
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return opcode, payload


class JsonRpcWs:
    def __init__(self, url: str, logger: DaemonLogger) -> None:
        self.ws = MinimalWebSocket(url)
        self.next_id = random.randint(1, 1_000_000)
        self.buffer: list[dict[str, Any]] = []
        self.logger = logger

    def connect(self) -> None:
        self.ws.connect()

    def close(self) -> None:
        self.ws.close()

    def request(self, method: str, params: list[Any] | None = None) -> Any:
        request_id = self.next_id
        self.next_id += 1
        self.ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or [],
            }
        )
        while True:
            message = self.ws.recv_json()
            if message.get("id") == request_id:
                if "error" in message:
                    raise WebSocketError(f"{method} failed: {message['error']}")
                return message.get("result")
            self.buffer.append(message)

    def recv(self, timeout: float = 1.0) -> dict[str, Any] | None:
        if self.buffer:
            return self.buffer.pop(0)
        try:
            return self.ws.recv_json(timeout=timeout)
        except socket.timeout:
            return None


def decode_order_filled_data(data: str) -> dict[str, int]:
    clean = data[2:] if data.startswith("0x") else data
    if len(clean) < 64 * 5:
        raise ValueError(f"OrderFilled data too short: {len(clean)}")
    values = [int(clean[i:i + 64], 16) for i in range(0, 64 * 5, 64)]
    return {
        "maker_asset_id": values[0],
        "taker_asset_id": values[1],
        "maker_amount_filled": values[2],
        "taker_amount_filled": values[3],
        "fee": values[4],
    }


def infer_coldmath_action(
    role: str,
    maker_asset_id: int,
    taker_asset_id: int,
    maker_amount_filled: int,
    taker_amount_filled: int,
) -> dict[str, Any]:
    if role == "maker":
        if maker_asset_id == 0:
            action = "BUY"
            asset_id = taker_asset_id
            size = int_to_float_amount(taker_amount_filled)
            notional = int_to_float_amount(maker_amount_filled)
        else:
            action = "SELL"
            asset_id = maker_asset_id
            size = int_to_float_amount(maker_amount_filled)
            notional = int_to_float_amount(taker_amount_filled)
    elif role == "taker":
        if maker_asset_id != 0:
            action = "BUY"
            asset_id = maker_asset_id
            size = int_to_float_amount(maker_amount_filled)
            notional = int_to_float_amount(taker_amount_filled)
        else:
            action = "SELL"
            asset_id = taker_asset_id
            size = int_to_float_amount(taker_amount_filled)
            notional = int_to_float_amount(maker_amount_filled)
    else:
        raise ValueError(f"Unknown ColdMath role: {role}")
    price = notional / size if size else 0.0
    return {
        "inferred_action": action,
        "asset_id": str(asset_id),
        "size": size,
        "estimated_usdc_notional": notional,
        "price": price,
    }


@dataclass
class DecodedFill:
    event_id: str
    tx_hash: str
    log_index: str
    exchange_contract: str
    coldmath_role: str
    inferred_action: str
    asset_id: str
    price: float
    size: float
    estimated_usdc_notional: float
    block_number: int


def decode_log(log: dict[str, Any], role_hint: str | None = None) -> DecodedFill:
    topics = [topic.lower() for topic in log.get("topics", [])]
    if len(topics) < 4 or topics[0] != ORDER_FILLED_TOPIC:
        raise ValueError("not an OrderFilled log")
    maker = topic_to_address(topics[2])
    taker = topic_to_address(topics[3])
    if role_hint in {"maker", "taker"}:
        role = role_hint
    elif maker == COLDMATH_WALLET:
        role = "maker"
    elif taker == COLDMATH_WALLET:
        role = "taker"
    else:
        raise ValueError("ColdMath is neither maker nor taker")

    decoded = decode_order_filled_data(log.get("data", ""))
    action = infer_coldmath_action(
        role,
        decoded["maker_asset_id"],
        decoded["taker_asset_id"],
        decoded["maker_amount_filled"],
        decoded["taker_amount_filled"],
    )
    tx_hash = normalize_hex(log.get("transactionHash", ""))
    log_index = str(hex_to_int(log.get("logIndex")))
    return DecodedFill(
        event_id=f"{tx_hash}:{log_index}",
        tx_hash=tx_hash,
        log_index=log_index,
        exchange_contract=normalize_hex(log.get("address", "")),
        coldmath_role=role,
        block_number=hex_to_int(log.get("blockNumber")),
        **action,
    )


class MetadataCache:
    def __init__(
        self,
        logger: DaemonLogger,
        local_cache: Path = DEFAULT_LOCAL_CACHE,
        gamma_limit: int = 500,
        gamma_max_pages: int = 20,
    ) -> None:
        self.logger = logger
        self.local_cache = local_cache
        self.gamma_limit = gamma_limit
        self.gamma_max_pages = gamma_max_pages
        self.by_asset_id: dict[str, dict[str, Any]] = {}
        self.last_gamma_refresh_monotonic = 0

    def load(self) -> None:
        self.load_local_cache()
        self.refresh_gamma()

    def load_local_cache(self) -> None:
        if not self.local_cache.exists():
            self.logger.log("metadata_local_cache_missing", path=str(self.local_cache))
            return
        try:
            rows = json.loads(self.local_cache.read_text(encoding="utf-8"))
        except Exception as exc:
            self.logger.log("metadata_local_cache_error", path=str(self.local_cache), error=str(exc))
            return
        count = 0
        for row in rows:
            asset_id = str(row.get("asset") or "")
            if not asset_id:
                continue
            if not is_weather_text(row.get("title", ""), row.get("slug", ""), row.get("eventSlug", "")):
                continue
            self.by_asset_id[asset_id] = {
                "asset_id": asset_id,
                "condition_id": row.get("conditionId") or "",
                "outcome": row.get("outcome") or "",
                "title": row.get("title") or "",
                "slug": row.get("slug") or "",
                "event_slug": row.get("eventSlug") or "",
                "market_url": f"{POLYMARKET_MARKET_BASE}/{row.get('eventSlug') or row.get('slug')}",
                "metadata_status": "resolved_weather",
                "metadata_source": "local_history",
            }
            count += 1
        self.logger.log(
            "metadata_local_cache_loaded",
            path=str(self.local_cache),
            rows=len(rows),
            assets=len(self.by_asset_id),
            weather_rows=count,
        )

    def refresh_gamma(self) -> None:
        before = len(self.by_asset_id)
        added = 0
        try:
            for offset in range(0, self.gamma_limit * self.gamma_max_pages, self.gamma_limit):
                params = urlencode(
                    {
                        "active": "true",
                        "closed": "false",
                        "limit": self.gamma_limit,
                        "offset": offset,
                    }
                )
                data = request_json_url(f"{GAMMA_API}/markets?{params}")
                if not isinstance(data, list):
                    break
                for market in data:
                    if not is_weather_text(
                        market.get("question", ""),
                        market.get("slug", ""),
                        event_slug_from_market(market),
                    ):
                        continue
                    added += self._add_gamma_market(market)
                if len(data) < self.gamma_limit:
                    break
        except Exception as exc:
            self.logger.log("metadata_gamma_refresh_error", error=str(exc))
            return
        self.last_gamma_refresh_monotonic = time.monotonic()
        self.logger.log(
            "metadata_gamma_refreshed",
            assets_before=before,
            assets_after=len(self.by_asset_id),
            added_or_updated=added,
        )

    def _add_gamma_market(self, market: dict[str, Any]) -> int:
        token_ids = parse_json_list(market.get("clobTokenIds"))
        outcomes = parse_json_list(market.get("outcomes"))
        if not token_ids:
            return 0
        event_slug = event_slug_from_market(market)
        slug = market.get("slug") or ""
        title = market.get("question") or ""
        added = 0
        for index, token_id in enumerate(token_ids):
            asset_id = str(token_id)
            outcome = str(outcomes[index]) if index < len(outcomes) else ""
            self.by_asset_id[asset_id] = {
                "asset_id": asset_id,
                "condition_id": market.get("conditionId") or "",
                "outcome": outcome,
                "title": title,
                "slug": slug,
                "event_slug": event_slug,
                "market_url": f"{POLYMARKET_MARKET_BASE}/{event_slug or slug}",
                "metadata_status": "resolved_weather",
                "metadata_source": "gamma_active",
            }
            added += 1
        return added

    def resolve(self, asset_id: str) -> dict[str, Any] | None:
        return self.by_asset_id.get(str(asset_id))


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def event_slug_from_market(market: dict[str, Any]) -> str:
    events = market.get("events")
    if isinstance(events, list) and events:
        return str(events[0].get("slug") or "")
    return str(market.get("eventSlug") or "")


def request_json_url(url: str, timeout: float = 12.0) -> Any:
    request = Request(url, headers={"User-Agent": "coldmath-live-alert/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class EventStore:
    def __init__(self, logger: DaemonLogger) -> None:
        self.logger = logger
        self.events: dict[str, dict[str, Any]] = {}
        LIVE_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.events = {
                        event_id: event
                        for event_id, event in data.get("events", {}).items()
                        if isinstance(event, dict)
                    }
            except Exception as exc:
                self.logger.log("state_load_error", error=str(exc))
        elif BUY_EVENTS_JSONL.exists():
            with BUY_EVENTS_JSONL.open(encoding="utf-8") as file:
                for line in file:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    event_id = event.get("event_id")
                    if event_id:
                        self.events[event_id] = event
        self.logger.log("state_loaded", event_count=len(self.events))

    def has_seen(self, event_id: str) -> bool:
        return event_id in self.events

    def record_event(self, event: dict[str, Any]) -> bool:
        event_id = event["event_id"]
        if event_id in self.events:
            self.logger.log("duplicate_seen", event_id=event_id, tx_hash=event.get("tx_hash"))
            return False
        self.events[event_id] = event
        append_jsonl(BUY_EVENTS_JSONL, event)
        self.save_state()
        self.write_csv()
        return True

    def update_event(self, event_id: str, updates: dict[str, Any]) -> None:
        if event_id not in self.events:
            return
        self.events[event_id].update(updates)
        self.save_state()
        self.write_csv()

    def unknown_events(self) -> list[dict[str, Any]]:
        return [
            event
            for event in self.events.values()
            if event.get("metadata_status") == "unknown"
        ]

    def save_state(self) -> None:
        STATE_FILE.write_text(
            json.dumps({"events": self.events}, sort_keys=True, indent=2),
            encoding="utf-8",
        )

    def write_csv(self) -> None:
        rows = sorted(self.events.values(), key=lambda row: row.get("ws_received_at_utc") or "")
        with LATENCY_CSV.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})


class Notifier:
    def __init__(
        self,
        logger: DaemonLogger,
        *,
        enable_mac: bool = True,
        enable_email: bool = True,
        email_to: str = "",
    ) -> None:
        self.logger = logger
        self.enable_mac = enable_mac
        self.enable_email = enable_email
        self.email_to = email_to
        self.email_failures = 0

    def notify(self, event: dict[str, Any]) -> dict[str, Any]:
        statuses: list[str] = []
        updates: dict[str, Any] = {}
        title = event.get("title") or "Unknown Polymarket market"
        body = (
            f"{event.get('outcome') or 'Unknown outcome'} at "
            f"{format_float(event.get('coldmath_paid_price') or event.get('price'))} "
            f"for ${format_float(event.get('coldmath_paid_usdc') or event.get('estimated_usdc_notional'))}"
        )
        if self.enable_mac:
            try:
                self.send_mac(title, body, event.get("market_url") or event.get("tx_hash") or "")
                updates["mac_notification_at_utc"] = utc_now_iso()
                statuses.append("mac_sent")
            except Exception as exc:
                statuses.append("mac_error")
                self.logger.log("mac_notification_error", event_id=event.get("event_id"), error=str(exc))
        else:
            statuses.append("mac_disabled")

        if self.enable_email:
            try:
                email_status = self.send_email(event)
                if email_status == "email_sent":
                    updates["email_sent_at_utc"] = utc_now_iso()
                statuses.append(email_status)
            except Exception as exc:
                self.email_failures += 1
                statuses.append("email_error")
                self.logger.log("email_notification_error", event_id=event.get("event_id"), error=str(exc))
        else:
            statuses.append("email_disabled")

        updates["notification_status"] = ",".join(statuses)
        first_alert = min(
            [
                ts
                for ts in (
                    updates.get("mac_notification_at_utc"),
                    updates.get("email_sent_at_utc"),
                )
                if ts
            ],
            default="",
        )
        if first_alert:
            updates["total_alert_lag_ms"] = ms_between(first_alert, event.get("block_timestamp_utc", ""))
        updates["mac_alert_lag_ms"] = blank_if_none(ms_between(
            updates.get("mac_notification_at_utc", ""),
            event.get("ws_received_at_utc", ""),
        ))
        updates["email_lag_ms"] = blank_if_none(ms_between(
            updates.get("email_sent_at_utc", ""),
            event.get("ws_received_at_utc", ""),
        ))
        return updates

    def send_mac(self, title: str, body: str, subtitle: str) -> None:
        script = (
            'display notification '
            f'{apple_string(body)} '
            f'with title {apple_string("ColdMath BUY")} '
            f'subtitle {apple_string(title[:80])}'
        )
        subprocess.run(["osascript", "-e", script], check=True, timeout=5)

    def send_email(self, event: dict[str, Any]) -> str:
        required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM"]
        missing = [name for name in required if not os.getenv(name)]
        recipient = os.getenv("ALERT_EMAIL_TO") or self.email_to
        if not recipient:
            missing.append("ALERT_EMAIL_TO")
        if missing:
            self.logger.log("email_skipped_missing_env", missing=missing)
            return "email_skipped_missing_env"

        message = EmailMessage()
        message["Subject"] = (
            f"ColdMath BUY: {event.get('outcome') or ''} "
            f"{event.get('coldmath_paid_price') or event.get('price')}"
        )
        message["From"] = os.environ["SMTP_FROM"]
        message["To"] = recipient
        message.set_content(render_email_body(event))

        port = int(os.environ["SMTP_PORT"])
        host = os.environ["SMTP_HOST"]
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
            smtp.send_message(message)
        return "email_sent"


def apple_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def format_float(value: Any) -> str:
    try:
        return f"{float(value):.4g}"
    except (TypeError, ValueError):
        return ""


def render_email_body(event: dict[str, Any]) -> str:
    lines = [
        "ColdMath BUY detected",
        "",
        f"Market: {event.get('title')}",
        f"Outcome: {event.get('outcome')}",
        f"ColdMath paid price: {event.get('coldmath_paid_price') or event.get('price')}",
        f"Size: {event.get('size')}",
        f"ColdMath paid USDC: {event.get('coldmath_paid_usdc') or event.get('estimated_usdc_notional')}",
        f"Role: {event.get('coldmath_role')}",
        f"Market URL: {event.get('market_url')}",
        f"Tx: https://polygonscan.com/tx/{event.get('tx_hash')}",
        "",
        f"Block time UTC: {event.get('block_timestamp_utc')}",
        f"Detected UTC: {event.get('ws_received_at_utc')}",
        f"Detect lag ms: {event.get('detect_lag_ms')}",
        f"Source: {event.get('source')}",
    ]
    return "\n".join(lines)


class LiveMonitor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.logger = DaemonLogger()
        self.store = EventStore(self.logger)
        self.metadata = MetadataCache(self.logger, local_cache=args.local_cache)
        self.notifier = Notifier(
            self.logger,
            enable_mac=not args.no_mac,
            enable_email=not args.no_email,
            email_to=args.alert_email_to,
        )
        self.block_timestamps: dict[int, str] = {}
        self.subscription_roles: dict[str, dict[str, str]] = {}
        self.latest_block_number = 0
        self.last_block_seen_monotonic = 0.0
        self.reconnect_count = 0
        self.detect_lags: list[int] = []
        self.last_heartbeat_monotonic = 0.0
        self.last_data_api_poll_monotonic = 0.0

    def run(self) -> None:
        self.metadata.load()
        if self.args.test_notification:
            self.send_test_notification()
            return
        if not self.args.polygon_wss_url:
            raise SystemExit("Set POLYGON_WSS_URL or pass --polygon-wss-url")

        backoff = 1.0
        while True:
            rpc = JsonRpcWs(self.args.polygon_wss_url, self.logger)
            try:
                rpc.connect()
                self.logger.log("wss_connected")
                self.subscribe(rpc)
                backoff = 1.0
                self.loop_connected(rpc)
            except KeyboardInterrupt:
                self.logger.log("shutdown_keyboard_interrupt")
                rpc.close()
                raise
            except Exception as exc:
                self.reconnect_count += 1
                self.logger.log("wss_error", error=str(exc), reconnect_count=self.reconnect_count)
                rpc.close()
                self.poll_fallback_if_due(force=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def subscribe(self, rpc: JsonRpcWs) -> None:
        head_id = rpc.request("eth_subscribe", ["newHeads"])
        self.subscription_roles[head_id] = {"type": "newHeads"}
        for exchange_name, address in EXCHANGES.items():
            for role, topics in {
                "maker": [ORDER_FILLED_TOPIC, None, COLDMATH_TOPIC, None],
                "taker": [ORDER_FILLED_TOPIC, None, None, COLDMATH_TOPIC],
            }.items():
                sub_id = rpc.request(
                    "eth_subscribe",
                    [
                        "logs",
                        {
                            "address": address,
                            "topics": topics,
                        },
                    ],
                )
                self.subscription_roles[sub_id] = {
                    "type": "logs",
                    "exchange_name": exchange_name,
                    "exchange_contract": address,
                    "role": role,
                }
        self.logger.log("wss_subscribed", subscriptions=len(self.subscription_roles))
        self.last_block_seen_monotonic = time.monotonic()

    def loop_connected(self, rpc: JsonRpcWs) -> None:
        while True:
            now_mono = time.monotonic()
            if now_mono - self.metadata.last_gamma_refresh_monotonic >= self.args.gamma_refresh_seconds:
                self.metadata.refresh_gamma()
                self.resolve_pending_unknowns()
            self.emit_heartbeat_if_due(connected=True)
            stale = (
                self.last_block_seen_monotonic
                and now_mono - self.last_block_seen_monotonic > self.args.stale_seconds
            )
            if stale:
                seconds_since_last_block = now_mono - self.last_block_seen_monotonic
                self.logger.log(
                    "wss_stale",
                    seconds_since_last_block=round(seconds_since_last_block, 3),
                    latest_block_number=self.latest_block_number,
                )
                self.poll_fallback_if_due(force=True)
                raise WebSocketError(
                    f"WSS stale for {seconds_since_last_block:.3f}s after block "
                    f"{self.latest_block_number}; reconnecting"
                )
            message = rpc.recv(timeout=1.0)
            if message is None:
                continue
            self.handle_message(rpc, message)

    def handle_message(self, rpc: JsonRpcWs, message: dict[str, Any]) -> None:
        if message.get("method") != "eth_subscription":
            self.logger.log("wss_unexpected_message", message=message)
            return
        params = message.get("params") or {}
        sub_id = params.get("subscription")
        sub_meta = self.subscription_roles.get(sub_id, {})
        result = params.get("result") or {}
        if sub_meta.get("type") == "newHeads":
            self.latest_block_number = hex_to_int(result.get("number"))
            self.last_block_seen_monotonic = time.monotonic()
            return
        if sub_meta.get("type") != "logs":
            return
        self.process_wss_log(rpc, result, sub_meta.get("role"))

    def process_wss_log(self, rpc: JsonRpcWs, log: dict[str, Any], role_hint: str | None) -> None:
        received_mono = time.monotonic_ns()
        ws_received_at = utc_now_iso()
        try:
            decoded = decode_log(log, role_hint=role_hint)
        except Exception as exc:
            self.logger.log("decode_error", error=str(exc), log=log)
            return
        if decoded.inferred_action != "BUY":
            self.logger.log(
                "ignored_non_buy",
                event_id=decoded.event_id,
                tx_hash=decoded.tx_hash,
                role=decoded.coldmath_role,
                action=decoded.inferred_action,
            )
            return
        if self.store.has_seen(decoded.event_id):
            self.logger.log("duplicate_seen", event_id=decoded.event_id, tx_hash=decoded.tx_hash)
            return

        decoded_mono = time.monotonic_ns()
        decoded_at = utc_now_iso()
        block_timestamp_utc = self.block_timestamp_for(rpc, decoded.block_number) or ws_received_at
        metadata = self.metadata.resolve(decoded.asset_id)
        metadata_resolved_at = utc_now_iso() if metadata else ""
        metadata_status = metadata.get("metadata_status", "resolved_weather") if metadata else "unknown"
        event = self.build_event(
            decoded,
            block_timestamp_utc=block_timestamp_utc,
            ws_received_at=ws_received_at,
            decoded_at=decoded_at,
            metadata=metadata,
            metadata_resolved_at=metadata_resolved_at,
            source="polygon_wss",
        )
        event["decode_lag_ms"] = int((decoded_mono - received_mono) / 1_000_000)
        event["detect_lag_ms"] = ms_between(ws_received_at, block_timestamp_utc)
        event["metadata_lag_ms"] = ms_between(metadata_resolved_at, ws_received_at)
        event["metadata_status"] = metadata_status
        if event["detect_lag_ms"] is not None:
            self.detect_lags.append(event["detect_lag_ms"])

        if metadata_status == "resolved_weather":
            event.update(self.notifier.notify(event))
        else:
            event["notification_status"] = "metadata_unknown_no_alert"
        self.store.record_event(event)
        self.print_detection(event)

    def block_timestamp_for(self, rpc: JsonRpcWs, block_number: int) -> str:
        if block_number in self.block_timestamps:
            return self.block_timestamps[block_number]
        block = rpc.request("eth_getBlockByNumber", [hex(block_number), False])
        timestamp = hex_to_int(block.get("timestamp")) if isinstance(block, dict) else 0
        value = iso_from_unix(timestamp) if timestamp else ""
        if value:
            self.block_timestamps[block_number] = value
        return value

    def build_event(
        self,
        decoded: DecodedFill,
        *,
        block_timestamp_utc: str,
        ws_received_at: str,
        decoded_at: str,
        metadata: dict[str, Any] | None,
        metadata_resolved_at: str,
        source: str,
    ) -> dict[str, Any]:
        metadata = metadata or {}
        return {
            "event_id": decoded.event_id,
            "tx_hash": decoded.tx_hash,
            "log_index": decoded.log_index,
            "exchange_contract": decoded.exchange_contract,
            "coldmath_role": decoded.coldmath_role,
            "inferred_action": decoded.inferred_action,
            "asset_id": decoded.asset_id,
            "condition_id": metadata.get("condition_id", ""),
            "outcome": metadata.get("outcome", ""),
            "title": metadata.get("title", ""),
            "market_url": metadata.get("market_url", ""),
            "price": f"{decoded.price:.8g}",
            "coldmath_paid_price": f"{decoded.price:.8g}",
            "size": f"{decoded.size:.8g}",
            "coldmath_paid_usdc": f"{decoded.estimated_usdc_notional:.8g}",
            "estimated_usdc_notional": f"{decoded.estimated_usdc_notional:.8g}",
            "block_number": decoded.block_number,
            "block_timestamp_utc": block_timestamp_utc,
            "ws_received_at_utc": ws_received_at,
            "decoded_at_utc": decoded_at,
            "metadata_resolved_at_utc": metadata_resolved_at,
            "mac_notification_at_utc": "",
            "email_sent_at_utc": "",
            "data_api_seen_at_utc": "",
            "detect_lag_ms": "",
            "decode_lag_ms": "",
            "metadata_lag_ms": "",
            "mac_alert_lag_ms": "",
            "email_lag_ms": "",
            "data_api_lag_ms": "",
            "total_alert_lag_ms": "",
            "source": source,
            "metadata_status": metadata.get("metadata_status", "unknown"),
            "notification_status": "",
        }

    def resolve_pending_unknowns(self) -> None:
        for event in self.store.unknown_events():
            metadata = self.metadata.resolve(event.get("asset_id", ""))
            if not metadata:
                continue
            metadata_resolved_at = utc_now_iso()
            updates = {
                "condition_id": metadata.get("condition_id", ""),
                "outcome": metadata.get("outcome", ""),
                "title": metadata.get("title", ""),
                "market_url": metadata.get("market_url", ""),
                "metadata_status": "resolved_weather",
                "metadata_resolved_at_utc": metadata_resolved_at,
                "metadata_lag_ms": ms_between(metadata_resolved_at, event.get("ws_received_at_utc", "")),
            }
            event.update(updates)
            updates.update(self.notifier.notify(event))
            self.store.update_event(event["event_id"], updates)
            self.logger.log("metadata_resolved_late", event_id=event["event_id"], asset_id=event.get("asset_id"))

    def poll_fallback_if_due(self, force: bool = False) -> None:
        now_mono = time.monotonic()
        if not force and now_mono - self.last_data_api_poll_monotonic < self.args.fallback_poll_seconds:
            return
        self.last_data_api_poll_monotonic = now_mono
        try:
            params = urlencode({"user": COLDMATH_WALLET, "limit": 10})
            rows = request_json_url(f"{DATA_API}/activity?{params}", timeout=8)
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            self.logger.log("data_api_fallback_error", error=str(exc))
            return
        if not isinstance(rows, list):
            return
        seen_at = utc_now_iso()
        for row in rows:
            if row.get("type") != "TRADE" or row.get("side") != "BUY":
                continue
            if not is_weather_text(row.get("title", ""), row.get("slug", ""), row.get("eventSlug", "")):
                continue
            event = self.event_from_data_api(row, seen_at)
            if self.store.has_seen(event["event_id"]):
                continue
            event.update(self.notifier.notify(event))
            self.store.record_event(event)
            self.print_detection(event)

    def event_from_data_api(self, row: dict[str, Any], seen_at: str) -> dict[str, Any]:
        tx_hash = normalize_hex(row.get("transactionHash", ""))
        asset_id = str(row.get("asset") or "")
        size = float(row.get("size") or 0)
        price = float(row.get("price") or 0)
        notional = float(row.get("usdcSize") or 0) or size * price
        block_timestamp = iso_from_unix(int(row.get("timestamp") or 0))
        event_id = f"{tx_hash}:data_api:{asset_id}:{row.get('outcome')}:{size}"
        market_url = f"{POLYMARKET_MARKET_BASE}/{row.get('eventSlug') or row.get('slug')}"
        event = {
            "event_id": event_id,
            "tx_hash": tx_hash,
            "log_index": "",
            "exchange_contract": "",
            "coldmath_role": "",
            "inferred_action": "BUY",
            "asset_id": asset_id,
            "condition_id": row.get("conditionId") or "",
            "outcome": row.get("outcome") or "",
            "title": row.get("title") or "",
            "market_url": market_url,
            "price": f"{price:.8g}",
            "coldmath_paid_price": f"{price:.8g}",
            "size": f"{size:.8g}",
            "coldmath_paid_usdc": f"{notional:.8g}",
            "estimated_usdc_notional": f"{notional:.8g}",
            "block_number": "",
            "block_timestamp_utc": block_timestamp,
            "ws_received_at_utc": seen_at,
            "decoded_at_utc": seen_at,
            "metadata_resolved_at_utc": seen_at,
            "mac_notification_at_utc": "",
            "email_sent_at_utc": "",
            "data_api_seen_at_utc": seen_at,
            "detect_lag_ms": ms_between(seen_at, block_timestamp),
            "decode_lag_ms": 0,
            "metadata_lag_ms": 0,
            "mac_alert_lag_ms": "",
            "email_lag_ms": "",
            "data_api_lag_ms": ms_between(seen_at, block_timestamp),
            "total_alert_lag_ms": "",
            "source": "data_api_fallback",
            "metadata_status": "resolved_weather",
            "notification_status": "",
        }
        if event["detect_lag_ms"] is not None:
            self.detect_lags.append(event["detect_lag_ms"])
        return event

    def emit_heartbeat_if_due(self, connected: bool) -> None:
        now_mono = time.monotonic()
        if now_mono - self.last_heartbeat_monotonic < self.args.heartbeat_seconds:
            return
        self.last_heartbeat_monotonic = now_mono
        lags = self.detect_lags[-1000:]
        seconds_since_last_block = (
            round(now_mono - self.last_block_seen_monotonic, 3)
            if self.last_block_seen_monotonic
            else ""
        )
        self.logger.log(
            "heartbeat",
            latest_block_number=self.latest_block_number,
            seconds_since_last_block=seconds_since_last_block,
            wss_connected=connected,
            total_detections=len(self.store.events),
            detect_lag_avg_ms=round(sum(lags) / len(lags), 2) if lags else "",
            detect_lag_p50_ms=percentile(lags, 50),
            detect_lag_p95_ms=percentile(lags, 95),
            reconnect_count=self.reconnect_count,
            email_failures=self.notifier.email_failures,
        )

    def send_test_notification(self) -> None:
        now = utc_now_iso()
        event = {
            field: ""
            for field in CSV_FIELDS
        }
        event.update(
            {
                "event_id": f"test:{int(time.time())}",
                "inferred_action": "BUY",
                "outcome": "Yes",
                "title": "Test ColdMath alert",
                "market_url": "https://polymarket.com",
                "price": "0.01",
                "coldmath_paid_price": "0.01",
                "size": "1",
                "coldmath_paid_usdc": "0.01",
                "estimated_usdc_notional": "0.01",
                "block_timestamp_utc": now,
                "ws_received_at_utc": now,
                "metadata_status": "resolved_weather",
                "source": "test",
            }
        )
        event.update(self.notifier.notify(event))
        self.logger.log("test_notification_sent", status=event.get("notification_status"))
        print(json.dumps(event, indent=2))

    def print_detection(self, event: dict[str, Any]) -> None:
        print(
            f"[{utc_now_iso()}] ColdMath BUY {event.get('outcome') or ''} "
            f"paid_price={event.get('coldmath_paid_price') or event.get('price')} "
            f"paid_usdc=${event.get('coldmath_paid_usdc') or event.get('estimated_usdc_notional')} "
            f"lag_ms={event.get('detect_lag_ms')} {event.get('market_url') or event.get('tx_hash')}",
            flush=True,
        )


def percentile(values: list[int], pct: int) -> int | str:
    if not values:
        return ""
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((pct / 100) * (len(ordered) - 1)))
    return ordered[index]


def blank_if_none(value: Any) -> Any:
    return "" if value is None else value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Alert on ColdMath Polymarket buy fills.")
    parser.add_argument("--polygon-wss-url", default=os.getenv("POLYGON_WSS_URL", ""))
    parser.add_argument("--local-cache", type=Path, default=DEFAULT_LOCAL_CACHE)
    parser.add_argument("--alert-email-to", default=os.getenv("ALERT_EMAIL_TO", ""))
    parser.add_argument("--heartbeat-seconds", type=int, default=60)
    parser.add_argument("--stale-seconds", type=int, default=15)
    parser.add_argument("--fallback-poll-seconds", type=int, default=2)
    parser.add_argument("--gamma-refresh-seconds", type=int, default=60)
    parser.add_argument("--no-email", action="store_true")
    parser.add_argument("--no-mac", action="store_true")
    parser.add_argument("--test-notification", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    monitor = LiveMonitor(args)
    monitor.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
