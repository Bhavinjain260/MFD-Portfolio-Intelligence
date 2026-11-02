"""
recon.py
========
AUM reconciliation: RTA closing units (CAMS WBR4 / KFin MFSD203) against
units derived from our transaction tables.

  short   : our units <  AUM units  -> missing transactions -> request them
            (auto-clears once the transactions are imported)
  excess  : our units >  AUM units  -> likely duplicate txn -> mark inactive
  matched : hidden

Inactive transactions live in `txn_overrides` (survives re-imports) and are
excluded everywhere via the views `v_cams_txn_active` / `v_kfin_txn_active`.
Point every calculation query at those views, never at the base tables.

CAMS aggregation
----------------
`our_units` for CAMS is computed the SAME way the Clients tab does it:

  * SELECT folio_no AS-is and UPPER(TRIM(prodcode)) — no further normalisation.
  * Stable sort by parsed traddate (COALESCE(txn_date_iso, traddate)).
  * Pass raw `units`, `purprice`, `amount` to `replay_folio_scheme()`.
  * Group by RAW folio/product for the replay, then normalise keys to match
    the AUM side.

KFin units are signed at source and don't need replay.
"""
from __future__ import annotations

import logging
import re

import pandas as pd

from app.core.compat import get_conn
from app.services.cache import bump

log = logging.getLogger(__name__)

TOL = 0.001

CAMS_VIEW = "v_cams_txn_active"
KFIN_VIEW = "v_kfin_txn_active"


def _conn():
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
                rta        TEXT NOT NULL,
                folio      TEXT NOT NULL,
                txn_no     TEXT NOT NULL,
                fund       TEXT NOT NULL DEFAULT '',
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
        bump()
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
    df["signed"] = _num(df["units"]).fillna(0.0)
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
    from app.services import capital_gain as cg_row

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

    out["status"] = "matched"
    out.loc[out["diff"] > TOL, "status"] = "excess"
    out.loc[out["diff"] < -TOL, "status"] = "short"

    # AUM file has no units for this folio+scheme — data-completeness issue
    # on the AUM side, not a duplicate-transaction signal.
    no_aum_units = (out["aum_units"] <= TOL) & (out["our_units"] > TOL)
    out.loc[no_aum_units, "status"] = "txn_only"
    return out


RECON_COLS = ["rta", "folio", "product", "scheme", "investor", "amc", "aum_date",
              "aum_units", "our_units", "diff", "folio_master_units", "last_txn_date", "status"]


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
                continue
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