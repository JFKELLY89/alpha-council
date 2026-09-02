"""Alpha Council read-only Streamlit dashboard."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import streamlit as st

# Streamlit puts the script directory on sys.path. Add its parent explicitly so
# both alpha_council and dashboard package imports resolve from any launch cwd.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from alpha_council.settings import get_settings
from dashboard import theme
from dashboard.tabs import (
    audit,
    command_center,
    council,
    counterfactual,
    discovery,
    evolution,
    execution_quality,
    gate_lab,
    scanner,
)


st.set_page_config(
    page_title="Alpha Council",
    page_icon=str(theme.EMBLEM) if theme.EMBLEM.exists() else "⚖️",
    layout="wide",
    initial_sidebar_state="expanded",
)


def main() -> None:
    settings = get_settings()
    database_path = Path(settings.database_path)

    with st.sidebar:
        if theme.EMBLEM.exists():
            st.image(str(theme.EMBLEM), width=170)
        st.markdown("### Alpha Council")
        st.caption("Read-only presentation layer")

        # The toggle is the first stateful thing on the page: everything
        # downstream reads the palette it selects. Dark is the brand.
        palette = theme.theme_toggle()

        st.code(str(database_path), language=None)
        if st.button("Refresh database", width="stretch"):
            st.cache_data.clear()
            st.rerun()
        st.divider()
        st.caption("SQLite mode=ro · 30-second query cache · no API or LLM calls")

    theme.apply_streamlit_theme(palette)
    theme.inject_css(palette)
    theme.brand_header()

    if not database_path.exists():
        st.error(f"Database not found: {database_path}")
        st.stop()

    labels = [
        "Command Center",
        "Discovery Funnel",
        "Scanner",
        "Council Decision",
        "Counterfactual Lab",
        "Gate Lab",
        "Execution Quality",
        "Alpha Evolution",
        "Audit",
    ]
    renderers = [
        command_center.render,
        discovery.render,
        scanner.render,
        council.render,
        counterfactual.render,
        gate_lab.render,
        execution_quality.render,
        evolution.render,
        audit.render,
    ]

    try:
        tabs = st.tabs(labels)
        for container, renderer in zip(tabs, renderers, strict=True):
            with container:
                renderer(database_path)
    except sqlite3.OperationalError as exc:
        st.error(f"The dashboard could not read SQLite: {exc}")
        st.caption("The database is opened read-only; verify that the configured file exists and is readable.")


if __name__ == "__main__":
    main()
