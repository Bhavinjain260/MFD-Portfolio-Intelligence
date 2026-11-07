"""
aum_recon.py
============
AUM Reconciliation: compare RTA closing units (CAMS WBR4 / KFin MFSD203)
against units derived from our transaction tables.

  short   : our units <  AUM units  -> missing transactions -> request them
            (auto-clears once the transactions are imported)
  excess  : our units >  AUM units  -> likely duplicate txn  -> mark inactive
  matched : hidden

Inactive transactions live in `txn_overrides` (survives re-imports) and are
excluded everywhere via the views `v_cams_txn_active` / `v_kfin_txn_active`.
Point every calculation query at those views, never at the base tables.

CAMS aggregation
----------------
`our_units` for CAMS is computed the SAME way the Clients tab does it
(`app.get_cams_invested_per_scheme`):

  * SELECT folio_no AS-is and UPPER(TRIM(prodcode)) — no further normalisation
    of the values that get passed to `replay_folio_scheme()`.
  * Stable sort by parsed traddate (COALESCE(txn_date_iso, traddate)).
  * Pass raw `units`, `purprice`, `amount` to `replay_folio_scheme()` —
    no pd.to_numeric() coercion, no NaN fill.
  * Group by RAW folio/product for the replay, then normalise the resulting
    keys to match the AUM side.

KFin units are signed at source and don't need replay.
"""
from __future__ import annotations

import logging
import re

import pandas as pd
import streamlit as st

log = logging.getLogger(__name__)

TOL = 0.001  # unit tolerance

CAMS_VIEW = "v_cams_txn_active"
KFIN_VIEW = "v_kfin_txn_active"


def _conn():
    from init_db import get_conn
    return get_conn()


# ══════════════════════════════════════════════════════════════
# SCHEMA
# ══════════════════════════════════════════════════════════════
def ensure_schema() -> None:
    """Idempotent. Creates the overrides table and the active-only views."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS txn_overrides (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                rta        TEXT NOT NULL,           -- 'CAMS' | 'KFinTech'
                folio      TEXT NOT NULL,
                txn_no     TEXT NOT NULL,
                fund       TEXT NOT NULL DEFAULT '',-- KFin td_fund, '' for CAMS
                status     TEXT NOT NULL DEFAULT 'inactive',
                reason     TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (rta, folio, txn_no, fund)
            )
        """)
        conn.execute(f"""
            CREATE VIEW IF NOT EXISTS {CAMS_VIEW} AS
            SELECT t.* FROM cams_wbr2_transaction t
            WHERE NOT EXISTS (
                SELECT 1 FROM txn_overrides o
                WHERE o.rta = 'CAMS' AND o.status = 'inactive'
                  AND o.folio  = CAST(t.folio_no AS TEXT)
                  AND o.txn_no = CAST(t.trxnno  AS TEXT)
            )
        """)
        conn.execute(f"""
            CREATE VIEW IF NOT EXISTS {KFIN_VIEW} AS
            SELECT t.* FROM kfin_mfsd201_transaction t
            WHERE NOT EXISTS (
                SELECT 1 FROM txn_overrides o
                WHERE o.rta = 'KFinTech' AND o.status = 'inactive'
                  AND o.folio  = CAST(t.td_acno AS TEXT)
                  AND o.txn_no = CAST(t.td_trno AS TEXT)
                  AND o.fund   = COALESCE(CAST(t.td_fund AS TEXT), '')
            )
        """)


# ══════════════════════════════════════════════════════════════
# OVERRIDES
# ══════════════════════════════════════════════════════════════
def set_inactive(rta: str, folio: str, txn_no: str, fund: str = "", reason: str = "") -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO txn_overrides (rta, folio, txn_no, fund, status, reason) "
            "VALUES (?, ?, ?, ?, 'inactive', ?) "
            "ON CONFLICT(rta, folio, txn_no, fund) DO UPDATE SET "
            "status='inactive', reason=excluded.reason, created_at=CURRENT_TIMESTAMP",
            (rta, str(folio), str(txn_no), str(fund or ""), reason),
        )
    _bump()


def restore(override_id: int) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM txn_overrides WHERE id = ?", (int(override_id),))
    _bump()


