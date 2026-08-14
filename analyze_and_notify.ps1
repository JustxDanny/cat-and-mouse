# Daily Cat and Mouse - post-digest analyze + verdict push.
# Called by run_morning.ps1 AFTER digest.py succeeds. Self-contained and
# fully error-isolated: any failure here logs and exits 0 so the caller's
# existing flow is never blocked.
#
#   1. guard: today's digest_<date>.json must exist
#   2. run Claude headless on /digest-analyze --save (+ write verdict file)
#   3. send_verdict.py pushes the verdict to Telegram (sent LAST = most visible)
#
# Security/correctness (cross-review 2026-05-19_1455):
#   - PowerShell owns the contract: explicit $today injected into the prompt,
#     exact verdict_$today.txt enforced, path passed explicitly to Python.
#     The LLM only fills content.
#   - Least privilege: no Bash (prompt-injection RCE via fetched READMEs).
#     WebFetch stays - it is the skill's core function.
#
# ASCII-only: PS 5.1 reads no-BOM files as ANSI; non-ASCII breaks the parser.
#
# Manual test:  powershell -File analyze_and_notify.ps1 -Verbose

[CmdletBinding()]
param(
    [int]$TimeoutMin = 8
)

$ProjectRoot = "C:\Users\1.DANIEL-LAPTOP\projects\DenchikAgent"
$OutDir      = Join-Path $ProjectRoot "out"
$LogFile     = Join-Path $OutDir "analyze.log"
$PromptFile  = Join-Path $ProjectRoot "verdict_prompt.txt"

# Python: resolve via PATH, fall back to known install (mirrors claude.exe).
$Python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $Python) { $Python = "C:\Program Files\PyManager\python.exe" }

function ALog($msg) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LogFile -Value "[$stamp] $msg" -Encoding utf8
    Write-Verbose "[$stamp] $msg"
}

# Whole script is best-effort. Never let an exception escape to the caller.
try {
    ALog "=== analyze_and_notify start ==="

    $today      = (Get-Date).ToString('yyyy-MM-dd')
    $digestJson = Join-Path $OutDir "digest_$today.json"
    if (-not (Test-Path $digestJson)) {
        ALog "no $digestJson - nothing to analyze, skip."
        ALog "=== analyze_and_notify done (no-op) ==="
        exit 0
    }

    $Claude = (Get-Command claude.exe -ErrorAction SilentlyContinue).Source
    if (-not $Claude) { $Claude = "C:\Users\1.daniel-laptop\.local\bin\claude.exe" }
    if (-not (Test-Path $Claude)) {
        ALog "claude.exe not found (PATH or fallback) - abort."
        exit 0
    }
    if (-not (Test-Path $PromptFile)) {
        ALog "verdict_prompt.txt missing - abort."
        exit 0
    }
    if (-not $Python -or -not (Test-Path $Python)) {
        ALog "python.exe not found - abort."
        exit 0
    }

    # Date injection: PS owns the date, the LLM does not infer it. Write a
    # per-run prompt with <DATE> resolved, pipe THAT via stdin.
    $promptRaw     = Get-Content -Path $PromptFile -Raw -Encoding utf8
    $promptRuntime = Join-Path $OutDir "verdict_prompt.runtime.txt"
    ($promptRaw -replace '<DATE>', $today) | Set-Content -Path $promptRuntime -Encoding utf8 -NoNewline

    # Account safety (hard rule): unattended runs must bill a metered API key,
    # never the subscription OAuth session (Anthropic Consumer Terms sec. 3 -
    # no automated access except via API key). Key is read from .env; without
    # it we SKIP the analyze pass rather than run non-compliant.
    $apiKey = ""
    foreach ($l in Get-Content (Join-Path $ProjectRoot ".env") -ErrorAction SilentlyContinue) {
        if ($l -match '^\s*ANTHROPIC_API_KEY\s*=\s*(.+)$') { $apiKey = $Matches[1].Trim().Trim('"').Trim("'"); break }
    }
    if (-not $apiKey) {
        ALog "no ANTHROPIC_API_KEY in .env - SKIPPING analyze: unattended claude on subscription OAuth violates the account-safety rule. Add a metered key (console.anthropic.com) to .env to restore this step."
        ALog "=== analyze_and_notify done (no api key) ==="
        exit 0
    }
    $env:ANTHROPIC_API_KEY = $apiKey   # child claude.exe inherits -> bills the metered key

    # Least privilege: NO Bash (RCE via prompt injection in fetched READMEs).
    # WebFetch required (skill core). Write required for --save archive.
    $allowed = "WebFetch Read Glob Grep Write"

    $stdoutF = Join-Path $OutDir "analyze_claude.out"
    $stderrF = Join-Path $OutDir "analyze_claude.err"

    ALog "launching claude headless (timeout ${TimeoutMin}m, allowedTools='$allowed', date=$today)"
    Set-Location $ProjectRoot

    # Prompt via STDIN, never argv (multi-line in -ArgumentList word-splits;
    # verified end-to-end that the slash command fires through stdin).
    $p = Start-Process -FilePath $Claude -ArgumentList @("-p", "--allowedTools", $allowed) -WorkingDirectory $ProjectRoot -RedirectStandardInput $promptRuntime -RedirectStandardOutput $stdoutF -RedirectStandardError $stderrF -WindowStyle Hidden -PassThru

    if (-not $p.WaitForExit($TimeoutMin * 60 * 1000)) {
        ALog "claude headless TIMEOUT after ${TimeoutMin}m - tree-killing PID $($p.Id)."
        # PS5.1 $p.Kill() is not recursive; claude.exe spawns node children.
        & taskkill /F /T /PID $p.Id 2>&1 | Out-Null
        ALog "=== analyze_and_notify done (timeout) ==="
        exit 0
    }
    ALog "claude headless exit=$($p.ExitCode)"
    $errTail = (Get-Content $stderrF -Tail 3 -ErrorAction SilentlyContinue) -join " | "
    if ($errTail) { ALog "claude stderr tail: $errTail" }

    # Strict contract: exactly today's file. No glob-newest (it would silently
    # broadcast a stale or attacker-future-dated verdict on analyze failure).
    $verdict = Join-Path $OutDir "verdict_$today.txt"
    if (-not (Test-Path $verdict)) {
        ALog "no verdict_$today.txt produced - analyze pass failed, no Telegram push."
        ALog "=== analyze_and_notify done (no verdict) ==="
        exit 0
    }
    ALog "verdict file: verdict_$today.txt"

    ALog "sending verdict via send_verdict.py"
    & $Python (Join-Path $ProjectRoot "send_verdict.py") $verdict
    ALog "send_verdict.py exit=$LASTEXITCODE"

    ALog "=== analyze_and_notify done ==="
    exit 0
}
catch {
    ALog "UNHANDLED: $($_.Exception.ToString())"
    exit 0
}
