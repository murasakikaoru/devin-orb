"""Devin account status: credential discovery + GetUserStatus RPC.

Data source: Connect RPC `GetUserStatus` on the Codeium API server — the same
read-only endpoint the Devin client's `/usage` view uses. Credentials are read
from files the Devin CLI / Devin Desktop already create on this machine; we
never modify them.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tomllib
import urllib.request
import urllib.error

CLOUD_SERVICE = "exa.seat_management_pb.SeatManagementService"
DEFAULT_API_SERVER = "https://server.codeium.com"
COMPAT_VERSION = "1.108.2"
HTTP_TIMEOUT = 15


class NoCredentials(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status: int | None, detail: str = ""):
        super().__init__(detail or f"HTTP {status}")
        self.status = status
        self.detail = detail


def _roaming() -> str:
    return os.environ.get("APPDATA") or os.path.expandvars(r"%APPDATA%")


def _toml_string(text: str, key: str) -> str | None:
    # Parse with tomllib if possible; the file is a flat [section-less] or
    # sectioned TOML. Walk tables until we find the key.
    try:
        data = tomllib.loads(text)
    except Exception:
        return None
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if key in node and isinstance(node[key], str):
                return node[key]
            stack.extend(node.values())
    return None


def _load_credentials_file() -> dict | None:
    """credentials.toml written by `devin auth login`."""
    candidates = [
        os.path.join(_roaming(), "devin", "credentials.toml"),                    # Windows
        os.path.expanduser("~/.local/share/devin/credentials.toml"),              # Linux/macOS
        os.path.join(os.environ.get("XDG_DATA_HOME", ""), "devin", "credentials.toml"),
    ]
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            text = open(path, "r", encoding="utf-8").read()
        except OSError:
            continue
        key = _toml_string(text, "windsurf_api_key")
        if not key:
            continue
        url = _toml_string(text, "api_server_url")
        if url and url.startswith("https://"):
            url = url.rstrip("/")
        else:
            url = None
        return {"api_key": key.strip(), "api_url": url, "source": "credentials.toml"}
    return None


def _load_app_auth() -> dict | None:
    """Devin Desktop's global storage: state.vscdb ItemTable['windsurfAuthStatus']."""
    if sys.platform == "win32":
        base = _roaming()
        names = ["devin", "Devin", "Devin - Next"]
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
        names = ["Devin", "devin", "Devin - Next"]
    else:
        base = os.path.expanduser("~/.config")
        names = ["Devin", "devin", "Devin - Next"]
    for name in names:
        db_path = os.path.join(base, name, "User", "globalStorage", "state.vscdb")
        if not os.path.isfile(db_path):
            continue
        try:
            # immutable=1 opens the SQLite file without taking a lock, so we
            # never block the app that owns it.
            db = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
            row = db.execute(
                "SELECT value FROM ItemTable WHERE key='windsurfAuthStatus' LIMIT 1"
            ).fetchone()
            db.close()
            if not row:
                continue
            auth = json.loads(row[0])
            key = (auth.get("apiKey") or "").strip()
            if key:
                return {"api_key": key, "api_url": None, "source": f"{name} app"}
        except Exception:
            continue
    return None


def find_credentials() -> dict:
    for loader in (_load_credentials_file, _load_app_auth):
        auth = loader()
        if auth:
            return auth
    raise NoCredentials()


def fetch_status(auth: dict | None = None) -> dict:
    """Fetch and normalize account status. Raises NoCredentials/ApiError."""
    if auth is None:
        auth = find_credentials()
    url = (auth.get("api_url") or DEFAULT_API_SERVER) + "/" + CLOUD_SERVICE + "/GetUserStatus"
    body = json.dumps({
        "metadata": {
            "apiKey": auth["api_key"],
            "ideName": "devin",
            "ideVersion": COMPAT_VERSION,
            "extensionName": "devin",
            "extensionVersion": COMPAT_VERSION,
            "locale": "en",
        }
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1"},
    )
    try:
        raw = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT).read()
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, str(e))
    except Exception as e:
        raise ApiError(None, str(e))
    data = json.loads(raw)
    return normalize(data)


def _num(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def normalize(data: dict) -> dict:
    """Map the proto3-JSON userStatus into a flat snapshot.

    proto3 omits zero-valued fields: a weekly reset timestamp without a
    weeklyQuotaRemainingPercent means the weekly quota is exhausted (0% left).
    """
    us = data.get("userStatus") or {}
    ps = us.get("planStatus") or {}
    pi = ps.get("planInfo") or {}

    hide_daily = pi.get("hideDailyQuota") is True
    daily_left = _num(ps.get("dailyQuotaRemainingPercent"))
    weekly_left = _num(ps.get("weeklyQuotaRemainingPercent"))
    daily_reset = _num(ps.get("dailyQuotaResetAtUnix"))
    weekly_reset = _num(ps.get("weeklyQuotaResetAtUnix"))

    if weekly_left is None and weekly_reset is not None:
        weekly_left = 0.0  # field omitted => zero remaining

    micros = _num(ps.get("overageBalanceMicros"))
    overage = max(0.0, micros / 1_000_000) if micros is not None else None

    return {
        "plan": (pi.get("planName") or "Unknown").strip(),
        "billing": pi.get("billingStrategy") or "",
        "daily_left": None if hide_daily else daily_left,
        "weekly_left": weekly_left,
        "daily_reset": daily_reset,
        "weekly_reset": weekly_reset,
        "overage_usd": overage,
        "period_start": ps.get("planStart"),
        "period_end": ps.get("planEnd"),
        "fetched_at": __import__("time").time(),
        "source": None,
    }
