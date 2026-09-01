import sqlite3
c = sqlite3.connect("data/alpha_council.db")
c.row_factory = sqlite3.Row
for table in ["decisions", "orders", "fills", "execution_calibrations",
              "trade_journal", "shadow_trades", "decision_attribution"]:
    rows = list(c.execute(f"SELECT * FROM {table}"))
    print(f"{table}: {len(rows)} rows")
    for r in rows[:5]:
        print("   ", dict(r))
    print()
