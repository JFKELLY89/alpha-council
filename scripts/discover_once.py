"""
Alpha Council v2.4 - single discovery + Stage-0 run.

Probes screener entitlements, assembles the discovery universe, runs the
fast screen, and prints the funnel. Calls no LLM and fetches no option
chains, by design.

Place at: scripts/discover_once.py

Usage:
    uv run python scripts/discover_once.py
    uv run python scripts/discover_once.py --core-only
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_council.alpaca.market_data import MarketDataService  # noqa: E402
from alpha_council.alpaca.rest_client import AlpacaRestClient  # noqa: E402
from alpha_council.alpaca.screeners import AssetCatalog, ScreenerService  # noqa: E402
from alpha_council.db.config_store import ensure_config_version  # noqa: E402
from alpha_council.db.engine import Database  # noqa: E402
from alpha_council.models.discovery import FunnelSnapshot  # noqa: E402
from alpha_council.quant.discovery import DiscoveryService, UniverseManager  # noqa: E402
from alpha_council.settings import get_settings, load_yaml  # noqa: E402
from alpha_council.utils.ids import scan_id as make_scan_id  # noqa: E402
from alpha_council.utils.time import utc_now  # noqa: E402


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say("")
    say("=" * 74)
    say(title)
    say("=" * 74)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.assert_paper_only()

    scoring = load_yaml("scoring")
    universe_cfg = load_yaml("universe")
    core = universe_cfg.get("core_symbols", [])
    exclusions = {e["symbol"] for e in universe_cfg.get("exclusions", [])}
    disc_cfg = scoring.get("discovery", {})

    risk_cfg = load_yaml("risk_constitution")
    config_version = scoring.get("config_version", settings.config_version)
    scan = make_scan_id()

    rule("ALPHA COUNCIL - DISCOVERY + STAGE-0")
    say(f"  scan_id : {scan}")
    say(f"  core    : {len(core)} symbols ({len(exclusions)} excluded)")
    say(f"  cap     : {disc_cfg.get('max_dynamic_symbols', 250)}")
    say(f"  stage-0 : top {disc_cfg.get('stage0_top_n', 30)}")
    say(f"  config  : {config_version}")

    async with Database(settings.database_path) as db, \
            AlpacaRestClient(settings, scoring) as api:

        # gate_rejections.config_version is a foreign key; the parent row
        # must exist before any rejection is written.
        await ensure_config_version(db, config_version, scoring, risk_cfg,
                                    tier=1, note="discovery run")

        market = MarketDataService(api, db)
        catalog = AssetCatalog(api)
        screeners = ScreenerService(api, db)
        universe = UniverseManager(
            core, cap=disc_cfg.get("max_dynamic_symbols", 250),
            ttl_minutes=disc_cfg.get("dynamic_ttl_minutes", 90),
            exclusions=exclusions,
            name_lookup=lambda s: (catalog.get(s).name if catalog.get(s) else ""),
        )
        service = DiscoveryService(market, catalog, screeners, universe,
                                   db, scoring)

        rule("1. ASSET CATALOG")
        n = await catalog.load()
        if n == 0:
            say(f"  FAILED to load: {catalog.load_error}")
            say("  Discovery will fall back to Core only.")
        else:
            say(f"  {n:,} active US equities")
            say(f"  {catalog.options_enabled_count:,} with options enabled "
                f"(field: {catalog.options_field_found or 'NOT FOUND'})")
            if catalog.options_detection_failed:
                say("")
                say("  No asset reports options eligibility, so this API")
                say("  version names the field differently. Degrading to")
                say("  'unknown': eligibility defers to the options-contracts")
                say("  endpoint, which is authoritative. Run")
                say("  scripts/inspect_assets.py to identify the real field.")
            missing = [s for s in core if not catalog.is_eligible(s)]
            if missing:
                say(f"  Core symbols failing eligibility: {missing}")

        rule("2. SCREENER ENTITLEMENTS")
        if args.core_only:
            say("  skipped (--core-only)")
            probe = {}
        else:
            probe = await screeners.probe_entitlements()
            for name, r in probe.items():
                status = "AVAILABLE" if r["available"] else "UNAVAILABLE"
                extra = r.get("reason") or r.get("error") or ""
                say(f"  {name:<14} {status:<12} {extra}")
            if any(not r["available"] for r in probe.values()):
                say("")
                say("  A 403 here is expected on the Basic plan. These are")
                say("  OPTIONAL sources: the session continues on Core plus")
                say("  news injections, with no loss of trading capability.")

        rule("3. DISCOVERY UNIVERSE")
        if args.core_only:
            disc_cfg = {**disc_cfg, "enable_most_active": False,
                        "enable_movers": False}
            service.config = {**scoring, "discovery": disc_cfg}
        symbols = await service.refresh()
        say(f"  {len(symbols)} symbols after eligibility and cap")

        by_source: dict[str, int] = {}
        for s in symbols:
            key = str(universe.source_of(s))
            by_source[key] = by_source.get(key, 0) + 1
        for src, count in sorted(by_source.items(), key=lambda x: -x[1]):
            say(f"    {src:<16} {count}")

        rule("4. STAGE-0 FAST SCREEN")
        top_n = disc_cfg.get("stage0_top_n", 30)
        results = await service.stage0(symbols, top_n=top_n)
        say(f"  {len(results)} survivors of {len(symbols)}")
        say("")
        say(f"  {'SYM':<7}{'FAST':>7}{'DIR':>5}{'MOM':>7}{'RVOL':>7}"
            f"{'RS':>7}{'TREND':>7}  SOURCE")
        say("  " + "-" * 68)
        for r in results[:25]:
            arrow = "UP" if r.direction > 0 else "DN"
            say(f"  {r.symbol:<7}{r.fast_score:>7.1f}{arrow:>5}"
                f"{r.momentum:>7.1f}{r.relative_volume:>7.1f}"
                f"{r.relative_strength:>7.1f}{r.trend_regime:>7.1f}"
                f"  {r.source}")

        if service.last_backfill:
            filled = sum(1 for n in service.last_backfill.values() if n > 0)
            say(f"  backfilled {filled} newly discovered symbol(s)")

        rule("5. REJECTIONS")
        gates: dict[str, list[tuple[str, str]]] = {}
        for sym, gate, detail in universe.rejected:
            gates.setdefault(gate, []).append((sym, detail))
        for gate, items in sorted(gates.items(), key=lambda x: -len(x[1])):
            say(f"  {gate:<28} {len(items)}")
            for sym, detail in items[:4]:
                say(f"      {sym:<8} {str(detail)[:64]}")
            if len(items) > 4:
                say(f"      ... {len(items) - 4} more")

        await service.persist(scan, symbols, results)
        written = await service.persist_rejections(scan, config_version)
        say(f"  {written} rejection rows written")

        snapshot = FunnelSnapshot(
            scan_id=scan, as_of=utc_now(),
            discovery_count=len(symbols), stage0_survivors=len(results),
            prescore_survivors=0, options_prescreened=0,
            final_candidates=0, councils_started=0,
            source_counts=by_source,
        )
        await db.execute(
            "INSERT OR REPLACE INTO funnel_snapshots("
            "scan_id, as_of, discovery_count, stage0_survivors, "
            "prescore_survivors, options_prescreened, final_candidates, "
            "councils_started, event_track_count, momentum_track_count, "
            "source_counts_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot.scan_id, snapshot.as_of.isoformat(),
             snapshot.discovery_count, snapshot.stage0_survivors, 0, 0, 0, 0,
             0, 0, str(by_source).replace("'", '"')),
        )

        rule("SUMMARY")
        say(f"  discovery -> stage0 : {len(symbols)} -> {len(results)}")
        say(f"  screeners           : {screeners.status_summary()}")
        say(f"  client              : {api.stats()}")
        say("")
        say("  No option chains fetched. No LLM calls made.")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--core-only", action="store_true",
                    help="skip optional screeners")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
