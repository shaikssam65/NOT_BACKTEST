from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_bot.config import ROOT, Settings, load_settings

logger = logging.getLogger(__name__)

DEFAULT_REDIRECT_URL = "http://127.0.0.1:8501/"
SESSION_PATH = ROOT / "data" / "kite_session.json"
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE_PATH = ROOT / ".env.example"


def kite_redirect_url(settings: Settings | None = None) -> str:
    value = (settings.kite_redirect_url if settings else None) or DEFAULT_REDIRECT_URL
    return value.rstrip("/") + "/"


def ensure_env_file() -> Path:
    if not ENV_PATH.exists() and ENV_EXAMPLE_PATH.exists():
        ENV_PATH.write_text(ENV_EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    elif not ENV_PATH.exists():
        ENV_PATH.write_text(
            "PAPER_MODE=true\nKITE_REDIRECT_URL=http://127.0.0.1:8501/\n",
            encoding="utf-8",
        )
    return ENV_PATH


def upsert_env(updates: dict[str, str], path: Path | None = None) -> None:
    """Write key=value pairs into .env without dropping unrelated lines."""
    env_path = path or ensure_env_file()
    raw = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = raw.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


def load_session(path: Path | None = None) -> dict[str, Any] | None:
    session_path = path or SESSION_PATH
    if not session_path.exists():
        return None
    try:
        data = json.loads(session_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("access_token"):
        return None
    return data


def save_session(payload: dict[str, Any], path: Path | None = None) -> Path:
    session_path = path or SESSION_PATH
    session_path.parent.mkdir(parents=True, exist_ok=True)
    stored = {
        "access_token": payload.get("access_token"),
        "public_token": payload.get("public_token"),
        "user_id": payload.get("user_id"),
        "user_name": payload.get("user_name"),
        "email": payload.get("email"),
        "login_time": payload.get("login_time") or datetime.now(timezone.utc).isoformat(),
        "saved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    session_path.write_text(json.dumps(stored, indent=2), encoding="utf-8")
    if stored.get("access_token"):
        upsert_env({"KITE_ACCESS_TOKEN": str(stored["access_token"])})
    return session_path


def clear_session(path: Path | None = None) -> None:
    session_path = path or SESSION_PATH
    if session_path.exists():
        session_path.unlink()
    upsert_env({"KITE_ACCESS_TOKEN": ""})


def login_url(api_key: str) -> str:
    from kiteconnect import KiteConnect

    return KiteConnect(api_key=api_key).login_url()


def exchange_request_token(request_token: str, settings: Settings | None = None) -> dict[str, Any]:
    from kiteconnect import KiteConnect

    settings = settings or load_settings()
    if not settings.kite_api_key or not settings.kite_api_secret:
        raise RuntimeError("Save KITE_API_KEY and KITE_API_SECRET in .env before connecting.")
    kite = KiteConnect(api_key=settings.kite_api_key)
    session = kite.generate_session(request_token, api_secret=settings.kite_api_secret)
    save_session(session)
    return session


def kite_client(settings: Settings | None = None):
    from kiteconnect import KiteConnect

    settings = settings or load_settings()
    if not settings.kite_api_key:
        raise RuntimeError("KITE_API_KEY is missing.")
    kite = KiteConnect(api_key=settings.kite_api_key)
    if settings.kite_access_token:
        kite.set_access_token(settings.kite_access_token)
    return kite


def profile_status(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    if not settings.kite_api_key:
        return {"ok": False, "reason": "missing_api_key"}
    if not settings.kite_access_token:
        return {"ok": False, "reason": "missing_access_token"}
    try:
        kite = kite_client(settings)
        profile = kite.profile()
        return {
            "ok": True,
            "user_id": profile.get("user_id"),
            "user_name": profile.get("user_name"),
            "email": profile.get("email"),
            "exchanges": profile.get("exchanges"),
        }
    except Exception as exc:
        logger.warning("Kite profile check failed: %s", exc)
        return {"ok": False, "reason": "token_invalid_or_expired", "error": str(exc)}


def fetch_ltp(symbols: list[str], settings: Settings | None = None) -> dict[str, float]:
    if not symbols:
        return {}
    kite = kite_client(settings)
    keys = [f"NSE:{symbol.upper()}" for symbol in symbols]
    raw = kite.ltp(keys) or {}
    out: dict[str, float] = {}
    for key, payload in raw.items():
        symbol = key.split(":", 1)[-1]
        price = payload.get("last_price") if isinstance(payload, dict) else None
        if price is not None:
            out[symbol] = float(price)
    return out
