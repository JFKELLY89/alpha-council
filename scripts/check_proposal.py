import sqlite3
from alpha_council.settings import get_settings
c = sqlite3.connect(f"file:{get_settings().database_path}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
print("newest decisions:")
for r in c.execute("SELECT decision_id, symbol, state FROM decisions "
                   "ORDER BY created_at DESC LIMIT 3"):
    print("  ", dict(r))
print()
print("what the PM returned:")
for r in c.execute("SELECT substr(output_json,1,200) o FROM agent_runs "
                   "WHERE agent_name='portfolio_manager' "
                   "ORDER BY started_at DESC LIMIT 1"):
    print("  ", r["o"])
