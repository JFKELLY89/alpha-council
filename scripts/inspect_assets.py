"""
Alpha Council v2.4 - asset payload shape diagnostic.

The catalog reported 0 of 14,260 assets as options-enabled, which means the
field name in this API version is not one we guessed. This dumps the actual
payload for a few known-optionable symbols so the shape can be read rather
than guessed.

Place at: scripts/inspect_assets.py

Usage:
    uv run python scripts/inspect_assets.py
    uv run python scripts/inspect_assets.py --symbols SPY,NVDA,AMD
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_council.alpaca.rest_client import AlpacaRestClient  # noqa: E402
from alpha_council.alpaca.screeners import (  # noqa: E402
    OPTIONS_FLAG_CANDIDATES,
    detect_options_flag,
)
from alpha_council.settings import get_settings  # noqa: E402

KNOWN_OPTIONABLE = ["SPY", "NVDA", "AMD", "AAPL"]


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say("")
    say("=" * 70)
    say(title)
    say("=" * 70)


async def run(symbols: list[str]) -> int:
    settings = get_settings()
    settings.assert_paper_only()

    async with AlpacaRestClient(settings) as api:
        rule("1. SINGLE ASSET PAYLOAD")
        sample = symbols[0]
        one = await api._get(f"{api.trade_base}/v2/assets/{sample}")
        say(f"  GET /v2/assets/{sample}")
        say("")
        say(json.dumps(one, indent=2, sort_keys=True)[:2500])

        rule("2. FIELD INVENTORY")
        say(f"  top-level keys: {sorted(one.keys())}")
        attrs = one.get("attributes")
        say(f"  attributes    : {attrs!r} (type {type(attrs).__name__})")
        say("")
        say("  candidate options fields checked:")
        for key in OPTIONS_FLAG_CANDIDATES:
            present = key in one
            value = one.get(key)
            say(f"    {key:<20} present={present!s:<6} value={value!r}")
        found, field = detect_options_flag(one)
        say("")
        say(f"  detect_options_flag -> {found} via {field!r}")

        rule("3. KNOWN-OPTIONABLE COMPARISON")
        for sym in symbols:
            try:
                a = await api._get(f"{api.trade_base}/v2/assets/{sym}")
            except Exception as exc:  # noqa: BLE001
                say(f"  {sym:<6} error: {exc}")
                continue
            ok, field = detect_options_flag(a)
            say(f"  {sym:<6} detected={ok!s:<6} field={field!r} "
                f"attributes={a.get('attributes')!r}")

        rule("4. BULK SCAN")
        payload = await api._get(f"{api.trade_base}/v2/assets",
                                 {"status": "active", "asset_class": "us_equity"})
        rows = payload if isinstance(payload, list) else payload.get("assets", [])
        say(f"  {len(rows):,} assets")

        key_counts: Counter[str] = Counter()
        attr_values: Counter[str] = Counter()
        for a in rows:
            key_counts.update(a.keys())
            at = a.get("attributes")
            if isinstance(at, list):
                attr_values.update(at)
            elif isinstance(at, dict):
                attr_values.update(at.keys())

        say("")
        say("  keys present across the catalog:")
        for k, n in key_counts.most_common():
            marker = "  <-- candidate" if "option" in k.lower() else ""
            say(f"    {k:<32} {n:>7,}{marker}")

        say("")
        say("  distinct attribute values:")
        if attr_values:
            for v, n in attr_values.most_common(30):
                marker = "  <-- candidate" if "option" in str(v).lower() else ""
                say(f"    {str(v):<32} {n:>7,}{marker}")
        else:
            say("    (none present)")

        detected = sum(1 for a in rows if detect_options_flag(a)[0])
        say("")
        say(f"  detected as options-enabled: {detected:,} of {len(rows):,}")
        if detected == 0:
            say("")
            say("  No field carries options eligibility in this response.")
            say("  Eligibility will defer to /v2/options/contracts, which is")
            say("  authoritative: a symbol with contracts in the DTE window is")
            say("  optionable by definition.")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=str, default=",".join(KNOWN_OPTIONABLE))
    syms = [s.strip().upper() for s in ap.parse_args().symbols.split(",") if s.strip()]
    return asyncio.run(run(syms))


if __name__ == "__main__":
    sys.exit(main())
