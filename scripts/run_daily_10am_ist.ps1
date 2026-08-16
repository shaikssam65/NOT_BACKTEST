# Daily 10:00 AM IST — auto-sell positions at ≥30% profit (and stop/target checks).
# Windows Task Scheduler:
#   Trigger: Daily at 10:00 (set timezone to India Standard Time)
#   Action:  powershell.exe -File D:\NOT_BACKTEST\scripts\run_daily_10am_ist.ps1

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
python -m trading_bot take-profits
