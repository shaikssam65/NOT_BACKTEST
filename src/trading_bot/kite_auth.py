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
    """Write key=value into .env when possible; always update process env."""
    import os

    for key, value in updates.items():
        os.environ[key] = value
    try:
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
    except OSError:
        # Streamlit Cloud / read-only hosts: secrets live in st.secrets + os.environ.
        logger.info("Could not write .env; using process environment only")


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


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def save_session(payload: dict[str, Any], path: Path | None = None) -> Path | None:
    session_path = path or SESSION_PATH
    login_time = payload.get("login_time")
    stored = {
        "access_token": _as_text(payload.get("access_token")),
        "public_token": _as_text(payload.get("public_token")),
        "user_id": _as_text(payload.get("user_id")),
        "user_name": _as_text(payload.get("user_name")),
        "email": _as_text(payload.get("email")),
        "login_time": _as_text(login_time) or datetime.now(timezone.utc).isoformat(),
        "saved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    try:
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text(json.dumps(stored, indent=2, default=str), encoding="utf-8")
    except OSError:
        logger.info("Could not persist kite_session.json; token kept in environment only")
        session_path = None  # type: ignore[assignment]
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
    # Return JSON-safe fields only (Kite may include datetime objects).
    return {
        "access_token": _as_text(session.get("access_token")),
        "public_token": _as_text(session.get("public_token")),
        "user_id": _as_text(session.get("user_id")),
        "user_name": _as_text(session.get("user_name")),
        "email": _as_text(session.get("email")),
        "login_time": _as_text(session.get("login_time")),
    }


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


def fetch_outbound_ip() -> dict[str, Any]:
    """
    Public IP this app uses for outbound HTTPS (what Zerodha Kite sees).
    Streamlit Community Cloud IPs are shared and can change.
    """
    import httpx

    urls = (
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
    )
    last_err = ""
    with httpx.Client(timeout=8.0, follow_redirects=True) as client:
        for url in urls:
            try:
                r = client.get(url)
                if r.status_code != 200:
                    last_err = f"{url} status {r.status_code}"
                    continue
                ip = r.text.strip().split()[0]
                if ip and all(c.isdigit() or c == "." for c in ip):
                    return {"ok": True, "ip": ip, "source": url}
                last_err = f"bad body from {url}: {r.text[:40]!r}"
            except Exception as exc:
                last_err = str(exc)
    return {"ok": False, "ip": None, "error": last_err or "lookup_failed"}


# Shared Streamlit Community Cloud egress IPs (community-reported; may change).
STREAMLIT_CLOUD_EGRESS_IPS: tuple[str, ...] = (
    "35.230.127.150",
    "35.203.151.101",
    "34.19.100.134",
    "34.83.176.217",
    "35.230.58.211",
    "35.203.187.165",
    "35.185.209.55",
    "34.127.88.74",
    "34.127.0.121",
    "35.230.78.192",
    "35.247.110.67",
    "35.197.92.111",
    "34.168.247.159",
    "35.230.56.30",
    "34.127.33.101",
    "35.227.190.87",
    "35.199.156.97",
    "34.82.135.155",
)