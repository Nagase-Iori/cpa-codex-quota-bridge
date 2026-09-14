#!/usr/bin/env python3
"""Refresh the existing CLIProxyAPI Credit Manager Codex snapshot.

This helper is intentionally narrow: it reads the current CPA auth file,
fetches the official Codex quota through the configured local proxy, and
updates only the matching sanitized snapshot row. It never prints or stores
the OAuth token in the snapshot.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


def now_ms() -> int:
    return int(time.time() * 1000)


def finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or abs(result) == float("inf"):
        return None
    return result


def iso_time(seconds: Any) -> str | None:
    value = finite_number(seconds)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    except (OverflowError, OSError, ValueError):
        return None


def quota_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    auth_file = Path(str(config["auth_file"]))
    with auth_file.open("r", encoding="utf-8") as handle:
        credential = json.load(handle)

    access_token = str(credential.get("access_token") or "").strip()
    account_id = str(credential.get("account_id") or "").strip()
    if not access_token or not account_id:
        raise RuntimeError("OAuth credential is incomplete")

    url = str(config.get("quota_url") or "https://chatgpt.com/backend-api/wham/usage")
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "ChatGPT-Account-Id": account_id,
            "User-Agent": str(config.get("user_agent") or "Codex/1.7.4"),
            "Accept": "application/json",
        },
        method="GET",
    )
    proxy_url = str(config.get("proxy_url") or "").strip()
    if proxy_url and proxy_url.lower() not in {"none", "direct"}:
        opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
    else:
        opener = build_opener(ProxyHandler({}))

    try:
        with opener.open(
            request, timeout=max(3, int(config.get("timeout_seconds", 12)))
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(f"quota endpoint HTTP {error.code}") from error
    except (OSError, URLError, TimeoutError, ValueError) as error:
        raise RuntimeError("quota endpoint request failed") from error

    rate_limit = payload.get("rate_limit") if isinstance(payload, dict) else None
    if not isinstance(rate_limit, dict):
        raise RuntimeError("quota response has no rate_limit")

    windows: list[dict[str, Any]] = []
    for key, window_id, label in (
        ("primary_window", "rate-limit-primary", "Rate limit primary"),
        ("secondary_window", "rate-limit-secondary", "Rate limit secondary"),
    ):
        raw = rate_limit.get(key)
        if not isinstance(raw, dict):
            continue
        used = finite_number(raw.get("used_percent"))
        duration = finite_number(raw.get("limit_window_seconds"))
        reset_at = finite_number(raw.get("reset_at"))
        if used is None or duration is None or reset_at is None:
            continue
        used = max(0.0, min(100.0, used))
        remaining = 100.0 - used
        windows.append(
            {
                "cycle_start_at": iso_time(reset_at - duration),
                "cycle_start_source": "official_window",
                "duration_seconds": int(duration),
                "id": window_id,
                "label": label,
                "limit": 100,
                "local_attribution_status": "",
                "mode": "rolling",
                "prediction_available": False,
                "remaining": round(remaining, 4),
                "remaining_ratio": round(remaining / 100.0, 6),
                "resets_at": iso_time(reset_at),
                "scope": "account",
                "unit": "percentage",
                "used": round(used, 4),
                "used_ratio": round(used / 100.0, 6),
            }
        )

    if not windows:
        raise RuntimeError("quota response has no usable windows")

    return {
        "plan": str(payload.get("plan_type") or "plus"),
        "reset_credits": int(
            finite_number(
                (payload.get("rate_limit_reset_credits") or {}).get("available_count")
                if isinstance(payload.get("rate_limit_reset_credits"), dict)
                else 0
            )
            or 0
        ),
        "windows": windows,
    }


def update_database(config: dict[str, Any], snapshot: dict[str, Any]) -> None:
    database = Path(str(config["database"]))
    provider = str(config.get("provider") or "codex")
    auth_id = str(config["auth_id"])
    timestamp = now_ms()
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    connection = sqlite3.connect(str(database), timeout=5)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        with connection:
            cursor = connection.execute(
                """
                UPDATE auth_quota_snapshots
                   SET snapshot_json = ?,
                       last_attempt_at_unix_ms = ?,
                       last_success_at_unix_ms = ?,
                       last_error_at_unix_ms = NULL,
                       last_error = ''
                 WHERE provider = ? AND auth_id = ?
                """,
                (encoded, timestamp, timestamp, provider, auth_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("matching Credit Manager snapshot row not found")
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    with Path(args.config).open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    lock_path = Path(
        str(config.get("lock_file") or "/opt/cpa-quota-bridge/credit-manager-quota-sync.lock")
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        snapshot = quota_snapshot(config)
        update_database(config, snapshot)
    print(
        "updated Credit Manager Codex snapshot: "
        + ", ".join(
            f"{window['label']}={window['used']}%"
            for window in snapshot["windows"]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
