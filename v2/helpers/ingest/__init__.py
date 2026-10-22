"""ingest package — Phase 8 parsers (copied from data_manager for migration).

Until data_manager is slimmed, the live app still calls data_manager.parse_*.
New code should import from ingest.parsers_*.
"""
from ingest.parsers_bse import parse_bse_client_master, parse_bse_sip, parse_bse_scheme_master
from ingest.parsers_cams import (
    parse_cams_wbr4_aum, parse_cams_wbr9_folio, parse_cams_wbr2_transaction,
    parse_cams_wbr49_sip, parse_cams_wbr77_brokerage,
)
from ingest.parsers_kfin import (
    parse_kfin_mfsd203_aum, parse_kfin_mfsd211_folio, parse_kfin_mfsd201_transaction,
    parse_kfin_mfsd243_sip, parse_kfin_mfsd205_brokerage,
)
from ingest.parsers_nav import parse_nav_file, parse_nav_filepath
