"""Minimal tests for core.folios — run without Streamlit."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_streamlit_not_imported():
    import core.folios
    assert "streamlit" not in sys.modules


def test_empty_folio_shape():
    from core.folios import get_folio_transactions, EMPTY_TXN_COLS
    df = get_folio_transactions("__nonexistent_folio__")
    assert list(df.columns) == EMPTY_TXN_COLS
    assert df.empty


if __name__ == "__main__":
    test_streamlit_not_imported()
    print("test_streamlit_not_imported OK")
    try:
        test_empty_folio_shape()
        print("test_empty_folio_shape OK")
    except Exception as e:
        print(f"test_empty_folio_shape skipped/failed (no DB?): {e}")
