import sqlite3

c = sqlite3.connect("data/alpha_council.db")
c.row_factory = sqlite3.Row

for table in ["orders", "fills", "execution_calibrations", "trade_journal"]:
    rows = list(c.execute(f"SELECT * FROM {table}"))
    print(f"{table}: {len(rows)} rows")
    for r in rows:
        print("   ", dict(r))
    print()
