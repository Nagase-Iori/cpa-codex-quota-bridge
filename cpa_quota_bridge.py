#!/usr/bin/env python3
"""Read-only CCS quota bridge for a local CLIProxyAPI Credit Manager.

The bridge prefers a live Codex quota request using the credential already
stored by CLIProxyAPI, then falls back to a sanitized Credit Manager snapshot.
It never returns or logs OAuth tokens or the CPA management key. The public
endpoint only returns normalized percentage windows.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from urllib.parse import urlparse


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_time(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
        # Credit Manager stores Unix milliseconds.
        if number > 100_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def countdown_seconds(resets_at: str | None) -> int | None:
    """Return seconds until an ISO-8601 reset time, using UTC internally."""
    if not resets_at:
        return None
    try:
        reset = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        return max(0, int((reset - utc_now()).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None


def countdown_label(seconds: int | None) -> str | None:
    """Format a compact, timezone-independent countdown for CCS."""
    if seconds is None:
        return None
    minutes = seconds // 60
    days, day_remainder = divmod(minutes, 24 * 60)
    hours, remaining_minutes = divmod(day_remainder, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{remaining_minutes}m"
    return f"{remaining_minutes}m"


def decorate_window(window: dict[str, Any]) -> dict[str, Any]:
    """Add a fresh countdown without changing the canonical UTC reset time."""
    decorated = dict(window)
    seconds = countdown_seconds(decorated.get("resets_at"))
    decorated["remaining_seconds"] = seconds
    decorated["reset_in"] = countdown_label(seconds)
    return decorated


def number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def percentage(value: Any) -> float | None:
    result = number(value)
    if result is None:
        return None
    # Ratios in the CPA snapshot are normally 0..1; tolerate 0..100 too.
    return result * 100 if 0 <= result <= 1 else result


def window_label(window: dict[str, Any]) -> str:
    identifier = str(window.get("id") or "").lower()
    original = str(window.get("label") or "").strip()
    duration = number(window.get("duration_seconds"))

    if duration is not None and 4 * 3600 <= duration <= 6 * 3600:
        return "5小时"
    if duration is not None and 6 * 86400 <= duration <= 8 * 86400:
        return "7天"
    if any(part in identifier for part in ("primary", "five_hour", "5h")):
        return "5小时"
    if any(part in identifier for part in ("secondary", "weekly", "seven_day", "7d")):
        return "7天"
    if original:
        return original
    return identifier or "额度窗口"


def normalize_window(window: dict[str, Any]) -> dict[str, Any] | None:
    used = percentage(window.get("used_ratio"))
    if used is None:
        used = percentage(window.get("used_percent"))
    if used is None and str(window.get("unit") or "").lower() == "percentage":
        used = number(window.get("used"))
    if used is None:
        return None

    remaining = percentage(window.get("remaining_ratio"))
    if remaining is None:
        remaining = max(0.0, 100.0 - used)

    used = max(0.0, min(100.0, used))
    remaining = max(0.0, min(100.0, remaining))
    return {
        "label": window_label(window),
        "used_percent": round(used, 4),
        "remaining_percent": round(remaining, 4),
        "resets_at": iso_time(window.get("resets_at")),
    }


class QuotaStore:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._live_cache: dict[str, tuple[float, list[dict[str, Any]], str | None]] = {}
        self._live_errors: dict[str, str] = {}

    def _live_quota(self, auth_id: str) -> tuple[list[dict[str, Any]] | None, str]:
        """Fetch the current Codex quota without exposing the OAuth credential."""
        now = time.monotonic()
        cached = self._live_cache.get(auth_id)
        cache_seconds = max(0, int(self.config.get("live_cache_seconds", 20)))
        if cached and now - cached[0] <= cache_seconds:
            return cached[1], ""

        auth_file = Path(str(self.config.get("auth_file") or ""))
        if not auth_file.is_file():
            return None, "live quota credential unavailable"

        try:
            with auth_file.open("r", encoding="utf-8") as handle:
                credential = json.load(handle)
        except (OSError, ValueError):
            return None, "live quota credential unavailable"

        access_token = str(credential.get("access_token") or "").strip()
        account_id = str(credential.get("account_id") or "").strip()
        if not access_token or not account_id:
            return None, "live quota credential incomplete"

        url = str(
            self.config.get(
                "quota_url", "https://chatgpt.com/backend-api/wham/usage"
            )
        )
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "ChatGPT-Account-Id": account_id,
                "User-Agent": str(self.config.get("user_agent", "Codex/1.7.4")),
                "Accept": "application/json",
            },
            method="GET",
        )
        proxy_url = str(self.config.get("proxy_url") or "").strip()
        if proxy_url and proxy_url.lower() not in {"none", "direct"}:
            opener = build_opener(
                ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
        else:
            opener = build_opener(ProxyHandler({}))

        try:
            with opener.open(
                request, timeout=max(3, int(self.config.get("live_timeout_seconds", 12)))
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return None, f"live quota HTTP {error.code}"
        except (OSError, URLError, TimeoutError, ValueError):
            return None, "live quota request failed"

        rate_limit = payload.get("rate_limit") if isinstance(payload, dict) else None
        if not isinstance(rate_limit, dict):
            return None, "live quota response missing rate limit"

        windows: list[dict[str, Any]] = []
        for key, label in (("primary_window", "5小时"), ("secondary_window", "7天")):
            raw_window = rate_limit.get(key)
            if not isinstance(raw_window, dict):
                continue
            used = percentage(raw_window.get("used_percent"))
            if used is None:
                continue
            used = max(0.0, min(100.0, used))
            windows.append(
                {
                    "label": label,
                    "used_percent": round(used, 4),
                    "remaining_percent": round(100.0 - used, 4),
                    "resets_at": iso_time(
                        raw_window.get("reset_at") or raw_window.get("resets_at")
                    ),
                }
            )

        if not windows:
            return None, "live quota response has no windows"

        queried_at = utc_now().isoformat().replace("+00:00", "Z")
        self._live_cache[auth_id] = (now, windows, queried_at)
        self._live_errors.pop(auth_id, None)
        return windows, ""

    def _authorized_auth_id(self, bearer: str) -> str | None:
        digest = hashlib.sha256(bearer.encode("utf-8")).hexdigest()
        mappings = self.config.get("key_mappings") or {}
        for expected_hash, auth_id in mappings.items():
            if hmac.compare_digest(digest, str(expected_hash).lower()):
                return str(auth_id)
        allowed = self.config.get("allowed_key_hashes") or []
        if any(hmac.compare_digest(digest, str(item).lower()) for item in allowed):
            configured = str(self.config.get("default_auth_id") or "").strip()
            return configured or None
        return "__unauthorized__"

    def read(self, bearer: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            selected_auth = self._authorized_auth_id(bearer)
            if selected_auth == "__unauthorized__":
                return 401, {"success": False, "message": "unauthorized"}

            database = Path(str(self.config["database"]))
            if not database.exists():
                return 503, {"success": False, "message": "quota source unavailable"}

            query = (
                "SELECT auth_id, snapshot_json, last_success_at_unix_ms, last_error "
                "FROM auth_quota_snapshots WHERE provider = ?"
            )
            try:
                connection = sqlite3.connect(
                    f"file:{database}?mode=ro", uri=True, timeout=3
                )
                rows = connection.execute(query, ("codex",)).fetchall()
                connection.close()
            except sqlite3.Error:
                return 503, {"success": False, "message": "quota source unavailable"}

            if selected_auth:
                rows = [row for row in rows if str(row[0]) == selected_auth]
            elif len(rows) != 1:
                return 503, {
                    "success": False,
                    "message": "account mapping required",
                }

            if len(rows) != 1:
                return 503, {"success": False, "message": "quota snapshot unavailable"}

            auth_id, raw_snapshot, last_success_ms, last_error = rows[0]
            live_windows, live_error = self._live_quota(str(auth_id))
            if live_windows:
                return 200, {
                    "success": True,
                    "source": "CPA live Codex quota",
                    "plan": "ChatGPT/Codex",
                    "queried_at": self._live_cache[str(auth_id)][2],
                    "stale": False,
                    "last_error": "",
                    "windows": [decorate_window(window) for window in live_windows],
                }

            try:
                snapshot = json.loads(raw_snapshot or "{}")
            except (TypeError, ValueError):
                return 503, {"success": False, "message": "invalid quota snapshot"}

            windows: list[dict[str, Any]] = []
            seen: set[str] = set()
            for raw_window in snapshot.get("windows") or []:
                if not isinstance(raw_window, dict):
                    continue
                normalized = normalize_window(raw_window)
                if normalized is None or normalized["label"] in seen:
                    continue
                seen.add(normalized["label"])
                windows.append(normalized)

            if not windows:
                return 503, {"success": False, "message": "quota windows unavailable"}

            queried_at = iso_time(last_success_ms)
            stale = False
            if queried_at:
                try:
                    age = (utc_now() - datetime.fromisoformat(queried_at.replace("Z", "+00:00"))).total_seconds()
                    stale = age > int(self.config.get("max_stale_seconds", 1800))
                except ValueError:
                    stale = True

            return 200, {
                "success": True,
                "source": "CPA Credit Manager",
                "plan": snapshot.get("plan") or "ChatGPT/Codex",
                "queried_at": queried_at,
                "stale": True,
                "last_error": live_error or str(last_error or "") or "live quota unavailable",
                "windows": [decorate_window(window) for window in windows],
            }


class Handler(BaseHTTPRequestHandler):
    store: QuotaStore
    endpoint: str

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, {"success": True, "service": "cpa-quota-bridge"})
            return
        if path != self.endpoint:
            self._send(404, {"success": False, "message": "not found"})
            return

        authorization = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            self._send(401, {"success": False, "message": "unauthorized"})
            return
        status, payload = self.store.read(authorization[len(prefix) :].strip())
        self._send(status, payload)

    def do_POST(self) -> None:  # noqa: N802
        self._send(405, {"success": False, "message": "method not allowed"})

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        # Do not log Authorization headers or request paths containing credentials.
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    with Path(args.config).open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    endpoint = str(config.get("endpoint", "/ccs/quota"))
    host = str(config.get("listen_host", "127.0.0.1"))
    port = int(config.get("listen_port", 8765))
    Handler.store = QuotaStore(config)
    Handler.endpoint = endpoint
    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
