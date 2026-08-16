# Run daily auto-trade (paper) — schedule this with Windows Task Scheduler
# for every market morning, e.g. 9:20 AM IST.
# Example Task Scheduler action:
#   Program: powershell.exe
#   Args:    -File D:\NOT_BACKTEST\scripts\run_daily_auto_trade.ps1

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
python -m trading_bot auto-trade --strategy ensemble --capital 100000
