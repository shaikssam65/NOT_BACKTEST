# Secrets — never commit real keys

This repo runs with a **local** `.env` file (or GitHub Actions secrets for CI only).

## Why keys are not in GitHub

If `OPENAI_API_KEY`, `KITE_API_KEY`, or `KITE_API_SECRET` are committed, anyone with repo access can trade/use your accounts. `.env` is gitignored on purpose.

## Local setup (required for the bot to work)

```bash
copy .env.example .env
```

Edit `.env` and set:

- `OPENAI_API_KEY`
- `KITE_API_KEY`
- `KITE_API_SECRET`
- `KITE_REDIRECT_URL=http://127.0.0.1:8501/`
- `PAPER_MODE=true`

Or paste keys in the Streamlit **Setup & Kite** tab after `python -m trading_bot dashboard`.

Kite login still needs this PC at `http://127.0.0.1:8501/` — GitHub hosting cannot replace that redirect URL for personal Connect apps.

## GitHub Actions (optional CI)

Store the same names under **Repo → Settings → Secrets and variables → Actions**. Never paste them into workflow YAML files as plain text.

## If you already pasted keys in chat or a public place

1. OpenAI: revoke/rotate at https://platform.openai.com/api-keys  
2. Kite: regenerate secret on https://developers.kite.trade/apps → your app  
3. Update local `.env` with the new values  
