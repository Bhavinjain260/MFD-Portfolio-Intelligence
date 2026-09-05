import requests
from datetime import datetime

# AMFI Live NAV Text File URL
AMFI_TEXT_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"


def fetch_nav_by_isin(isin: str):
    """
    Standalone function to fetch NAV and Date from AMFI for a specific ISIN.
    """
    isin = isin.strip().upper()
    print(f"🔍 Searching AMFI database for ISIN: {isin} ...\n")

    # 1. Download the AMFI file
    try:
        print("⏳ Downloading live AMFI NAV file (this may take 2-3 seconds)...")
        res = requests.get(AMFI_TEXT_URL, timeout=30)
        res.raise_for_status()
        lines = res.text.splitlines()
        print("✅ File downloaded successfully. Searching...\n")
    except Exception as e:
        print(f"❌ Failed to download AMFI file: {e}")
        return

    # 2. Parse and search for the ISIN
    for line in lines:
        line = line.strip()
        if not line or ";" not in line:
            continue

        parts = line.split(";")
        if len(parts) < 6:
            continue

        # AMFI Format: Scheme Code; ISIN 1; ISIN 2; Scheme Name; NAV; Date
        scheme_code = parts[0].strip()
        isin_1 = parts[1].strip()  # Usually Growth / IDCW Payout
        isin_2 = parts[2].strip()  # Usually IDCW Reinvestment
        scheme_name = parts[3].strip()
        nav_str = parts[4].strip()
        date_str = parts[5].strip()

        # Check if our target ISIN matches either of the two ISIN columns
        if isin_1 == isin or isin_2 == isin:

            # Parse NAV
            try:
                nav = float(nav_str) if nav_str not in ("N.A.", "") else 0.0
            except ValueError:
                nav = 0.0

            # Parse Date
            try:
                nav_date = datetime.strptime(date_str, "%d-%b-%Y").strftime("%Y-%m-%d")
            except ValueError:
                nav_date = "Invalid Date Format"

            # 3. Print the beautiful terminal output
            print("=" * 60)
            print(f"✅ SUCCESS! Match Found for ISIN: {isin}")
            print("=" * 60)
            print(f"📌 Scheme Code : {scheme_code}")
            print(f"📌 Scheme Name : {scheme_name}")
            print(f"📌 NAV         : ₹ {nav}")
            print(f"📌 NAV Date    : {nav_date}")
            matched_col = "ISIN 1 (Growth/Payout)" if isin_1 == isin else "ISIN 2 (Reinvestment)"
            print(f"📌 Matched In  : {matched_col}")
            print("=" * 60)
            return nav, nav_date

    print(f"❌ No scheme found for ISIN: {isin}")
    print("   Please verify the ISIN is correct and belongs to an open-ended mutual fund scheme.")


if __name__ == "__main__":
    # ---------------------------------------------------------
    # TEST IT HERE: Pass any ISIN you want to verify!
    # ---------------------------------------------------------

    # Using the ISIN from your previous Karvy/BSE example:
    TEST_ISIN = "INF194KB1AJ8"

    # You can change the variable above to any other ISIN to test.
    fetch_nav_by_isin(TEST_ISIN)