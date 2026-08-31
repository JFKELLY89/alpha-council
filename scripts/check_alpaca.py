import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from alpha_council.alpaca.rest_client import AlpacaRestClient
from alpha_council.settings import get_settings

async def main():
    s = get_settings()
    async with AlpacaRestClient(s) as api:
        pos = await api._get(f"{api.trade_base}/v2/positions")
        print(f"{len(pos)} open positions")
        for p in pos:
            print(f"  {p.get('symbol')}  qty={p.get('qty')}  "
                  f"side={p.get('side')}  avg={p.get('avg_entry_price')}  "
                  f"mv={p.get('market_value')}  upl={p.get('unrealized_pl')}")
        print()
        orders = await api._get(f"{api.trade_base}/v2/orders",
                                {"status": "all", "limit": 50})
        print(f"{len(orders)} orders")
        for o in orders:
            print(f"  {str(o.get('submitted_at'))[:19]} {o.get('symbol')} "
                  f"{o.get('side'):<5} {o.get('status'):<12} "
                  f"qty={o.get('qty')} filled={o.get('filled_avg_price')} "
                  f"class={o.get('order_class')}")
        print()
        acct = await api.get_account()
        print(f"equity ${float(acct['equity']):,.2f}  "
              f"last_equity ${float(acct['last_equity']):,.2f}")

asyncio.run(main())
