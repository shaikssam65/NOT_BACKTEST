# Weekly under-₹50 buys — 2–3 medium established names, split capital.
# Windows Task Scheduler:
#   Trigger: Weekly on Monday at 10:15 AM IST
#   Action:  powershell.exe -File D:\NOT_BACKTEST\scripts\run_weekly_under50.ps1

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
python -m trading_bot auto-trade --strategy small_swing --capital 100000
