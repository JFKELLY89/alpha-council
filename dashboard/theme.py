"""Alpha Council dashboard theme - the court in dark and daylight.

One module owns every color, font, and chart template so the nine tabs
cannot drift apart. Two palettes derived from the brand art:

  DARK  - the council chamber: near-black starfield navy, warm ivory
          text, gold chrome, with the three pillar colors from the art
          (council blue, Evolution purple, Constitution green).
  LIGHT - the daylight court: parchment ivory, deep-navy text, the same
          gold and pillar accents recalibrated for contrast.

Three integration layers, in order of authority:
  1. Streamlit theme options (set per rerun via streamlit config) so
     NATIVE widgets - dataframes render on canvas and are unreachable
     from CSS - follow the palette.
  2. Injected CSS for the brand chrome: Cinzel display type, metric
     cards, tab accents, the hero frame, gold dividers.
  3. Registered Plotly templates + the plot() helper (theme=None) so
     charts use the brand colorway instead of Streamlit's default.

Financial semantics are deliberately conventional and theme-stable:
gains are green, losses are red, in both modes. The Constitution's
brand green is chrome only and never colors a P&L number.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import plotly.graph_objects as go
import plotly.io as pio

ASSETS = Path(__file__).resolve().parent / "assets"
HERO = ASSETS / "alpha-council-dashboard-hero-v3.png"
EMBLEM = ASSETS / "alpha-council-brand-mark-navy-v2.png"

FONT_IMPORT = (
    "@import url('https://fonts.googleapis.com/css2"
    "?family=Cinzel:wght@500;600;700&display=swap');"
)
DISPLAY_FONT = "'Cinzel', 'Trajan Pro', Georgia, serif"
BODY_FONT = "'Source Sans Pro', 'Source Sans 3', sans-serif"


@dataclass(frozen=True)
class Palette:
    name: str
    base: str                 # streamlit theme.base
    bg: str
    bg2: str                  # sidebar / secondary surfaces
    card: str
    text: str
    muted: str
    gold: str
    gold_bright: str
    blue: str                 # council
    purple: str               # Evolution
    green: str                # Constitution (chrome only, never P&L)
    gain: str
    loss: str
    border: str
    grid: str                 # chart gridlines
    glow: str                 # radial backdrop tint


DARK = Palette(
    name="dark", base="dark",
    bg="#070B15", bg2="#0C1424", card="rgba(16,27,48,0.62)",
    text="#EDE7D8", muted="#98A2B8",
    gold="#D4AF37", gold_bright="#EFD68A",
    blue="#5AA7FF", purple="#A873FF", green="#58B87C",
    gain="#34C97C", loss="#E5545C",
    border="rgba(212,175,55,0.28)", grid="rgba(212,175,55,0.13)",
    glow="rgba(212,175,55,0.07)",
)

LIGHT = Palette(
    name="light", base="light",
    bg="#F6F1E4", bg2="#EDE5D0", card="#FFFDF6",
    text="#16203B", muted="#5C6579",
    gold="#9C7A1F", gold_bright="#7E621A",
    blue="#1E5FB8", purple="#6C3FB0", green="#2F7D51",
    gain="#178A50", loss="#C03540",
    border="rgba(156,122,31,0.38)", grid="rgba(22,32,59,0.12)",
    glow="rgba(156,122,31,0.06)",
)

PALETTES = {"dark": DARK, "light": LIGHT}

# Counterfactual variants keep one identity across every chart and mode:
# the PM's original is council blue, the Red Team's is Evolution purple,
# and what actually executed wears the gold.
VARIANT_KEYS = ("GPT_ORIGINAL", "CLAUDE_MODIFIED", "EXECUTED")


def variant_colors(palette: Palette) -> dict[str, str]:
    return {
        "GPT_ORIGINAL": palette.blue,
        "CLAUDE_MODIFIED": palette.purple,
        "EXECUTED": palette.gold,
    }


# ----------------------------------------------------------------------
# plotly templates
# ----------------------------------------------------------------------

def build_template(palette: Palette) -> go.layout.Template:
    return go.layout.Template(
        layout=go.Layout(
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=palette.text, size=13),
            title_font=dict(color=palette.gold_bright, size=16),
            colorway=[palette.gold, palette.blue, palette.purple,
                      palette.green, palette.muted, palette.gold_bright,
                      palette.loss],
            xaxis=dict(gridcolor=palette.grid, zerolinecolor=palette.border,
                       linecolor=palette.border),
            yaxis=dict(gridcolor=palette.grid, zerolinecolor=palette.border,
                       linecolor=palette.border),
            legend=dict(bgcolor="rgba(0,0,0,0)",
                        font=dict(color=palette.text)),
            hoverlabel=dict(bgcolor=palette.bg2,
                            font=dict(color=palette.text),
                            bordercolor=palette.gold),
            margin=dict(l=10, r=10, t=30, b=10),
        )
    )


def register_templates() -> None:
    pio.templates["alpha_dark"] = build_template(DARK)
    pio.templates["alpha_light"] = build_template(LIGHT)


register_templates()


# ----------------------------------------------------------------------
# runtime state (imports streamlit lazily so pure helpers stay testable)
# ----------------------------------------------------------------------

def active_palette() -> Palette:
    import streamlit as st

    return PALETTES.get(st.session_state.get("ui_theme", "dark"), DARK)


def theme_toggle() -> Palette:
    """Render the sidebar mode toggle and return the active palette.

    Defaults to dark - the council chamber is the brand. Streamlit sends
    the theme to the browser at the START of a script run, so a change
    detected here re-points the theme options and reruns immediately;
    without that, canvas-drawn widgets (dataframes) would lag the CSS by
    one interaction.
    """
    import streamlit as st

    previous = st.session_state.get("ui_theme", "dark")
    # An explicit key makes the widget's identity stable across reruns;
    # a keyless toggle whose value= param moves gets silently reset.
    st.session_state.setdefault("ui_theme_dark", previous == "dark")
    dark = st.toggle(
        "Dark council",
        key="ui_theme_dark",
        help="Switch between the council chamber and the daylight court.",
    )
    chosen = "dark" if dark else "light"
    st.session_state["ui_theme"] = chosen
    if chosen != previous:
        apply_streamlit_theme(PALETTES[chosen])
        st.rerun()
    return PALETTES[chosen]


def apply_streamlit_theme(palette: Palette) -> None:
    """Point Streamlit's own theme at the palette.

    Uses the internal config API because theme options are not settable
    through the public one; each key is fenced so a renamed option in a
    future Streamlit costs that key, not the page. The injected CSS
    carries the look regardless - this layer exists for the canvas-drawn
    widgets (dataframes) that CSS cannot reach.
    """
    import streamlit as st

    options = {
        "theme.base": palette.base,
        "theme.primaryColor": palette.gold,
        "theme.backgroundColor": palette.bg,
        "theme.secondaryBackgroundColor": palette.bg2,
        "theme.textColor": palette.text,
        "theme.linkColor": palette.blue,
        "theme.borderColor": palette.border,
        "theme.chartCategoricalColors": [
            palette.gold, palette.blue, palette.purple, palette.green,
            palette.muted, palette.gold_bright, palette.loss],
    }
    for key, value in options.items():
        try:
            st._config.set_option(key, value)
        except Exception:  # noqa: BLE001 - a missing option is cosmetic
            continue

    # Plotly Express BAKES trace colors from the default template at
    # figure CREATION time; applying a template afterwards restyles the
    # canvas but not the traces. The default must therefore point at the
    # brand template before any tab builds a figure.
    pio.templates.default = f"alpha_{palette.name}"


def plot(figure: go.Figure, height: int | None = None) -> None:
    """Render a Plotly figure in the brand template for the active mode.

    theme=None is the point: Streamlit's own plotly theme would override
    the template's colorway and fonts. Margins are floored because the
    tabs' tight l=10 layouts clipped axis titles once Streamlit's
    auto-margin theme stopped applying.
    """
    import streamlit as st

    palette = active_palette()
    figure.update_layout(template=f"alpha_{palette.name}")
    if height is not None:
        figure.update_layout(height=height)

    # Funnel stage names live entirely in the left margin and need more
    # room than an axis title does.
    has_funnel = any(getattr(trace, "type", "") == "funnel"
                     for trace in figure.data)
    left_floor = 86 if has_funnel else 48
    margin = figure.layout.margin
    figure.update_layout(margin=dict(
        l=max(margin.l or 0, left_floor),
        r=max(margin.r or 0, 16),
        t=max(margin.t or 0, 30),
        b=max(margin.b or 0, 40),
    ))
    st.plotly_chart(figure, width="stretch", theme=None)


# ----------------------------------------------------------------------
# chrome
# ----------------------------------------------------------------------

def inject_css(palette: Palette) -> None:
    import streamlit as st

    p = palette
    st.markdown(
        f"""
        <style>
        {FONT_IMPORT}

        /* ---- canvas ------------------------------------------------ */
        .stApp {{
            background:
                radial-gradient(1100px 420px at 50% -60px, {p.glow}, transparent 70%),
                {p.bg};
            color: {p.text};
        }}
        header[data-testid="stHeader"] {{
            background: transparent;
        }}
        section[data-testid="stSidebar"] {{
            background: {p.bg2};
            border-right: 1px solid {p.border};
        }}
        .block-container {{ padding-top: 1.1rem; padding-bottom: 3rem; }}

        /* ---- type -------------------------------------------------- */
        h1, [data-testid="stHeading"] h1 {{
            font-family: {DISPLAY_FONT} !important;
            font-weight: 700;
            letter-spacing: .14em;
            text-transform: uppercase;
            background: linear-gradient(180deg, {p.gold_bright}, {p.gold});
            -webkit-background-clip: text;
            background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0;
        }}
        /* st.subheader renders inside stHeading with its own font rule,
           which outranks a bare element selector. */
        h3, [data-testid="stHeading"] h3 {{
            font-family: {DISPLAY_FONT} !important;
            font-weight: 600;
            letter-spacing: .08em;
            color: {p.gold if p.name == 'light' else p.gold_bright} !important;
        }}
        h4 {{
            letter-spacing: .10em;
            text-transform: uppercase;
            font-size: .82rem;
            color: {p.muted};
            border-bottom: 1px solid {p.border};
            padding-bottom: .3rem;
        }}
        .ac-kicker {{
            font-family: {DISPLAY_FONT};
            letter-spacing: .16em;
            text-transform: uppercase;
            color: {p.gold};
            font-size: .72rem;
        }}
        .ac-muted {{ color: {p.muted}; }}

        /* ---- metric + effect cards --------------------------------- */
        [data-testid="stMetric"], .ac-effect {{
            background: {p.card};
            border: 1px solid {p.border};
            border-radius: .65rem;
            padding: .78rem .95rem;
            box-shadow: 0 1px 10px rgba(0,0,0,{'0.35' if p.name == 'dark' else '0.06'});
        }}
        [data-testid="stMetric"] label {{
            color: {p.muted} !important;
            letter-spacing: .08em;
            text-transform: uppercase;
            font-size: .70rem;
        }}
        [data-testid="stMetricValue"] {{ color: {p.text}; }}
        .ac-effect {{ min-height: 7rem; }}
        .ac-effect .value {{
            font-size: 1.5rem; font-weight: 650; margin: .2rem 0;
            color: {p.text};
        }}

        /* ---- tabs: distinct chips, pillar accents ------------------
           Streamlit 1.62 renders tabs as bare role="tab" divs with no
           data-baseweb hooks; the ARIA roles are the stable surface.
           Chips instead of a run-on text row: nine adjacent labels with
           .25rem gaps read as one sentence, not as navigation. */
        .stTabs [role="tablist"] {{
            gap: .5rem;
            border-bottom: 1px solid {p.border};
            padding-bottom: .55rem;
        }}
        .stTabs [role="tab"] {{
            background: {p.card};
            border: 1px solid {p.grid};
            border-radius: .55rem;
            padding: .34rem .9rem;
            color: {p.muted};
            letter-spacing: .03em;
            transition: color .15s, border-color .15s;
        }}
        .stTabs [role="tab"]:hover {{
            color: {p.text};
            border-color: {p.border};
        }}
        .stTabs [role="tab"][aria-selected="true"] {{
            color: {p.gold} !important;
            border-color: {p.gold};
            background: {'rgba(212,175,55,0.10)' if p.name == 'dark'
                         else 'rgba(156,122,31,0.10)'};
            font-weight: 600;
            box-shadow: 0 0 10px {'rgba(212,175,55,0.22)' if p.name == 'dark'
                                  else 'rgba(156,122,31,0.15)'};
        }}
        /* the sliding underline is redundant under chips */
        .stTabs [data-baseweb="tab-highlight"],
        .stTabs [data-baseweb="tab-border"] {{ display: none; }}
        /* pillar tints: Council Decision blue, Gate Lab green,
           Alpha Evolution purple (fixed tab order) */
        .stTabs [role="tab"]:nth-child(4)[aria-selected="true"] {{
            color: {p.blue} !important; border-color: {p.blue};
        }}
        .stTabs [role="tab"]:nth-child(6)[aria-selected="true"] {{
            color: {p.green} !important; border-color: {p.green};
        }}
        .stTabs [role="tab"]:nth-child(8)[aria-selected="true"] {{
            color: {p.purple} !important; border-color: {p.purple};
        }}

        /* ---- surfaces ---------------------------------------------- */
        [data-testid="stExpander"] {{
            background: {p.card};
            border: 1px solid {p.border};
            border-radius: .6rem;
        }}
        hr {{
            border: none; height: 1px;
            background: linear-gradient(90deg, transparent, {p.gold}, transparent);
            opacity: .55;
        }}
        [data-testid="stImage"] img {{
            border-radius: .8rem;
            border: 1px solid {p.border};
        }}
        .stButton > button {{
            border: 1px solid {p.gold};
            color: {p.gold};
            background: transparent;
            border-radius: .5rem;
        }}
        .stButton > button:hover {{
            background: {p.gold};
            color: {p.bg};
        }}

        /* gate-lab raw table */
        table {{ border-collapse: collapse; }}
        table th {{
            color: {p.gold};
            border-bottom: 1px solid {p.border};
            text-align: left; padding: .3rem .5rem;
            letter-spacing: .05em; text-transform: uppercase;
            font-size: .74rem;
        }}
        table td {{
            border-bottom: 1px solid {p.grid};
            padding: .3rem .5rem;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def brand_header() -> None:
    """The slim court header shown above the tabs on every page."""
    import streamlit as st

    left, right = st.columns([0.09, 0.91])
    with left:
        if EMBLEM.exists():
            st.image(str(EMBLEM), width=76)
    with right:
        st.markdown('<div class="ac-kicker">Autonomous options desk</div>',
                    unsafe_allow_html=True)
        st.title("Alpha Council")
        st.caption(
            "Why the system noticed it, why it acted, and whether "
            "governance added or destroyed value."
        )


def hero() -> None:
    """The full council-chamber artwork. Command Center only, by design."""
    import streamlit as st

    if HERO.exists():
        st.image(str(HERO), width="stretch")
