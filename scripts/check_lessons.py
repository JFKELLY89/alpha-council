import sqlite3
from alpha_council.settings import get_settings
c = sqlite3.connect(f"file:{get_settings().database_path}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
for r in c.execute(
        "SELECT occurred_at, event_type, message, context_json "
        "FROM system_events WHERE component='lessons' "
        "ORDER BY occurred_at DESC LIMIT 3"):
    print(r["occurred_at"], r["event_type"])
    print(" ", r["message"])
print()
for r in c.execute(
        "SELECT status, error, substr(output_json,1,1500) out "
        "FROM agent_runs WHERE purpose='lessons' "
        "ORDER BY started_at DESC LIMIT 1"):
    print("status:", r["status"])
    print("error :", r["error"])
    print("output:", r["out"])
