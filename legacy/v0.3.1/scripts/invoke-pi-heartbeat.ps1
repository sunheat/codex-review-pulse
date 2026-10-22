param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryPath,
    [Parameter(Mandatory = $true)]
    [string]$PullRequestUrl,
    [string]$ScheduledTaskName = "Pi Codex Review Pulse"
)

$ErrorActionPreference = "Stop"
$resolvedRepository = (Resolve-Path -LiteralPath $RepositoryPath).Path
$skillRoot = Split-Path -Parent $PSScriptRoot
$slug = [Convert]::ToHexString(
    [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($PullRequestUrl))
).Substring(0, 16).ToLowerInvariant()
$sessionRoot = Join-Path $env:USERPROFILE ".pi\agent\codex-review-pulse\$slug\sessions"
New-Item -ItemType Directory -Force -Path $sessionRoot | Out-Null

$createdNew = $false
$mutex = [Threading.Mutex]::new($true, "Local\PiCodexReviewPulse-$slug", [ref]$createdNew)
if (-not $createdNew) {
    Write-Output "Another pulse for this pull request is still running; skipping this interval."
    exit 0
}

$locationPushed = $false
try {
    Push-Location -LiteralPath $resolvedRepository
    $locationPushed = $true
    $prompt = @"
Use `$codex-review-pulse for exactly one scheduled cycle.
Pull request: $PullRequestUrl
Scheduler: Windows Task Scheduler task named "$ScheduledTaskName".
This is Pi, so Codex app task inspection is unavailable; follow the bounded GitHub fallback.
Continue the isolated session's previous pulse state. Perform only authorized PR fixes, issues, thread resolution, commits, pushes, and the single allowed trigger comment. Never merge or force-push.
If the skill reaches its immediate approval terminal condition or a stalled state, disable the named scheduled task and report why.
"@
    & pi --skill $skillRoot --session-dir $sessionRoot --continue --print $prompt
    if ($LASTEXITCODE -ne 0) {
        throw "Pi pulse exited with code $LASTEXITCODE."
    }
}
finally {
    if ($locationPushed) {
        Pop-Location
    }
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
