"""Streamlit Cloud entrypoint. Main file path: app.py"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import streamlit as st

# MUST be the first Streamlit command — otherwise Cloud shows a blank page.
st.set_page_config(page_title="NSE Simple Bot", layout="wide")


def _apply_streamlit_secrets() -> None:
    try:
        secrets = st.secrets
    except Exception:
        return
    keys = (
        "PAPER_MODE",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "FINNHUB_API_KEY",
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
    os.environ.setdefault("PAPER_MODE", "false")
    # Default cloud redirect if not set in secrets yet.
    os.environ.setdefault("KITE_REDIRECT_URL", "https://backtestind.streamlit.app/")
    if os.getenv("STREAMLIT_RUNTIME_ENV", "").lower() == "cloud" or "STREAMLIT_SHARING_MODE" in os.environ:
        os.environ.setdefault("DATABASE_PATH", "/tmp/trading_bot/trading.db")


_apply_streamlit_secrets()

from trading_bot.dashboard import main

main()
