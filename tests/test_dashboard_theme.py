"""Dashboard theme invariants.

The pure half of dashboard/theme.py - palettes and Plotly templates -
tested without a Streamlit runtime. The rule that matters most is
theme-stability of financial semantics: gains are green and losses are
red in BOTH modes, and the Constitution's brand green never becomes a
P&L color.
"""

from __future__ import annotations

import plotly.io as pio
import pytest

from dashboard.theme import (
    DARK,
    EMBLEM,
    HERO,
    LIGHT,
    PALETTES,
    build_template,
    variant_colors,
)


def test_palettes_share_every_token():
    assert set(PALETTES) == {"dark", "light"}
    dark_fields = {f for f in DARK.__dataclass_fields__}
    light_fields = {f for f in LIGHT.__dataclass_fields__}
    assert dark_fields == light_fields


def test_backgrounds_and_text_differ_per_mode():
    for palette in (DARK, LIGHT):
        assert palette.bg.lower() != palette.text.lower()
    assert DARK.bg != LIGHT.bg
    assert DARK.text != LIGHT.text


def test_financial_semantics_are_theme_stable():
    """Gain/loss keep their meaning in both modes, and the Constitution
    green is a distinct chrome color, never the gain color."""
    for palette in (DARK, LIGHT):
        assert palette.gain != palette.loss
        assert palette.green != palette.gain


def test_variant_identity_is_consistent():
    for palette in (DARK, LIGHT):
        colors = variant_colors(palette)
        assert set(colors) == {"GPT_ORIGINAL", "CLAUDE_MODIFIED", "EXECUTED"}
        assert colors["EXECUTED"] == palette.gold
        assert colors["GPT_ORIGINAL"] == palette.blue
        assert colors["CLAUDE_MODIFIED"] == palette.purple


def test_templates_registered_and_transparent():
    """Importing the module registers both templates with transparent
    canvases (the CSS gradient behind the chart is the background)."""
    for name in ("alpha_dark", "alpha_light"):
        assert name in pio.templates
    template = build_template(DARK)
    assert template.layout.paper_bgcolor == "rgba(0,0,0,0)"
    assert template.layout.plot_bgcolor == "rgba(0,0,0,0)"
    assert template.layout.colorway[0] == DARK.gold


def test_brand_assets_exist():
    assert HERO.exists(), "hero artwork missing from dashboard/assets"
    assert EMBLEM.exists(), "brand emblem missing from dashboard/assets"
