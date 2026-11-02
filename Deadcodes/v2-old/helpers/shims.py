"""
Compatibility shims — import this early from app.py to prefer core implementations.

    import shims  # noqa: F401 — rebinds names into this module's namespace

Or selective:
    from core.clients import get_client_folios, search_clients
"""
from core.db import get_conn, bump, current as data_version
from core.clients import (
    search_clients, get_client_identity, get_client_folios, normalize_folio,
)
from core.folios import get_folio_transactions, get_folio_rta, get_folio_units
from core.families import (
    ensure_family_tables, get_family_for_client, get_family_members,
    create_family, add_family_member, remove_family_member, delete_family,
    get_family_folios, get_family_all_folios, get_family_folios_by_rta,
)
from core.holdings import (
    get_cams_invested, get_kfin_invested, get_client_holdings,
)
from core.xirr import calculate_xirr, compute_xirr_for_folio, compute_portfolio_xirr
from core.tax import Lot, Match, match_fifo
from core.brokerage import (
    load_brokerage_report, ensure_sub_broker_tables, list_sub_brokers,
    create_sub_broker, update_sub_broker, delete_sub_broker,
    compute_sub_broker_summary, get_available_brokerage_periods,
)
from core.dashboard import (
    load_dashboard_summary, load_db_stats, load_table_summary,
    get_sip_counts_by_source,
)
from core.valuation import calc_units_before, calc_units_upto, calc_invested_upto

__all__ = [name for name in dir() if not name.startswith("_")]
