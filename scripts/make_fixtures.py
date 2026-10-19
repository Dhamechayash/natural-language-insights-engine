"""Generate small deterministic CSVs used by tests and the second-CSV demo."""

import csv
import datetime as dt
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
rng = random.Random(7)


def retail(path: Path, n_invoices: int = 400):
    products = [(f"P{1000 + i}", f"ITEM {i}", round(rng.uniform(0.5, 25), 2)) for i in range(40)]
    countries = ["United Kingdom"] * 8 + ["France", "Germany", "EIRE"]
    with open(path, "w", newline="", encoding="latin-1") as f:
        w = csv.writer(f)
        w.writerow(["InvoiceNo", "StockCode", "Description", "Quantity", "InvoiceDate", "UnitPrice", "CustomerID", "Country"])
        for i in range(n_invoices):
            when = dt.datetime(2010, 12, 1) + dt.timedelta(days=rng.randint(0, 364), minutes=rng.randint(480, 1000))
            cust = rng.choice([None] + list(range(12000, 12060)))
            country = rng.choice(countries)
            cancelled = rng.random() < 0.05
            for code, desc, price in rng.sample(products, rng.randint(1, 5)):
                qty = rng.randint(1, 12) * (-1 if cancelled else 1)
                w.writerow([("C" if cancelled else "") + str(536000 + i), code, desc, qty,
                            f"{when.month}/{when.day}/{when.year} {when.hour}:{when.minute:02d}", price,
                            "" if cust is None else f"{cust}.0", country])


def orders(path: Path, n: int = 300):
    skus = [(f"SKU-{i:03d}", rng.choice(["toys", "home", "garden"]), round(rng.uniform(3, 90), 2)) for i in range(25)]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Order ID", "Ordered At", "SKU", "Category", "Units", "Price (EUR)", "Buyer Email", "Region"])
        for i in range(n):
            sku, cat, price = rng.choice(skus)
            when = dt.date(2023, 1, 1) + dt.timedelta(days=rng.randint(0, 364))
            w.writerow([f"O-{i}", when.isoformat(), sku, cat, rng.randint(1, 5), price,
                        f"buyer{rng.randint(1, 80)}@example.com", rng.choice(["north", "south", "east", "west"])])


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    retail(OUT / "retail_small.csv")
    orders(OUT / "orders_second.csv")
