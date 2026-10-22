"""
ui/pages/recon.py
=================
AUM Reconciliation UI. Pure engine lives in core.recon.
"""
from __future__ import annotations

# Placeholder: move render_aum_recon_tab, _render_drilldown, _debug_fifo_detail
# from the original aum_recon.py here during Phase 10.
# For now, re-export a thin adapter if the old module is still importable.

def render_aum_recon_tab():
    try:
        import aum_recon
        aum_recon.render_aum_recon_tab()
    except Exception as e:
        import streamlit as st
        st.error(f"Recon UI not yet fully migrated: {e}")
        st.info("core.recon.compute_recon() is available for pure computation.")
