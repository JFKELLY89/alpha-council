import sqlite3
c = sqlite3.connect("data/alpha_council.db")
c.row_factory = sqlite3.Row
rows = list(c.execute(
    "SELECT occurred_at, level, component, event_type, message "
    "FROM system_events WHERE occurred_at > '2026-09-01' "
    "ORDER BY occurred_at DESC LIMIT 25"))
print(f"{len(rows)} events today")
for r in rows:
    print(f"{r['occurred_at'][11:19]} {r['level']:<6}{r['component']:<18}"
          f"{r['event_type']:<28}{r['message'][:60]}")
