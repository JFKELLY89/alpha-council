# Alpha Council launcher with a health-based no-duplicate guard (F15).
#
# Safe to run any number of times from any trigger (logon, scheduled task,
# by hand). A desk process only counts as running if the database shows
# recent writes: a startup that wedges before SESSION_STARTED holds a
# process-exists guard open forever (2026-11-17: a four-day zombie that
# neutralized every weekday-morning restart). The position monitor writes
# a heartbeat on every 2-minute poll, so a healthy desk touches the DB or
# its -wal file around the clock; a stale file means a zombie, which is
# killed and replaced.
#
# Registered on the operator's machine as:
#   - a Startup-folder shortcut (fires at logon)
#   - scheduled task "AlphaCouncilDesk" (weekdays 08:20 ET)

$repo = "C:\Users\LAPTOP\Documents\alpha-council"
$staleMinutes = 90

$running = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like '*run_alpha_council*' -and $_.Name -like '*python*' }
if ($running) {
    $newest = Get-ChildItem "$repo\data\alpha_council.db*" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($newest -and ($newest.LastWriteTime -gt (Get-Date).AddMinutes(-$staleMinutes))) {
        Write-Output "desk already running (PID $($running[0].ProcessId)); DB fresh; nothing to do"
        exit 0
    }
    Write-Output "desk processes exist but the DB is stale (> $staleMinutes min): zombie; replacing"
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -and ($_.CommandLine -match 'run_alpha_council|alpaca-mcp-server') } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }
    Start-Sleep -Seconds 3
}

$stamp = Get-Date -Format "yyyyMMdd_HHmm"
Start-Process -FilePath "uv" `
    -ArgumentList "run", "python", "scripts/run_alpha_council.py", "--max-trades", "25" `
    -WorkingDirectory $repo `
    -WindowStyle Hidden `
    -RedirectStandardOutput "$repo\data\run_$stamp.log" `
    -RedirectStandardError "$repo\data\run_$stamp.err.log"
Write-Output "desk started; log data\run_$stamp.log"
