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
from dashboard.tabs import (
    audit,
    command_center,
    council,
    counterfactual,
    discovery,
    execution_quality,
    gate_lab,
    scanner,
)


st.set_page_config(
    page_title="Alpha Council",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .block-container {padding-top: 1.4rem; padding-bottom: 3rem;}
    [data-testid="stMetric"] {border: 1px solid rgba(128,128,128,.22); border-radius: .6rem; padding: .75rem;}
    .ac-kicker {letter-spacing: .12em; text-transform: uppercase; opacity: .65; font-size: .72rem;}
    .ac-effect {border: 1px solid rgba(128,128,128,.22); border-radius: .6rem; padding: .8rem 1rem; min-height: 7rem;}
    .ac-effect .value {font-size: 1.55rem; font-weight: 650; margin: .2rem 0;}
    .ac-muted {opacity: .62;}
    </style>
    """,
    unsafe_allow_html=True,
)


def main() -> None:
    settings = get_settings()
    database_path = Path(settings.database_path)

    with st.sidebar:
        st.markdown("### Alpha Council")
        st.caption("Read-only presentation layer")
        st.code(str(database_path), language=None)
        if st.button("Refresh database", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        st.divider()
        st.caption("SQLite mode=ro · 30-second query cache · no API or LLM calls")

    st.markdown('<div class="ac-kicker">Autonomous options desk</div>', unsafe_allow_html=True)
    st.title("Alpha Council")
    st.caption("Why the system noticed it, why it acted, and whether governance added or destroyed value.")

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
