# Usage

## Codex

Place the skill at `~/.codex/skills/codex-review-pulse` or another configured
skill directory. Start a one-time cycle with:

```text
Use $codex-review-pulse on https://github.com/OWNER/REPO/pull/NUMBER.
The approval and targeted reviewer identities are independently configured as
chatgpt-codex-connector. I authorize PR-scoped fixes, commits, pushes,
exact thread resolution, and one Codex review trigger comment. Do not merge.
```

Add issue creation only when it is authorized. For recurring monitoring in the
Codex desktop app, ask Codex to create a heartbeat automation and state the
interval, PR identity, allowed mutations, approval logins, and stalled-review
policy. Reviewer logins are configured separately from approval logins. Keep
one heartbeat attached to one task so the task history retains batch and
idle-state evidence.

Ask Codex to view, pause, resume, or delete the automation instead of editing
its local configuration while it runs. A heartbeat must stop immediately when
the authoritative snapshot reports zero targeted Codex threads and a
checkpoint proves a qualifying PR-level thumbs-up for the current head; it does
not wait for a quiet interval. Non-target unresolved threads remain reported
and untouched.

Fetch and checkpoint state explicitly when diagnosing a cycle:

```powershell
python skills/codex-review-pulse/scripts/fetch_pr_state.py `
  --repo OWNER/REPO `
  --pr NUMBER `
  --reviewer-login chatgpt-codex-connector `
  --approval-login chatgpt-codex-connector
```

An existing qualifying reaction on cold start is ambiguous. Do not delete the
checkpoint to make approval appear fresh.

Freeze only the IDs returned by that stable snapshot, then persist an outcome
before resolving each thread:

```powershell
python skills/codex-review-pulse/scripts/update_batch_state.py `
  --repo OWNER/REPO --pr NUMBER `
  freeze --head-oid HEAD_OID --thread-id THREAD_ID
python skills/codex-review-pulse/scripts/update_batch_state.py `
  --repo OWNER/REPO --pr NUMBER `
  record-outcome --thread-id THREAD_ID --classification fix-now `
  --reference "focused checks passed"
python skills/codex-review-pulse/scripts/resolve_thread.py THREAD_ID `
  --repo OWNER/REPO --pr NUMBER
```

If the bracketing head reads differ, discard the output and fetch again. Do not
freeze IDs from a mixed-head snapshot. Custom reviewer logins are persisted in
the frozen batch and reused by checkpoint-driven resolution. Standalone
explicit expected-set calls must provide their reviewer logins and cannot
override an active batch.

Codex can inspect another task only when task-list/read/status capabilities are
available and the task matches the exact PR. If no genuine cancel/interrupt
capability exists, do not substitute archive for cancellation.

## Pi interactive use

Pi can load the skill directory directly. In PowerShell, start Pi from the
target repository:

```powershell
pi --skill "$env:USERPROFILE\.codex\skills\codex-review-pulse"
```

Then invoke:

```text
/skill:codex-review-pulse Process one cycle for
https://github.com/OWNER/REPO/pull/NUMBER. I authorize PR-scoped fixes,
commits, pushes, exact thread resolution, and one review trigger comment.
Do not merge.
```

To make the skill discoverable, add its exact directory to
`~/.pi/agent/settings.json` and restart Pi or run `/reload`:

```json
{
  "skills": ["~/.codex/skills/codex-review-pulse"]
}
```

## Pi scheduled use on Windows

Pi does not provide a built-in recurring scheduler. The included wrapper keeps
Pi session history isolated per PR and uses a named mutex so scheduled cycles
cannot overlap.

Test one cycle:

```powershell
$runner = "$env:USERPROFILE\.codex\skills\codex-review-pulse\scripts\invoke-pi-heartbeat.ps1"
& $runner `
  -RepositoryPath "C:\path\to\repo" `
  -PullRequestUrl "https://github.com/OWNER/REPO/pull/NUMBER" `
  -ScheduledTaskName "Pi Codex Review Pulse NUMBER"
```

After the test succeeds, register a 15-minute task:

```powershell
$taskName = "Pi Codex Review Pulse NUMBER"
$runner = "$env:USERPROFILE\.codex\skills\codex-review-pulse\scripts\invoke-pi-heartbeat.ps1"
$repo = "C:\path\to\repo"
$pr = "https://github.com/OWNER/REPO/pull/NUMBER"
$pwsh = (Get-Command pwsh).Source
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -RepositoryPath `"$repo`" -PullRequestUrl `"$pr`" -ScheduledTaskName `"$taskName`""
$action = New-ScheduledTaskAction -Execute $pwsh -Argument $arguments
$trigger = New-ScheduledTaskTrigger `
  -Once `
  -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes 15) `
  -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
  -MultipleInstances IgnoreNew `
  -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask `
  -TaskName $taskName `
  -Action $action `
  -Trigger $trigger `
  -Settings $settings `
  -Description "Run Codex Review Pulse for one GitHub pull request."
```

Inspect and control the task with:

```powershell
Get-ScheduledTask -TaskName $taskName
Start-ScheduledTask -TaskName $taskName
Disable-ScheduledTask -TaskName $taskName
Enable-ScheduledTask -TaskName $taskName
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
```

Pi sessions are stored under
`~/.pi/agent/codex-review-pulse/<PR-hash>/sessions`. Pi cannot inspect Codex
desktop or cloud task state, so the scheduled prompt uses the bounded,
single-comment GitHub fallback.
