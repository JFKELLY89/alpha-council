import sqlite3
c = sqlite3.connect("data/alpha_council.db")
c.row_factory = sqlite3.Row
rows = list(c.execute("SELECT * FROM agent_runs ORDER BY started_at"))
print(f"{len(rows)} agent runs")
print()
for r in rows:
    print(f"{r['agent_name']:<22}{r['model']:<18}"
          f"in={r['input_tokens'] or 0:>6} out={r['output_tokens'] or 0:>6} "
          f"${r['cost_usd'] or 0:.4f}  {r['status']}")
print()
total = sum(r["cost_usd"] or 0 for r in rows)
print(f"total ${total:.4f}")
