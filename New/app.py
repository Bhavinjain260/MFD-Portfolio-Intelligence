"""
MFD Portfolio Intelligence — Entry point.
Mirrors legacy Admin Panel nav: Main(Users) / Users-Transactions-SIPs /
Reports / MIS / Upload Utilities / Products.

Run with: streamlit run app.py
Pages live in /pages — Streamlit auto-builds the sidebar nav from filenames.
"""
import streamlit as st

from core.db import init_db

st.set_page_config(page_title="MFD Portfolio Intelligence", layout="wide", page_icon="📊")

if not st.session_state.get("db_initialized"):
    init_db()
    st.session_state["db_initialized"] = True

st.title("📊 MFD Portfolio Intelligence")
st.caption("Use the sidebar to navigate: Upload Utilities, Users, Transactions/SIPs, MIS.")

st.markdown("""
### Quick Start
- **Upload Utilities** — import BSE Client Master, BSE SIP Report, CAMS (WBR2/4/9/49), KFinTech AUM & Brokerage
- **Users** — client list & client-level portfolio view
- **Transactions / SIPs** — all SIPs across clients, status tracking
- **MIS** — AUM by AMC, AUM by client, brokerage reconciliation

This app is being rebuilt section by section. Currently live: **Upload Utilities**.
""")
