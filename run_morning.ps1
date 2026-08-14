# Cat & Mouse - twice-weekly launcher (Tuesday + Saturday, 11:00)
# Triggered by Windows Task Scheduler - weekly Tue/Sat 11:00 triggers + at-logon catch-up trigger.
# Idempotency flag (out/ran_<date>.flag) prevents double-fire when two triggers hit the same day.
#
#  1. bail unless today is a digest day (the at-logon trigger fires every day; the guard is what
#     keeps it to Tue/Sat)
#  2. wait for VPN (known client running + api.telegram.org reachable). Polls up to 60 min, quits clean.
#  3. run digest.py (fetch, cluster into themes, ship ONE theme to Telegram)
#  4. open VS Code on the project folder
#     (Chrome auto-open removed 2026-06-22 - URLs still in Telegram + out/latest_urls.txt.)
#
# The post-digest analyze step is gone as of 2026-08-04: analysis now runs inside digest.py against
# a LOCAL model (Ollama), so there is no Anthropic API key and no second Telegram message.
#
# Notes:
#   - PS 5.1: do NOT use 2>&1 with native exes (wraps stderr as ErrorRecord).
#   - --force bypasses the day guard, idempotency and VPN-wait (for manual testing).
#   - --skip-vpn skips just the VPN check (keeps the day guard and idempotency).

$ProjectRoot = "C:\Users\1.DANIEL-LAPTOP\projects\DenchikAgent"
$Python      = "C:\Program Files\PyManager\python.exe"
$Chrome      = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$VSCode      = "C:\Users\1.DANIEL-LAPTOP\AppData\Local\Programs\Microsoft VS Code\bin\code.cmd"
$LogDir      = Join-Path $ProjectRoot "out"
$LogFile     = Join-Path $LogDir "run_morning.log"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

function Log($msg) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LogFile -Value "[$stamp] $msg" -Encoding utf8
    Write-Host "[$stamp] $msg"
}

function Test-VpnReady {
    # Fast check: a known VPN client running AND TCP-reachable Telegram API.
    # Any of these counts — Daniel switches clients (AmneziaVPN since 2026-06, Karing/v2rayN before).
    $proc = Get-Process -Name "AmneziaVPN", "Karing", "v2rayN" -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    try {
        $tcp = New-Object Net.Sockets.TcpClient
        $async = $tcp.BeginConnect("api.telegram.org", 443, $null, $null)
        $hit = $async.AsyncWaitHandle.WaitOne(3000, $false)
        $ok = $hit -and $tcp.Connected
        $tcp.Close()
        return $ok
    } catch {
        return $false
    }
}

Log "=== run_morning start ==="

# Day guard + idempotency + VPN-wait
$DigestDays = @("Tuesday", "Saturday")
$Force   = ($args -contains "--force")
$SkipVpn = ($args -contains "--skip-vpn")
$Now = Get-Date
$TodayFlag = Join-Path $LogDir "ran_$($Now.ToString('yyyy-MM-dd')).flag"

if (-not $Force) {
    # The at-logon trigger fires every single day; this is what keeps the digest twice-weekly.
    if ($DigestDays -notcontains $Now.DayOfWeek.ToString()) {
        Log "$($Now.DayOfWeek) is not a digest day ($($DigestDays -join '/')) - skip."
        exit 0
    }
    if (Test-Path $TodayFlag) {
        Log "already ran today ($TodayFlag) - skip. Use --force to override."
        exit 0
    }
}

# VPN-ready loop. Polls every 30s up to 60 min. If still not up, exit 0 (next trigger will retry).
if (-not $Force -and -not $SkipVpn) {
    $VpnTimeoutMin = 60
    $VpnPollSec    = 30
    $VpnDeadline   = (Get-Date).AddMinutes($VpnTimeoutMin)
    $VpnReady      = $false

    while ((Get-Date) -lt $VpnDeadline) {
        if (Test-VpnReady) {
            $VpnReady = $true
            Log "VPN ready (Karing + api.telegram.org reachable)"
            break
        }
        Log "VPN not ready, retry in ${VpnPollSec}s (deadline $($VpnDeadline.ToString('HH:mm')))"
        Start-Sleep -Seconds $VpnPollSec
    }

    if (-not $VpnReady) {
        Log "VPN not up after $VpnTimeoutMin min - skip today. Next trigger (logon / next-day daily) will retry."
        exit 0
    }
}

Set-Location $ProjectRoot

# 1. fetch + send telegram
Log "running digest.py"
& $Python "$ProjectRoot\digest.py"
$digestExit = $LASTEXITCODE
Log "digest.py exit=$digestExit"
if ($digestExit -ne 0) {
    Log "digest.py FAILED - abort opening windows"
    exit 1
}

# 2. (Chrome auto-open disabled 2026-06-22 at Daniel's request — was cluttering the PC.
#    URLs still land in out/latest_urls.txt and the Telegram message; open manually if wanted.)

# 3. open VS Code on project (code.cmd is a batch wrapper -> launch via cmd /c)
Log "opening VS Code on $ProjectRoot"
if (Test-Path $VSCode) {
    Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", $VSCode, "-n", $ProjectRoot) -WindowStyle Hidden
} else {
    Log "VS Code launcher not found at $VSCode - skipping"
}

# Ran-flag written after a successful send, so a crash mid-fetch leaves the day
# open for the logon trigger to retry. The gap between sending and writing the
# flag is covered by the task's MultipleInstances=IgnoreNew - a second trigger
# during a run is dropped, so the same digest cannot go out twice.
New-Item -ItemType File -Path $TodayFlag -Force | Out-Null

Log "=== run_morning done ==="
