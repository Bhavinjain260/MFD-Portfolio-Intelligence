import sqlite3
from pathlib import Path

# Resolve DB path relative to this script (goes up one level to new_version/)
DB_FILE = str(Path(__file__).parent.parent / "mfd_local.db")


def get_folio_units(folio_number: str):
    """
    Returns unit holdings for a given KFin folio.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Sum units per scheme
    query = """
        SELECT 
            fmcode,
            SUM(td_units) as total_units
        FROM kfin_transactions
        WHERE td_acno = ?
        GROUP BY fmcode
        HAVING total_units != 0
        ORDER BY total_units DESC
    """

    cursor.execute(query, (folio_number,))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print(f"❌ No transactions found for folio: {folio_number}")
        return []

    total_units = 0.0
    print(f"\n📋 Unit Holdings for Folio: {folio_number}\n")
    print(f"{'Scheme Code (fmcode)':<20} | {'Units':>15}")
    print("-" * 40)

    for fmcode, units in rows:
        total_units += units
        print(f"{fmcode:<20} | {units:>15.4f}")

    print("-" * 40)
    print(f"{'TOTAL':<20} | {total_units:>15.4f}\n")

    return rows, total_units


if __name__ == "__main__":
    TEST_FOLIO = "77721887803"
    schemes, total = get_folio_units(TEST_FOLIO)
    print(f"🎯 Total units across all schemes: {total:.4f}")