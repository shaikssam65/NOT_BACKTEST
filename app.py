"""Streamlit Cloud entrypoint. Set Main file path to: app.py"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _apply_streamlit_secrets() -> None:
    try:
        import streamlit as st

        secrets = st.secrets
    except Exception:
        return
    keys = (
        "PAPER_MODE",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "KITE_API_KEY",
        "KITE_API_SECRET",
        "KITE_ACCESS_TOKEN",
        "KITE_REDIRECT_URL",
        "DATABASE_PATH",
    )
    for key in keys:
        try:
            value = secrets[key]
        except Exception:
            continue
        if value is None:
            continue
        text = str(value).strip()
        if text:
            os.environ[key] = text
    os.environ.setdefault("PAPER_MODE", "true")
    # Community Cloud markers — use ephemeral /tmp SQLite.
    if os.getenv("STREAMLIT_RUNTIME_ENV", "").lower() == "cloud" or "STREAMLIT_SHARING_MODE" in os.environ:
        os.environ.setdefault("DATABASE_PATH", "/tmp/trading_bot/trading.db")


_apply_streamlit_secrets()

# Dashboard module calls main() on import (Streamlit script pattern).
import trading_bot.dashboard  # noqa: E402,F401