def list_overrides() -> pd.DataFrame:
    with _conn() as conn:
        return pd.read_sql(
            "SELECT id, rta, folio, txn_no, fund, reason, created_at "
            "FROM txn_overrides WHERE status='inactive' ORDER BY id DESC", conn)


def _bump() -> None:
    try:
        import data_manager as dm
        dm.bump()   # invalidates every @st.cache_data keyed on data_version
    except Exception:
        log.exception("[AUM-RECON] could not bump data_version")


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════
def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(r"[,'\"]", "", regex=True).str.strip(),
                         errors="coerce")


def _norm(s: pd.Series) -> pd.Series:
    return (s.astype(str).str.replace(r"['\"]", "", regex=True).str.strip().str.upper()
            .replace({"NAN": "", "NONE": ""}))


def _to_iso(raw, dayfirst_default: bool) -> str | None:
    """Parse messy RTA date strings to YYYY-MM-DD. Slash dates: unambiguous
    cases are resolved by value, ambiguous ones use the RTA's default."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none", "nat"):
        return None
    s = re.split(r"\s+", s, maxsplit=1)[0]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if a > 12:
            d, mo = a, b
        elif b > 12:
            mo, d = a, b
        else:
            d, mo = (a, b) if dayfirst_default else (b, a)
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        return None
    ts = pd.to_datetime(s, errors="coerce", format="mixed")
    return None if pd.isna(ts) else ts.strftime("%Y-%m-%d")


def _iso_series(s: pd.Series, dayfirst_default: bool) -> pd.Series:
    return s.map(lambda v: _to_iso(v, dayfirst_default))


# ══════════════════════════════════════════════════════════════
# LOADERS
# ══════════════════════════════════════════════════════════════
def _load_cams_aum(conn) -> pd.DataFrame:
    df = pd.read_sql(
        "SELECT FOLIOCHK AS folio, PRODUCT AS product, SCH_NAME AS scheme, "
        "INV_NAME AS investor, AMC_CODE AS amc, REP_DATE AS rdate, CLOS_BAL AS bal "
        "FROM cams_wbr4_aum", conn)
    return _prep_aum(df, "CAMS", dayfirst_default=False)


def _load_kfin_aum(conn) -> pd.DataFrame:
    df = pd.read_sql(
        "SELECT FOLIO_NUMBER AS folio, PRODUCT_CODE AS product, FUND_DESCRIPTION AS scheme, "
        "INVESTOR_NAME AS investor, FUND AS amc, REPORT_DATE AS rdate, BALANCE AS bal "
        "FROM kfin_mfsd203_aum", conn)
    return _prep_aum(df, "KFinTech", dayfirst_default=True)


AUM_COLS = ["rta", "folio", "product", "scheme", "investor", "amc", "aum_date", "aum_units"]


def _prep_aum(df: pd.DataFrame, rta: str, dayfirst_default: bool) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=AUM_COLS)
    df["folio"] = _norm(df["folio"])
    df["product"] = _norm(df["product"])
    df["aum_date"] = _iso_series(df["rdate"], dayfirst_default)
    df["aum_units"] = _num(df["bal"]).fillna(0.0)
    df = df[(df["folio"] != "") & (df["product"] != "")]
    # latest snapshot per folio+scheme
    df = df.sort_values("aum_date", na_position="first").drop_duplicates(
        ["folio", "product"], keep="last")
    df["rta"] = rta
    return df[AUM_COLS]


def _signed_cams(df: pd.DataFrame) -> pd.Series:
    """Same convention as capital_gain.replay_folio_scheme."""
    units = _num(df["units"]).fillna(0.0)
    is_red = (df["trxntype"].astype(str).str.strip().str.upper() == "R1") | \
             df["trxn_nature"].astype(str).str.lower().str.contains("redemption")
    units = units.where(units > 0, 0.0)
    return units.where(~is_red, -units)


def _load_cams_txns(conn, view: str = CAMS_VIEW) -> pd.DataFrame:
    """SELECT columns exactly like app.get_cams_invested_per_scheme does.
    Both a normalised (folio, product) and a raw (folio_raw, product_raw)
    view of the key are carried through — the FIFO replay uses the raw
    values so its output matches the Clients tab byte for byte."""
    df = pd.read_sql(
        f"SELECT folio_no AS folio, UPPER(TRIM(prodcode)) AS product, "
        f"folio_no AS folio_raw, UPPER(TRIM(prodcode)) AS product_raw, "
        f"COALESCE(txn_date_iso, traddate) AS raw_date, "
        f"COALESCE(txn_date_iso, traddate) AS d, trxntype, trxn_nature, units, purprice, amount, "
        f"trxnno AS txn_no, inv_name AS investor, amc_code AS amc, scheme AS scheme "
        f"FROM {view}", conn)
    if df.empty:
        return df.assign(signed=pd.Series(dtype=float), fund=pd.Series(dtype=str))
    df["folio"] = _norm(df["folio"])
    df["product"] = _norm(df["product"])
    df["d"] = _iso_series(df["d"], dayfirst_default=False)
    df["signed"] = _signed_cams(df)
    df["fund"] = ""
    return df


def _load_kfin_txns(conn, view: str = KFIN_VIEW) -> pd.DataFrame:
    df = pd.read_sql(
        f"SELECT td_acno AS folio, UPPER(TRIM(fmcode)) AS product, "
        f"COALESCE(txn_date_iso, td_trdt) AS d, td_purred AS trxntype, "
        f"td_units AS units, td_amt AS amount, td_trno AS txn_no, td_fund AS fund, "
        f"invname AS investor, td_fund AS amc, funddesc AS scheme "
        f"FROM {view}", conn)
    if df.empty:
        return df.assign(signed=pd.Series(dtype=float), trxn_nature=pd.Series(dtype=str))
    df["folio"] = _norm(df["folio"])
    df["product"] = _norm(df["product"])
    df["d"] = _iso_series(df["d"], dayfirst_default=True)
    df["signed"] = _num(df["units"]).fillna(0.0)   # KFin units are signed at source
    df["fund"] = df["fund"].astype(str).replace({"None": "", "nan": ""})
    df["trxn_nature"] = ""
    return df


def _load_cams_folio_master(conn) -> pd.DataFrame:
    """CAMS WBR9 closing balance per folio+scheme (info column only)."""
    try:
        df = pd.read_sql("SELECT FOLIOCHK AS folio, PRODUCT AS product, CLOS_BAL AS fm "
                         "FROM cams_wbr9_folio", conn)
    except Exception:
        return pd.DataFrame(columns=["folio", "product", "folio_master_units"])
    df["folio"] = _norm(df["folio"])
    df["product"] = _norm(df["product"])
    df["folio_master_units"] = _num(df["fm"])
    return df.drop_duplicates(["folio", "product"], keep="last")[["folio", "product", "folio_master_units"]]


# ══════════════════════════════════════════════════════════════
# FIFO AGGREGATION — mirrors app.get_cams_invested_per_scheme
# ══════════════════════════════════════════════════════════════
def _fifo_units_cams(t: pd.DataFrame, keys: list) -> pd.DataFrame:
    """FIFO replay that produces the SAME result the Clients tab shows.

    Grouping is done on the NORMALISED keys (`folio`, `product`) so invisible
    variation in the raw folio_no / prodcode columns (trailing \\r, tabs,
    non-breaking spaces, embedded quotes) cannot split one folio's history
    into multiple FIFO replays. The replay itself still consumes the raw
    column values (raw_date, units, purprice, amount) exactly as
    app.get_cams_invested_per_scheme does.
    """
    import capital_gain as cg_row

    if t.empty:
        return pd.DataFrame(columns=keys + ["our_units"])

    t = t.copy()
    t["_sort_date"] = pd.to_datetime(t["raw_date"], errors="coerce", format="mixed")
    t = t.sort_values(keys + ["_sort_date"], kind="stable").reset_index(drop=True)

    replay_cols = ["raw_date", "trxntype", "trxn_nature", "units", "purprice", "amount"]
    rows = []
    for key_vals, grp in t.groupby(keys):
        replay = grp[replay_cols].rename(columns={"raw_date": "traddate"})
        try:
            lots, _ = cg_row.replay_folio_scheme(replay)
            units = float(sum(l.remaining_units for l in lots))
        except Exception:
            log.exception("[AUM-RECON] FIFO replay failed for %s — net fallback", key_vals)
            units = 0.0
            for _, r in grp.iterrows():
                is_red = str(r.get("trxntype", "")).strip().upper() == "R1"
                u = float(r["units"]) if pd.notna(r["units"]) else 0.0
                units += -u if is_red else u

        key_dict = dict(zip(keys, key_vals if isinstance(key_vals, tuple) else (key_vals,)))
        key_dict["our_units"] = units
        rows.append(key_dict)

    return pd.DataFrame(rows)

# ══════════════════════════════════════════════════════════════
# ENGINE
# ══════════════════════════════════════════════════════════════
def _reconcile(aum: pd.DataFrame, txns: pd.DataFrame, ignore_dates: bool,
               rta: str = "") -> pd.DataFrame:
    keys = ["folio", "product"]
    if txns.empty:
        ours = pd.DataFrame(columns=keys + ["our_units"])
        last_txn = pd.DataFrame(columns=keys + ["last_txn_date"])
    else:
        t = txns.merge(aum[keys + ["aum_date"]], on=keys, how="left")
        if not ignore_dates:
            keep = t["aum_date"].isna() | t["d"].isna() | (t["d"] <= t["aum_date"])
            t = t[keep]
        if rta == "CAMS":
            ours = _fifo_units_cams(t, keys)
        else:
            ours = t.groupby(keys, as_index=False)["signed"].sum().rename(columns={"signed": "our_units"})
        last_txn = t.groupby(keys, as_index=False)["d"].max().rename(columns={"d": "last_txn_date"})

    out = aum.merge(ours, on=keys, how="outer").merge(last_txn, on=keys, how="left")
    if not txns.empty:
        info = (txns.dropna(subset=["folio"]).drop_duplicates(keys)
                [keys + ["investor", "amc", "scheme"]]
                .rename(columns={"investor": "i2", "amc": "a2", "scheme": "s2"}))
        out = out.merge(info, on=keys, how="left")
        for c, c2 in (("investor", "i2"), ("amc", "a2"), ("scheme", "s2")):
            out[c] = out[c].where(out[c].notna() & (out[c].astype(str).str.strip() != ""), out[c2])
        out = out.drop(columns=["i2", "a2", "s2"])

    out["aum_units"] = out["aum_units"].fillna(0.0)
    out["our_units"] = out["our_units"].fillna(0.0)
    out["diff"] = (out["our_units"] - out["aum_units"]).round(4)

    # ── Status assignment ──
    out["status"] = "matched"
    out.loc[out["diff"] > TOL, "status"] = "excess"
    out.loc[out["diff"] < -TOL, "status"] = "short"

    # NEW: AUM file has no units for this folio+scheme — either no row at all,
    # or a row whose CLOS_BAL is 0 (typically an AUM statement dated before
    # the first SIP). This is a data-completeness issue on the AUM side, not
    # a duplicate-transaction signal. Downgrade from "excess" so the user
    # isn't nudged to deactivate valid transactions.
    no_aum_units = (out["aum_units"] <= TOL) & (out["our_units"] > TOL)
    out.loc[no_aum_units, "status"] = "txn_only"
    return out

RECON_COLS = ["rta", "folio", "product", "scheme", "investor", "amc", "aum_date",
              "aum_units", "our_units", "diff", "folio_master_units", "last_txn_date", "status"]


@st.cache_data(show_spinner="Reconciling units…")
def compute_recon(_v: int, ignore_dates: bool = True) -> pd.DataFrame:
    frames = []
    with _conn() as conn:
        for rta, aum_fn, txn_fn in (("CAMS", _load_cams_aum, _load_cams_txns),
                                    ("KFinTech", _load_kfin_aum, _load_kfin_txns)):
            try:
                aum = aum_fn(conn)
                txns = txn_fn(conn)
            except Exception:
                log.exception("[AUM-RECON] %s load failed", rta)
                continue
            if aum.empty:
                continue   # no AUM file loaded for this RTA -> nothing to compare
            r = _reconcile(aum, txns, ignore_dates, rta=rta)
            r["rta"] = rta
            if rta == "CAMS":
                r = r.merge(_load_cams_folio_master(conn), on=["folio", "product"], how="left")
            else:
                r["folio_master_units"] = float("nan")
            frames.append(r)
    if not frames:
        return pd.DataFrame(columns=RECON_COLS)
    out = pd.concat(frames, ignore_index=True)
    for c in ("scheme", "investor", "amc"):
        out[c] = out[c].fillna("").astype(str).str.replace(r"['\"]", "", regex=True).str.strip()
    return out[RECON_COLS]


# ══════════════════════════════════════════════════════════════
# DRILL-DOWN
# ══════════════════════════════════════════════════════════════
def txn_detail(rta: str, folio: str, product: str, as_of: str | None, diff: float) -> pd.DataFrame:
    """All transactions (active + inactive) for one folio+scheme, with flags."""
    with _conn() as conn:
        if rta == "CAMS":
            t = _load_cams_txns(conn, view="cams_wbr2_transaction")
        else:
            t = _load_kfin_txns(conn, view="kfin_mfsd201_transaction")
        ov = pd.read_sql("SELECT folio, txn_no, fund FROM txn_overrides "
                         "WHERE rta=? AND status='inactive'", conn, params=(rta,))
    t = t[(t["folio"] == folio) & (t["product"] == product)].copy()
    if t.empty:
        return t
    t["txn_no"] = t["txn_no"].astype(str)
    inactive = {(str(r.folio).strip().upper(), str(r.txn_no), str(r.fund or ""))
                for r in ov.itertuples()}
    t["active"] = [(f, n, fu) not in inactive
                   for f, n, fu in zip(t["folio"], t["txn_no"], t["fund"])]
    if as_of:
        t = t[t["d"].isna() | (t["d"] <= as_of)]
    amt = _num(t["amount"]).round(2)
    keys = list(zip(t["d"], t["signed"].round(4), amt))
    counts = pd.Series(keys).value_counts()
    t["flag"] = ""
    t.loc[[counts[k] > 1 for k in keys], "flag"] = "duplicate?"
    if diff > TOL:
        match = ((t["signed"] - diff).abs() <= TOL).values
        is_dup = (t["flag"] == "duplicate?").values
        t.loc[match & ~is_dup, "flag"] = "= excess units"
        t.loc[match & is_dup, "flag"] = "duplicate? = excess units"
    return t.sort_values("d", na_position="first").reset_index(drop=True)


# ══════════════════════════════════════════════════════════════
# DEBUG — step-by-step view of how our_units is calculated
# ══════════════════════════════════════════════════════════════
def _debug_fifo_detail(rta: str, folio: str, product: str, as_of: str | None) -> None:
    """Show exactly how 'our_units' is derived, step by step."""
    import capital_gain as cg_row

    if rta != "CAMS":
        st.info("Debug panel is CAMS-only. KFin units are signed at source.")
        return

    # ── Load active rows (same source as the real calculation) ──
    with _conn() as conn:
        t = _load_cams_txns(conn, view=CAMS_VIEW)
    t = t[(t["folio"] == folio) & (t["product"] == product)].copy()
    if as_of:
        t = t[t["d"].isna() | (t["d"] <= as_of)]
    t = t.sort_values("d", kind="stable").reset_index(drop=True)

    st.markdown("#### Step 1 — active rows fed into the calculation")
    st.caption(f"{len(t)} row(s) after folio/scheme/date filter.")
    if t.empty:
        st.caption("(no active rows)")
        return
    st.dataframe(
        t[["txn_no", "raw_date", "trxntype", "trxn_nature", "units", "purprice", "amount"]],
        use_container_width=True, hide_index=True,
    )

    # ── Duplicate check ──
    dupes = t["txn_no"].astype(str).value_counts()
    dupes = dupes[dupes > 1]
    if not dupes.empty:
        st.error(
            f"⚠️ Duplicate txn_no in the source data — this is what inflates "
            f"the raw sum. Duplicated: {dict(dupes)}. The FIFO replay silently "
            f"collapses them; the old raw-sum method did not."
        )

    # ── Build the replay frame the same way _fifo_units_cams does ──
    replay = t[["raw_date", "trxntype", "trxn_nature", "units", "purprice", "amount"]] \
        .rename(columns={"raw_date": "traddate"})
    replay_cols = ["traddate", "trxntype", "trxn_nature", "units", "purprice", "amount"]

    st.markdown("#### Step 2 — input frame passed to `replay_folio_scheme()`")
    st.dataframe(replay[replay_cols], use_container_width=True, hide_index=True)

    # ── Run the same FIFO replay the calculator uses ──
    st.markdown("#### Step 3 — FIFO lots produced")
    try:
        lots, _matches = cg_row.replay_folio_scheme(replay[replay_cols])
    except Exception as e:
        st.error(f"`replay_folio_scheme()` raised: {e!r}")
        return

    if lots:
        lot_rows = []
        for i, l in enumerate(lots):
            lot_rows.append({
                "lot #":            i,
                "buy date":         getattr(l, "date",           getattr(l, "buy_date", "")),
                "buy units":        getattr(l, "units",          None),
                "buy rate (NAV)":   getattr(l, "rate",           None),
                "remaining units":  getattr(l, "remaining_units", None),
                "cost remaining":   (getattr(l, "remaining_units", 0) or 0)
                                    * (getattr(l, "rate", 0) or 0),
            })
        st.dataframe(pd.DataFrame(lot_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("(no open lots — everything redeemed or nothing purchased)")

    fifo_units = float(sum(getattr(l, "remaining_units", 0.0) for l in lots))

    # ── Side-by-side of methods ──
    st.markdown("#### Step 4 — comparison of methods")
    units_num = pd.to_numeric(t["units"], errors="coerce").fillna(0.0)
    is_red = (
        t["trxntype"].astype(str).str.strip().str.upper().eq("R1")
        | t["trxn_nature"].astype(str).str.lower().str.contains("redemption", na=False)
    )
    signed_sum   = float(units_num.where(~is_red, -units_num).sum())
    raw_positive = float(units_num.sum())

    st.dataframe(pd.DataFrame([
        {"method": "FIFO replay  ← what Clients tab & recon now use",
         "units":  round(fifo_units, 4)},
        {"method": "Raw signed sum  ← what recon used BEFORE the fix",
         "units":  round(signed_sum, 4)},
        {"method": "Raw positive-only sum (no R1 sign flip)",
         "units":  round(raw_positive, 4)},
    ]), use_container_width=True, hide_index=True)

    st.caption(
        "If FIFO and raw-signed-sum disagree, look at Step 1: a row is "
        "probably duplicated (same txn_no twice). If both agree but the "
        "recon table still shows a mismatch, the AUM file for this folio is "
        "the stale side."
    )


# ══════════════════════════════════════════════════════════════
# FETCH FROM GMAIL  ->  PARSE INTO DB  ->  RECONCILE
# ══════════════════════════════════════════════════════════════
def fetch_from_gmail() -> dict:
    """Run one mailback sync in-process (download ZIPs, parse into DB).
    Holds the same flock as background_worker.py so the two never overlap."""
    import fcntl
    import os
    import cams_mailback_sync as cms

    if not cms.credentials_configured():
        return {"ok": False, "msg": "Gmail credentials not configured (Admin Panel → Mailback)."}

    lock_path = os.environ.get("WORKER_LOCK", "/tmp/background_worker.lock")
    try:
        fp = open(lock_path, "a")
    except OSError as e:
        return {"ok": False, "msg": f"Cannot open worker lock: {e}"}
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fp.close()
        return {"ok": False, "msg": "Background worker is running right now. Try again in a minute."}

    try:
        res = cms.sync_once()
    except Exception as e:
        log.exception("[AUM-RECON] Gmail fetch failed")
        return {"ok": False, "msg": str(e)}
    finally:
        try:
            fcntl.flock(fp, fcntl.LOCK_UN)
        finally:
            fp.close()

    _bump()   # make sure the recon cache rebuilds even if nothing new was imported
    msg = (f"checked {res['checked']} · downloaded {len(res['downloaded'])} · "
           f"imported {len(res['parsed'])} · failed {len(res['parse_failed'])} · "
           f"no data {len(res['no_data'])} · errors {len(res['errors'])}")
    return {"ok": not res["errors"] and not res["parse_failed"], "msg": msg, "result": res}


# ══════════════════════════════════════════════════════════════
# UI  (single page)
# ══════════════════════════════════════════════════════════════
def render_aum_recon_tab() -> None:
    try:
        import data_manager as dm
        v = dm.current()
    except Exception:
        dm = None
        v = 0
    ensure_schema()

    st.title("🔍 AUM Reconciliation")
    st.caption("RTA AUM units (as on report date) vs units from folio master + transactions. "
               "Only folios with a difference are listed.")

    # ── Action bar ──
    b1, b2, b3 = st.columns([2, 2, 3])
    if b1.button("📥 Fetch from Gmail & Reconcile", type="primary", key="recon_fetch"):
        with st.spinner("Downloading from Gmail, parsing into DB, reconciling…"):
            st.session_state["recon_fetch_result"] = fetch_from_gmail()
        st.rerun()
    ignore_dates = b2.checkbox(
        "Ignore AUM date", value=True, key="recon_ignore",
        help="ON (default): compare against ALL active transactions — same as the "
             "Clients tab does. OFF: only transactions dated up to each AUM report date.",
    )
    try:
        last = dm.get_credential("mailback_last_sync_at") if dm else None
    except Exception:
        last = None
    b3.caption(f"Last Gmail sync: {last or '—'}")

    fr = st.session_state.get("recon_fetch_result")
    if fr:
        (st.success if fr["ok"] else st.warning)(fr["msg"])
        res = fr.get("result") or {}
        if res.get("parse_failed") or res.get("errors"):
            with st.expander("Fetch problems"):
                for e in res.get("errors", []):
                    st.write(f"• {e}")
                for f in res.get("parse_failed", []):
                    st.write(f"• {f.get('rta')} {f.get('report')} {f.get('file')}: {f.get('msg', '')}")

    df = compute_recon(v, ignore_dates)
    if df.empty:
        st.info("No AUM data loaded yet. Click **Fetch from Gmail & Reconcile** after requesting the files.")
        return

    n_ok = int((df["status"] == "matched").sum())
    n_short = int((df["status"] == "short").sum())
    n_excess = int((df["status"] == "excess").sum())
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Compared", f"{len(df):,}")
    m2.metric("✅ Matched", f"{n_ok:,}")
    m3.metric("📉 Short", f"{n_short:,}")
    m4.metric("📈 Excess", f"{n_excess:,}")

    latest_aum = df["aum_date"].dropna().max()
    latest_txn = df["last_txn_date"].dropna().max()
    if pd.notna(latest_aum) and pd.notna(latest_txn) and latest_txn > latest_aum and not ignore_dates:
        st.caption(f"AUM as on {latest_aum}; transactions available up to {latest_txn} "
                   "(later ones are excluded from the comparison).")

    # ── Filters ──
    f1, f2, f3 = st.columns([1, 1, 2])
    rta_f = f1.selectbox("RTA", ["All", "CAMS", "KFinTech"], key="recon_rta")
    st_f = f2.selectbox("Status", ["All", "Short", "Excess"], key="recon_status")
    q = f3.text_input("Search folio / investor / scheme / AMC", key="recon_q").strip().lower()

    view = df[df["status"] != "matched"].copy()
    if rta_f != "All":
        view = view[view["rta"] == rta_f]
    if st_f != "All":
        view = view[view["status"] == st_f.lower()]
    if q:
        blob = (view["folio"] + " " + view["investor"].astype(str) + " " +
                view["scheme"].astype(str) + " " + view["amc"].astype(str)).str.lower()
        view = view[blob.str.contains(re.escape(q))]

    view = view.assign(_abs=view["diff"].abs()).sort_values("_abs", ascending=False).drop(columns="_abs")
    view = view.reset_index(drop=True)
    view.insert(0, "result", view["status"].map({"short": "📉 Short", "excess": "📈 Excess"}))

    if view.empty:
        st.success("Nothing to reconcile — all folios match 🎉" if n_short + n_excess == 0
                   else "No rows for the current filters.")
    else:
        cols = ["result", "rta", "amc", "folio", "product", "scheme", "investor",
                "aum_date", "aum_units", "our_units", "diff", "folio_master_units", "last_txn_date"]
        st.caption(f"{len(view):,} folio+scheme with a difference · click a row to investigate")
        sel_idx = None
        try:
            ev = st.dataframe(view[cols], use_container_width=True, hide_index=True,
                              on_select="rerun", selection_mode="single-row", key="recon_table")
            rows = ev.selection.rows
            sel_idx = rows[0] if rows else None
        except TypeError:   # older Streamlit without row selection
            st.dataframe(view[cols], use_container_width=True, hide_index=True)
            labels = [f"{r.result} | {r.rta} | {r.folio} | {r.product} | {r.diff:+.3f}" for r in view.itertuples()]
            sel_idx = st.selectbox("Investigate", [None] + list(range(len(view))),
                                   format_func=lambda i: "—" if i is None else labels[i], key="recon_pick")

        st.download_button("⬇️ Download list (CSV)", view[cols].to_csv(index=False).encode("utf-8"),
                           file_name="aum_recon_differences.csv", mime="text/csv")

        # ── Drill-down (same page) ──
        if sel_idx is not None and sel_idx < len(view):
            row = view.iloc[sel_idx]
            _render_drilldown(row, None if ignore_dates else row["aum_date"])

    # ── Inactive transactions (same page) ──
    ov = list_overrides()
    with st.expander(f"🚫 Inactive transactions ({len(ov)})"):
        if ov.empty:
            st.caption("None. Marked transactions are excluded from every calculation in the app.")
        for r in ov.itertuples():
            a, b = st.columns([6, 1])
            a.write(f"**{r.rta}** · folio `{r.folio}` · txn `{r.txn_no}`"
                    f"{' · fund ' + r.fund if r.fund else ''} · {r.reason or ''} · {r.created_at}")
            if b.button("Restore", key=f"recon_restore_{r.id}"):
                restore(r.id)
                st.rerun()


def _render_drilldown(row: pd.Series, as_of: str | None) -> None:
    st.divider()
    is_excess = row["status"] == "excess"
    st.subheader(f"{'📈 Excess' if is_excess else '📉 Short'} · {row['rta']} · folio {row['folio']} · {row['product']}")
    st.caption(f"{row['scheme']} · {row['investor']}")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("AUM units", f"{row['aum_units']:,.3f}")
    k2.metric("Our units", f"{row['our_units']:,.3f}")
    k3.metric("Difference", f"{row['diff']:+,.3f}")
    fm = row.get("folio_master_units")
    k4.metric("Folio master units", "—" if pd.isna(fm) else f"{fm:,.3f}")

    det = txn_detail(row["rta"], row["folio"], row["product"], as_of, float(row["diff"]))
    if not is_excess:
        st.info(f"Short by {abs(row['diff']):,.3f} units — transactions are missing for this folio. "
                "Request them from the RTA; this row disappears once the units match.")
    if det.empty:
        st.caption("No transactions on file for this folio+scheme.")
        return

    show = det[["txn_no", "fund", "d", "trxntype", "units", "signed", "amount", "active", "flag"]] \
        .rename(columns={"d": "date", "signed": "signed_units"})
    st.dataframe(show, use_container_width=True, hide_index=True)

    # ── Debug panel: how our_units is calculated ──
    with st.expander("🐞 Debug: how our_units is calculated", expanded=False):
        _debug_fifo_detail(row["rta"], row["folio"], row["product"],
                           None if as_of is None else as_of)

    if is_excess:
        act = det[det["active"]].reset_index(drop=True)
        if act.empty:
            return
        opts = [f"{r.txn_no} | {r.d} | {r.trxntype} | {r.signed:+.3f} u"
                f"{'  ⚠ ' + r.flag if r.flag else ''}" for r in act.itertuples()]
        default = next((i for i, r in enumerate(act.itertuples()) if r.flag), 0)
        c1, c2, c3 = st.columns([3, 2, 1])
        sel = c1.selectbox("Transaction to mark inactive", range(len(act)), index=default,
                           format_func=lambda i: opts[i], key=f"recon_txn_{row['rta']}_{row['folio']}_{row['product']}")
        reason = c2.text_input("Reason", value="Duplicate transaction", key="recon_reason")
        c3.write("")
        if c3.button("🚫 Mark inactive", type="primary", key="recon_mark"):
            r = act.iloc[sel]
            set_inactive(row["rta"], row["folio"], r["txn_no"], r["fund"], reason)
            st.rerun()