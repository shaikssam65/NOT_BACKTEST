from __future__ import annotations

from pathlib import Path

from trading_bot.kite_auth import kite_redirect_url, load_session, save_session, upsert_env


def test_upsert_env_preserves_other_keys(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("PAPER_MODE=true\nOPENAI_API_KEY=old\n", encoding="utf-8")
    upsert_env({"OPENAI_API_KEY": "new-key", "KITE_API_KEY": "abc"}, path=env)
    text = env.read_text(encoding="utf-8")
    assert "PAPER_MODE=true" in text
    assert "OPENAI_API_KEY=new-key" in text
    assert "KITE_API_KEY=abc" in text
    assert "old" not in text


def test_save_and_load_session(tmp_path: Path, monkeypatch):
    session_path = tmp_path / "kite_session.json"
    env_path = tmp_path / ".env"
    env_path.write_text("PAPER_MODE=true\n", encoding="utf-8")
    monkeypatch.setattr("trading_bot.kite_auth.ENV_PATH", env_path)
    save_session(
        {
            "access_token": "tok_123",
            "user_id": "AR5852",
            "user_name": "Test",
        },
        path=session_path,
    )
    loaded = load_session(session_path)
    assert loaded is not None
    assert loaded["access_token"] == "tok_123"
    assert loaded["user_id"] == "AR5852"
    assert "KITE_ACCESS_TOKEN=tok_123" in env_path.read_text(encoding="utf-8")


def test_redirect_url_has_trailing_slash(settings):
    url = kite_redirect_url(settings)
    assert url.startswith("http://127.0.0.1")
    assert url.endswith("/")
