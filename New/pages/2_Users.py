"""Users — client list & client-level view. Stub, to be built next."""
import sys
from pathlib import Path

import streamlit as st

sys.path.append(str(Path(__file__).resolve().parent.parent))
from core.db import init_db

if not st.session_state.get("db_initialized"):
    init_db()
    st.session_state["db_initialized"] = True

st.header("👤 Users")
st.info("Not built yet. Coming next section.")
