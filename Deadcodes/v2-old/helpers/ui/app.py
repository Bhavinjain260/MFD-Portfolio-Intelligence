"""
ui/app.py
=========
Thin Streamlit router. Page bodies move here in Phase 10.
Until then, the monolith app.py remains the entrypoint; this file is the
target shape.
"""
from __future__ import annotations

import streamlit as st

from core.db import ensure_schema, current as data_version


def main():
    st.set_page_config(page_title="MFD Portfolio Intelligence", layout="wide")
    ensure_schema()

    mode = st.sidebar.radio(
        "Mode",
        [
            "📊 Dashboard",
            "👤 Clients",
            "📋 Transactions",
            "💰 Brokerage",
            "📄 Reports",
            "📈 Capital Gains",
            "🔍 AUM Reconciliation",
            "⚙️ Admin Panel",
        ],
    )

    # Phase 10: import page modules
    # if mode == "🔍 AUM Reconciliation":
    #     from ui.pages.recon import render_aum_recon_tab
    #     render_aum_recon_tab()
    # ...

    st.info(
        f"Router scaffold active. data_version={data_version()}. "
        "Point streamlit run at the monolith until Phase 10 page split is complete."
    )


if __name__ == "__main__":
    main()
