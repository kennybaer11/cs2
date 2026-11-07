# Betano's CS2 round lines, collected from this PC.
#
# Betano refuses the posession server (HTTP 403 on every request since at
# least 6 Nov 2026, tennis too), so the lines are fetched here, as the tennis
# Betano lines are: fetch, price, write the tips, tell Telegram about new and
# changed ones. Results and settlement still run on the server (run_cs2.sh).
#
#   powershell -ExecutionPolicy Bypass -File C:\Users\kenny\cs2\betano_local.ps1
#
# Telegram credentials are read from the posession scraper's .env.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$env:PYTHONUTF8 = "1"
$env:PYTHONWARNINGS = "ignore"

foreach ($line in Get-Content "C:\Users\kenny\posession\scraper\.env") {
    if ($line -match '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID)=(.*)$') {
        Set-Item -Path "env:$($Matches[1])" -Value $Matches[2].Trim('"')
    }
}

& $py odds.py
if ($LASTEXITCODE -ne 0) { Write-Host "!! odds.py exited $LASTEXITCODE - stopping"; exit 1 }
& $py price.py --store --top 0
& $py advice.py
& $py notify.py
