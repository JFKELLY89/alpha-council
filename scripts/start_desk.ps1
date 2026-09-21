# Alpha Council launcher with a no-duplicate guard.
#
# Safe to run any number of times from any trigger (logon, scheduled task,
# by hand): if a run_alpha_council process already exists it does nothing,
# so triggers can overlap without ever spawning a second desk.
#
# Registered on the operator's machine as:
#   - a Startup-folder shortcut (fires at logon)
#   - scheduled task "AlphaCouncilDesk" (weekdays 08:20 ET)

$repo = "C:\Users\LAPTOP\Documents\alpha-council"

$running = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like '*run_alpha_council*' -and $_.Name -like '*python*' }
if ($running) {
    Write-Output "desk already running (PID $($running[0].ProcessId)); nothing to do"
    exit 0
}

$stamp = Get-Date -Format "yyyyMMdd_HHmm"
Start-Process -FilePath "uv" `
    -ArgumentList "run", "python", "scripts/run_alpha_council.py", "--max-trades", "25" `
    -WorkingDirectory $repo `
    -WindowStyle Hidden `
    -RedirectStandardOutput "$repo\data\run_$stamp.log" `
    -RedirectStandardError "$repo\data\run_$stamp.err.log"
Write-Output "desk started; log data\run_$stamp.log"
