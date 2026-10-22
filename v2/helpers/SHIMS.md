# Compatibility shims for gradual cutover

Until the monolith `app.py` is fully split, keep thin re-exports so existing
imports keep working:

```python
# init_db.py (keep schema string; re-export connection)
from core.db import get_conn, DB_PATH, set_db_path, ensure_schema
# def init_db(): ... existing CREATE TABLE script ...

# data_manager.py — after parsers move to ingest/
from core.db import get_conn, bump, current, set_db_path

# nav_service.py
from core.nav import *  # noqa

# aum_recon.py
from core.recon import *  # noqa
# render_aum_recon_tab stays or moves to ui/pages/recon.py

# capital_gain.py
from core.tax import *  # noqa

# xirr.py
from core.xirr import *  # noqa

# sync_failure_log.py
from core.failure_log import *  # noqa
```

Point `PYTHONPATH` at the project root containing `core/`.


# brokerage (Phase 9)
# In app.py you can gradually replace local defs with:
# from core.brokerage import (
#     load_brokerage_report, ensure_sub_broker_tables,
#     list_sub_brokers, create_sub_broker, update_sub_broker, delete_sub_broker,
#     assign_clients_to_sub_broker, unassign_client_from_sub_broker,
#     get_clients_for_sub_broker, get_client_to_sub_broker_map,
#     get_folio_to_client_code_map, list_payouts, record_payout, delete_payout,
#     compute_sub_broker_summary, get_available_brokerage_periods,
# )
# Keep render_sub_brokers_ui, render_sub_broker_sharing_report,
# generate_brokerage_sharing_pdf/excel/email in ui/.
