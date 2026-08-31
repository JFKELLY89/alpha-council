import sqlite3
from alpha_council.settings import get_settings

c = sqlite3.connect(f"file:{get_settings().database_path}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
rows = list(c.execute(
    "SELECT occurred_at, level, component, event_type, message "
    "FROM system_events ORDER BY occurred_at DESC LIMIT 25"))
print(f"{len(rows)} recent events")
for r in rows:
    print(f"{r['occurred_at'][11:19]} {r['level']:<6}{r['component']:<18}"
          f"{r['event_type']:<28}{r['message'][:70]}")
