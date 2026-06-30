import sys
sys.path.append(r"C:\Users\Bhavin\Documents\SW\MFDSoftware\MFD")
from data_manager import _read_csv_auto, _clean_cols

with open(r"C:\Users\Bhavin\Documents\SW\MFDSoftware\MFD\Reports\Karvy\20-06-2026\MFSD205_WBBRR2774589_part1.csv", "rb") as f:
    df = _read_csv_auto(f)
df = _clean_cols(df)
for c in df.columns:
    print(repr(c))