"""Download the UCI Online Retail dataset (xlsx) and convert it to data/online_retail.csv.

    python -I scripts/fetch_uci.py [path/to/online_retail.zip]
"""

import csv
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import openpyxl

URL = "https://archive.ics.uci.edu/static/public/352/online+retail.zip"
OUT = Path(__file__).resolve().parent.parent / "data" / "online_retail.csv"


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="uci_"))  # downloaded files stay in their own directory
    archive = Path(sys.argv[1]) if len(sys.argv) > 1 else work / "online_retail.zip"
    if len(sys.argv) <= 1:
        print("Downloading", URL)
        urllib.request.urlretrieve(URL, archive)
    with zipfile.ZipFile(archive) as z:
        xlsx = next(n for n in z.namelist() if n.lower().endswith(".xlsx"))
        z.extract(xlsx, work)
    OUT.parent.mkdir(exist_ok=True)
    wb = openpyxl.load_workbook(work / xlsx, read_only=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in wb.active.iter_rows(values_only=True):
            writer.writerow(["" if v is None else (v.isoformat(sep=" ") if hasattr(v, "isoformat") else v) for v in row])
    print("Wrote", OUT)


if __name__ == "__main__":
    main()
