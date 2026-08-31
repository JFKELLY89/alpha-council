import sqlite3
c = sqlite3.connect("data/alpha_council.db")
c.row_factory = sqlite3.Row
rows = list(c.execute(
    "SELECT event_type, message, context_json FROM system_events "
    "WHERE event_type = 'PARAM_DROPPED'"))
print(f"{len(rows)} PARAM_DROPPED events")
for r in rows:
    print(dict(r))
