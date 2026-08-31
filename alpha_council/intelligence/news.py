"""
Alpha Council v2.5 - Alpaca news intelligence.

Turns the Alpaca news feed into scored IntelligenceEvents so the EVENT
track has something to reason about. Without this every candidate is
MOMENTUM with a null catalyst, and the Portfolio Manager correctly
abstains on the grounds that there is no material catalyst.

Four rules govern the scoring, all inherited from the spec:

  DEDUPLICATION IS PER SOURCE, NOT GLOBAL. Two outlets publishing the same
  wire story are two pieces of evidence about how widely it is carried,
  which is what corroboration measures. A global hash constraint would
  make corroboration structurally impossible.

  MARKET RESPONSE OUTRANKS HEADLINE TONE. A positive headline with a
  negative price response is meaningful evidence, not noise. Direction is
  derived from both and the disagreement is recorded.

  NO CONSENSUS IS INVENTED. Surprise defaults to a neutral 50 when there
  is no expectation data, because there is none available on this plan.

  EVENTS YOUNGER THAN FIVE MINUTES ARE PROVISIONAL. Early wire copy gets
  corrected, and a headline that has not yet been picked up elsewhere
  cannot be distinguished from one that never will be.

Consolidates what the spec splits across normalizer, deduplicator,
scoring, entity_mapper and event_builder.

Place at: alpha_council/intelligence/news.py
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Sequence

from rapidfuzz import fuzz

from alpha_council.alpaca.rest_client import AlpacaError, AlpacaRestClient
from alpha_council.db.engine import Database
from alpha_council.models.enums import Direction, SourceTier
from alpha_council.models.intelligence import IntelligenceEvent, IntelligenceItem
from alpha_council.utils.ids import content_hash, new_uuid
from alpha_council.utils.math import clip, freshness_score
from alpha_council.utils.time import iso_utc, parse_alpaca_ts, utc_now

SOURCE_ID = "alpaca_news"
TITLE_MATCH_THRESHOLD = 88          # rapidfuzz token_set_ratio for one story
PROVISIONAL_SECONDS = 300

# Publishers that originate rather than syndicate. Tier drives the
# reliability component and the freshness half-life.
TIER_1_PUBLISHERS = {"businesswire", "globenewswire", "pr newswire",
                     "prnewswire", "accesswire", "newsfile", "sec"}
TIER_2_PUBLISHERS = {"benzinga", "reuters", "bloomberg", "dow jones",
                     "marketwatch", "cnbc", "barron's", "wsj",
                     "the wall street journal", "associated press"}

# Event type -> (materiality range, freshness half-life in minutes).
# Ranges come from spec §10.5; the midpoint is used unless keyword
# strength justifies the top of the band.
EVENT_TYPES: dict[str, tuple[tuple[int, int], int]] = {
    "earnings_guidance": ((90, 100), 180),
    "m_and_a": ((75, 100), 120),
    "major_contract": ((75, 100), 120),
    "regulatory": ((75, 100), 120),
    "litigation": ((75, 100), 120),
    "capital_action": ((75, 100), 120),
    "executive_change": ((50, 80), 120),
    "product_launch": ((50, 80), 120),
    "analyst_commentary": ((30, 60), 720),
    "routine_filing": ((0, 40), 240),
}

# Ordered: the first pattern to match wins, so the most material
# classifications are tested first.
TYPE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("earnings_guidance", re.compile(
        r"\b(earnings|guidance|q[1-4]\s|quarterly result|beats|misses|"
        r"raises outlook|cuts outlook|profit warning|preliminary result)\b",
        re.I)),
    ("m_and_a", re.compile(
        r"\b(acquire[sd]?|acquisition|merger|merges|takeover|buyout|"
        r"to be acquired|stake in|divest)\b", re.I)),
    ("major_contract", re.compile(
        r"\b(contract|awarded|wins? (a |the )?deal|partnership|agreement "
        r"with|selects?|order worth)\b", re.I)),
    ("regulatory", re.compile(
        r"\b(fda|approval|approved|clearance|investigation|probe|antitrust|"
        r"sanction|recall|licen[cs]e|patent)\b", re.I)),
    ("litigation", re.compile(
        r"\b(lawsuit|sues?|settlement|court|verdict|class action|"
        r"legal action)\b", re.I)),
    ("capital_action", re.compile(
        r"\b(dividend|buyback|repurchase|offering|split|spin-?off|"
        r"debt|refinanc|convertible)\b", re.I)),
    ("executive_change", re.compile(
        r"\b(ceo|cfo|chief executive|resign|appoint|steps? down|"
        r"names? new|succeeds?)\b", re.I)),
    ("product_launch", re.compile(
        r"\b(launch|unveil|introduc|announces? new|debut|releases?)\b", re.I)),
    ("analyst_commentary", re.compile(
        r"\b(upgrade[sd]?|downgrade[sd]?|price target|initiat\w+ coverage|"
        r"rating|analyst|reiterate)\b", re.I)),
]

BULLISH_TERMS = re.compile(
    r"\b(beats|tops|raises|upgrade[sd]?|surge[sd]?|soar|jump|rally|record|"
    r"strong|wins?|approval|approved|expands?|boost|outperform|buy rating|"
    r"higher|profit|growth|awarded)\b", re.I)
BEARISH_TERMS = re.compile(
    r"\b(misses|cuts?|downgrade[sd]?|plunge|falls?|drop|slump|weak|loss|"
    r"lawsuit|probe|investigation|recall|warns?|delay|halt|sell rating|"
    r"lower|decline|resign)\b", re.I)


@dataclass(slots=True)
class NewsStats:
    fetched: int = 0
    normalized: int = 0
    duplicates: int = 0
    clusters: int = 0
    events: int = 0
    symbols_with_events: int = 0
    by_type: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched, "normalized": self.normalized,
            "duplicates": self.duplicates, "clusters": self.clusters,
            "events": self.events,
            "symbols_with_events": self.symbols_with_events,
            "by_type": dict(sorted(self.by_type.items(),
                                   key=lambda kv: -kv[1])),
        }


# ======================================================================
# classification
# ======================================================================

def publisher_tier(publisher: str) -> SourceTier:
    name = (publisher or "").lower()
    if any(p in name for p in TIER_1_PUBLISHERS):
        return SourceTier.TIER_1_PRIMARY
    if any(p in name for p in TIER_2_PUBLISHERS):
        return SourceTier.TIER_2_MAJOR_NEWS
    return SourceTier.TIER_3_SPECIALIST


def classify_event_type(headline: str, summary: str = "") -> str:
    """Most material classification wins. Unmatched text is a routine filing."""
    text = f"{headline} {summary}"
    for event_type, pattern in TYPE_PATTERNS:
        if pattern.search(text):
            return event_type
    return "routine_filing"


def headline_direction(headline: str, summary: str = "") -> tuple[Direction, float]:
    """Direction implied by wording alone, with a confidence in [0, 1].

    Deliberately crude. It is one input among several and is explicitly
    outranked by the market's actual response.
    """
    text = f"{headline} {summary}"
    bull = len(BULLISH_TERMS.findall(text))
    bear = len(BEARISH_TERMS.findall(text))
    if bull == bear:
        return Direction.NEUTRAL, 0.0
    total = bull + bear
    confidence = clip(abs(bull - bear) / total, 0.0, 1.0)
    direction = Direction.BULLISH if bull > bear else Direction.BEARISH
    # Two matching terms is a weak signal; cap it well below certainty.
    return direction, round(min(confidence, 0.75), 2)


def market_confirmation(direction: Direction, price_return: float | None
                        ) -> float:
    """§10.7. Does price agree with the story?

    A positive headline against a negative move is a genuine warning, and
    scoring it low is the point rather than a rounding of enthusiasm.
    """
    if price_return is None or direction is Direction.NEUTRAL:
        return 50.0
    aligned = direction.sign * price_return
    return clip(50.0 + 50.0 * math.tanh(aligned / 0.01))


def resolve_direction(headline_dir: Direction, headline_conf: float,
                      price_return: float | None
                      ) -> tuple[Direction, float, bool]:
    """Combine wording and price response. Returns (direction, confidence,
    disagreement)."""
    if price_return is None or abs(price_return) < 0.001:
        return headline_dir, headline_conf, False

    price_dir = Direction.BULLISH if price_return > 0 else Direction.BEARISH
    if headline_dir is Direction.NEUTRAL:
        # No wording signal: the move is the signal.
        return price_dir, round(clip(abs(price_return) / 0.02, 0, 0.6), 2), False
    if headline_dir is price_dir:
        return headline_dir, round(min(1.0, headline_conf + 0.20), 2), False

    # They disagree. Trust the market, and cut confidence hard: a story the
    # tape is rejecting is weak evidence in either direction.
    return price_dir, round(min(headline_conf, 0.35), 2), True


# ======================================================================
# normalization and clustering
# ======================================================================

def normalize(raw: dict[str, Any], now: datetime | None = None
              ) -> IntelligenceItem | None:
    headline = (raw.get("headline") or "").strip()
    if not headline:
        return None

    published = parse_alpaca_ts(raw.get("created_at") or raw.get("updated_at"))
    summary = (raw.get("summary") or "").strip()
    publisher = (raw.get("source") or raw.get("author") or "").strip()

    return IntelligenceItem(
        item_id=f"news_{new_uuid()[:12]}",
        source_id=SOURCE_ID,
        source_native_id=str(raw.get("id") or "") or None,
        source_tier=publisher_tier(publisher),
        retrieved_at=now or utc_now(),
        published_at=published,
        updated_at=parse_alpaca_ts(raw.get("updated_at")),
        url=raw.get("url"),
        title=headline,
        summary=summary[:1000] or None,
        content_text=(raw.get("content") or "")[:4000] or None,
        content_hash=content_hash(headline, summary[:200]),
        symbols=[s.upper() for s in (raw.get("symbols") or [])],
        raw={"publisher": publisher, "id": raw.get("id")},
    )


def cluster(items: Sequence[IntelligenceItem]) -> dict[str, list[IntelligenceItem]]:
    """Group items reporting the same story.

    Clustering is where corroboration is measured, which is why the
    database constraint is per-source rather than global: both copies must
    survive insertion for the count to mean anything.
    """
    clusters: dict[str, list[IntelligenceItem]] = {}
    for item in sorted(items, key=lambda i: i.effective_timestamp):
        placed = False
        for cluster_id, members in clusters.items():
            if fuzz.token_set_ratio(item.title, members[0].title) >= \
                    TITLE_MATCH_THRESHOLD:
                members.append(item)
                item.duplicate_cluster_id = cluster_id
                placed = True
                break
        if not placed:
            cluster_id = f"clu_{new_uuid()[:10]}"
            item.duplicate_cluster_id = cluster_id
            clusters[cluster_id] = [item]
    return clusters


def corroboration_score(cluster_size: int, has_tier1: bool,
                        novelty: float) -> float:
    """§10.4. A Tier-1 primary source is self-corroborating when novel."""
    if has_tier1 and novelty >= 70:
        return 100.0
    return {1: 45.0, 2: 70.0, 3: 85.0}.get(cluster_size, 95.0)


def novelty_score(published: datetime, cluster: Sequence[IntelligenceItem],
                  now: datetime) -> float:
    """How new is this story, as opposed to how recently was it republished."""
    earliest = min(i.effective_timestamp for i in cluster)
    age_minutes = (now - earliest).total_seconds() / 60
    if age_minutes <= 30:
        return 100.0
    if age_minutes <= 120:
        return 80.0
    if age_minutes <= 360:
        return 55.0
    if age_minutes <= 1440:
        return 30.0
    return 10.0


# ======================================================================
# service
# ======================================================================

class NewsIntelligence:
    """Fetches, scores, and persists news events for the EVENT track."""

    def __init__(self, api: AlpacaRestClient, db: Database,
                 config: dict[str, Any]):
        self.api = api
        self.db = db
        self.config = config
        self.weights = config.get("catalyst_weights", {
            "materiality": 0.30, "freshness": 0.20,
            "source_reliability": 0.20, "market_confirmation": 0.15,
            "surprise": 0.15})
        self.reliability = config.get("source_base_reliability", {})
        self.stats = NewsStats()

    def _reliability(self, tier: SourceTier) -> float:
        return float({
            SourceTier.TIER_1_PRIMARY: self.reliability.get("issuer_ir", 100),
            SourceTier.TIER_2_MAJOR_NEWS: self.reliability.get("major_wire", 85),
            SourceTier.TIER_3_SPECIALIST: self.reliability.get("specialist", 70),
            SourceTier.TIER_4_SOCIAL: self.reliability.get("social", 35),
        }.get(tier, self.reliability.get("unknown", 20)))

    async def collect(self, symbols: Sequence[str], lookback_hours: int = 24,
                      price_returns: dict[str, float] | None = None,
                      now: datetime | None = None
                      ) -> dict[str, list[IntelligenceEvent]]:
        """Fetch, score and persist. Returns events keyed by symbol."""
        now = now or utc_now()
        price_returns = price_returns or {}
        self.stats = NewsStats()

        try:
            raw = await self.api.get_news(
                list(symbols), now - timedelta(hours=lookback_hours), now,
                limit=50)
        except AlpacaError as exc:
            await self.db.log_event("WARN", "news", "NEWS_FETCH_FAILED",
                                    str(exc)[:300])
            return {}

        self.stats.fetched = len(raw)
        items = [i for i in (normalize(r, now) for r in raw) if i]
        self.stats.normalized = len(items)
        if not items:
            return {}

        clusters = cluster(items)
        self.stats.clusters = len(clusters)
        self.stats.duplicates = len(items) - len(clusters)

        await self._persist_items(items)

        events: dict[str, list[IntelligenceEvent]] = {}
        for members in clusters.values():
            # The originating copy carries the story; later copies are
            # corroboration, not separate events.
            primary = min(members, key=lambda i: i.effective_timestamp)
            has_tier1 = any(i.source_tier is SourceTier.TIER_1_PRIMARY
                            for i in members)

            for symbol in primary.symbols:
                if symbol not in symbols:
                    continue
                event = self._score(primary, members, symbol, has_tier1,
                                    price_returns.get(symbol), now)
                events.setdefault(symbol, []).append(event)
                self.stats.events += 1
                self.stats.by_type[event.event_type] = (
                    self.stats.by_type.get(event.event_type, 0) + 1)

        self.stats.symbols_with_events = len(events)
        await self._persist_events(events)
        return events

    def _score(self, primary: IntelligenceItem,
               members: Sequence[IntelligenceItem], symbol: str,
               has_tier1: bool, price_return: float | None,
               now: datetime) -> IntelligenceEvent:
        event_type = classify_event_type(primary.title, primary.summary or "")
        (low, high), half_life = EVENT_TYPES[event_type]

        hl_dir, hl_conf = headline_direction(primary.title,
                                             primary.summary or "")
        direction, confidence, disagreement = resolve_direction(
            hl_dir, hl_conf, price_return)

        age_minutes = (now - primary.effective_timestamp).total_seconds() / 60
        fresh = freshness_score(age_minutes, half_life)
        novelty = novelty_score(primary.effective_timestamp, members, now)
        corroboration = corroboration_score(len(members), has_tier1, novelty)
        reliability = self._reliability(primary.source_tier)
        confirmation = market_confirmation(direction, price_return)

        # Materiality sits at the band midpoint, lifted toward the top when
        # the wording is unambiguous. Never outside the band.
        materiality = low + (high - low) * (0.5 + 0.4 * hl_conf)
        if disagreement:
            # The tape is rejecting the story. It is still material, but
            # less so than the headline implies.
            materiality *= 0.85

        # No consensus data exists on this plan, so surprise is neutral by
        # construction rather than invented.
        surprise = 50.0

        catalyst = clip(
            self.weights["materiality"] * materiality
            + self.weights["freshness"] * fresh
            + self.weights["source_reliability"] * reliability
            + self.weights["market_confirmation"] * confirmation
            + self.weights["surprise"] * surprise)

        facts = [primary.title]
        if disagreement:
            facts.append(
                f"Headline reads {hl_dir} but the tape moved "
                f"{price_return:+.2%}; direction taken from price.")

        return IntelligenceEvent(
            event_id=f"evt_{new_uuid()[:12]}",
            item_id=primary.item_id, symbol=symbol, event_type=event_type,
            direction=direction, direction_confidence=confidence,
            source_reliability_score=reliability,
            freshness_score=fresh, novelty_score=novelty,
            corroboration_score=corroboration,
            materiality_score=clip(materiality),
            surprise_score=surprise,
            market_confirmation_score=confirmation,
            catalyst_score=catalyst,
            provisional=age_minutes * 60 < PROVISIONAL_SECONDS,
            extracted_facts=facts[:4],
            evidence_urls=[i.url for i in members if i.url][:3],
            created_at=now)

    # ---- persistence -------------------------------------------------

    async def _persist_items(self, items: Sequence[IntelligenceItem]) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO source_registry(source_id, name, domain, "
            "source_type, tier, base_reliability, collector, enabled, "
            "config_json, created_at) VALUES(?,?,?,?,?,?,?,1,'{}',?)",
            (SOURCE_ID, "Alpaca News", "alpaca.markets", "news_aggregator",
             "TIER_2_MAJOR_NEWS", 85.0, "alpaca_news", iso_utc()))

        # INSERT OR IGNORE silently drops a row whose (source_id,
        # content_hash) already exists, which would leave an event pointing
        # at an item_id that was never written. Insert one at a time and
        # adopt the stored id on collision, so the foreign key always
        # resolves and re-running the collector is idempotent.
        for item in items:
            # Two unique constraints guard this table: (source_id,
            # source_native_id) and (source_id, content_hash). An edited
            # headline changes the hash but keeps the native id, so both
            # must be checked or a re-run raises on the second one.
            existing = await self.db.fetchone(
                "SELECT item_id FROM intelligence_items "
                "WHERE source_id = ? AND (content_hash = ? "
                "   OR (source_native_id IS NOT NULL "
                "       AND source_native_id = ?)) LIMIT 1",
                (item.source_id, item.content_hash, item.source_native_id))
            if existing:
                item.item_id = existing["item_id"]
                continue
            await self.db.execute(
                "INSERT INTO intelligence_items("
                "item_id, source_id, source_native_id, source_tier, "
                "retrieved_at, published_at, url, title, summary, "
                "content_text, content_hash, duplicate_cluster_id, "
                "ingest_status, raw_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'SCORED','{}')",
                (item.item_id, item.source_id, item.source_native_id,
                 str(item.source_tier), iso_utc(item.retrieved_at),
                 iso_utc(item.published_at) if item.published_at else None,
                 item.url, item.title, item.summary, item.content_text,
                 item.content_hash, item.duplicate_cluster_id))

    async def _persist_events(
            self, events: dict[str, list[IntelligenceEvent]]) -> None:
        rows = []
        for symbol_events in events.values():
            for e in symbol_events:
                rows.append((
                    e.event_id, e.item_id, e.symbol, e.event_type,
                    str(e.direction), e.direction_confidence,
                    e.source_reliability_score, e.freshness_score,
                    e.novelty_score, e.corroboration_score,
                    e.materiality_score, e.surprise_score,
                    e.market_confirmation_score, e.catalyst_score,
                    1 if e.provisional else 0,
                    str(e.extracted_facts).replace("'", '"'),
                    str(e.evidence_urls).replace("'", '"'),
                    iso_utc(e.created_at)))
        if rows:
            await self.db.executemany(
                "INSERT OR IGNORE INTO intelligence_events("
                "event_id, item_id, symbol, event_type, direction, "
                "direction_confidence, source_reliability_score, "
                "freshness_score, novelty_score, corroboration_score, "
                "materiality_score, surprise_score, "
                "market_confirmation_score, catalyst_score, provisional, "
                "extracted_facts_json, evidence_urls_json, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
