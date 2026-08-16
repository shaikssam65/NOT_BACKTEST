# Generic daily auto-trade (rules/agents/combined). Prefer specific scripts for under-₹50.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
python -m trading_bot auto-trade --strategy combined --capital 100000
