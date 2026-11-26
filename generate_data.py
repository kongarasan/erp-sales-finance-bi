"""
Synthetic German B2B ERP dataset (sales & receivables).

    python generate_data.py      -> writes CSVs to data/raw/ (for Power Query practice)
    from generate_data import generate   -> used by load.py

The data is deterministic (fixed seed) and DELIBERATELY DIRTY. Injected defects:
  1. ~2% of PAID invoices have payment_date = NULL
  2. duplicate invoice numbers: exact duplicate rows (double import) and
     number collisions (two different invoices sharing one number)
  3. 3 customers whose names carry trailing whitespace
  4. some PAID invoices with payment_date before invoice_date
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from faker import Faker

SEED = 42
START = date(2023, 1, 1)
AS_OF = date(2026, 11, 26)          # "today" of the dataset; later payments are still open
N_CUSTOMERS = 800
N_INVOICES = 60_000
VAT_RATE = 0.19

SEGMENTS = ["Key Account", "Mid-Market", "SMB"]
SEGMENT_P = [0.08, 0.32, 0.60]
SEGMENT_MU = {"Key Account": 8.4, "Mid-Market": 7.7, "SMB": 6.9}   # lognormal mean of net amount
SEGMENT_WEIGHT = {"Key Account": 6.0, "Mid-Market": 2.0, "SMB": 1.0}  # invoice frequency
CREDIT_LIMIT = {"Key Account": 250_000, "Mid-Market": 75_000, "SMB": 20_000}

INDUSTRIES = ["Maschinenbau", "Automobilzulieferer", "Chemie", "Logistik", "Elektrotechnik",
              "Lebensmittel", "Medizintechnik", "Bauwesen", "Großhandel", "IT-Dienstleistungen"]

REGIONS = ["Nord", "Süd", "West", "Ost"]
# (city, postcode prefix, federal state, sales region) — keeps city/state/region consistent
CITIES = [
    ("Hamburg", "20", "Hamburg", "Nord"), ("Bremen", "28", "Bremen", "Nord"),
    ("Hannover", "30", "Niedersachsen", "Nord"), ("Kiel", "24", "Schleswig-Holstein", "Nord"),
    ("Osnabrück", "49", "Niedersachsen", "Nord"), ("Wolfsburg", "38", "Niedersachsen", "Nord"),
    ("München", "80", "Bayern", "Süd"), ("Nürnberg", "90", "Bayern", "Süd"),
    ("Augsburg", "86", "Bayern", "Süd"), ("Regensburg", "93", "Bayern", "Süd"),
    ("Stuttgart", "70", "Baden-Württemberg", "Süd"), ("Karlsruhe", "76", "Baden-Württemberg", "Süd"),
    ("Mannheim", "68", "Baden-Württemberg", "Süd"), ("Ulm", "89", "Baden-Württemberg", "Süd"),
    ("Köln", "50", "Nordrhein-Westfalen", "West"), ("Düsseldorf", "40", "Nordrhein-Westfalen", "West"),
    ("Dortmund", "44", "Nordrhein-Westfalen", "West"), ("Essen", "45", "Nordrhein-Westfalen", "West"),
    ("Bielefeld", "33", "Nordrhein-Westfalen", "West"), ("Frankfurt am Main", "60", "Hessen", "West"),
    ("Mainz", "55", "Rheinland-Pfalz", "West"), ("Saarbrücken", "66", "Saarland", "West"),
    ("Berlin", "10", "Berlin", "Ost"), ("Leipzig", "04", "Sachsen", "Ost"),
    ("Dresden", "01", "Sachsen", "Ost"), ("Chemnitz", "09", "Sachsen", "Ost"),
    ("Erfurt", "99", "Thüringen", "Ost"), ("Magdeburg", "39", "Sachsen-Anhalt", "Ost"),
    ("Potsdam", "14", "Brandenburg", "Ost"), ("Rostock", "18", "Mecklenburg-Vorpommern", "Ost"),
]
# rough weighting toward the industrial West/South
CITY_W = np.array([5, 2, 3, 1, 1, 2, 6, 3, 2, 1, 6, 2, 2, 1, 5, 4, 3, 3, 2, 5, 1, 1, 5, 2, 2, 1, 1, 1, 1, 1], float)

# 10 departments x 4 regions = 40 cost centres; multiplier scales typical invoice size
DEPARTMENTS = [
    ("Vertrieb Maschinen", 2.5, 0.20), ("Vertrieb Ersatzteile", 0.6, 0.20),
    ("Service & Wartung", 0.8, 0.18), ("Projektgeschäft", 3.5, 0.06),
    ("Software & Lizenzen", 1.2, 0.08), ("Schulungen", 0.4, 0.05),
    ("Logistik-Dienstleistungen", 0.7, 0.07), ("Miete & Leasing", 1.0, 0.06),
    ("Export", 1.8, 0.04), ("Key-Account-Management", 2.0, 0.06),
]


def _customers(rng: np.random.Generator, fake: Faker) -> pd.DataFrame:
    n = N_CUSTOMERS
    seg = rng.choice(SEGMENTS, size=n, p=SEGMENT_P)
    city_idx = rng.choice(len(CITIES), size=n, p=CITY_W / CITY_W.sum())
    city = [CITIES[i] for i in city_idx]
    df = pd.DataFrame({
        "customer_number": [f"K{10001 + i}" for i in range(n)],
        "customer_name": [fake.unique.company() for _ in range(n)],
        "segment": seg,
        "industry": rng.choice(INDUSTRIES, size=n),
        "city": [c[0] for c in city],
        "postcode": [c[1] + f"{rng.integers(0, 1000):03d}" for c in city],
        "federal_state": [c[2] for c in city],
        "sales_region": [c[3] for c in city],
        "country": "DE",
        "payment_terms_days": rng.choice([14, 30, 30, 30, 45, 60], size=n),
        "customer_since": pd.Timestamp("2008-01-01")
        + pd.to_timedelta(rng.integers(0, 365 * 15, size=n), unit="D"),
    })
    df["credit_limit"] = (df["segment"].map(CREDIT_LIMIT) * rng.uniform(0.5, 1.5, n)).round(-3)
    # Hidden behaviour (prefixed "_", not loaded): ~12% are chronic late payers.
    slow = rng.random(n) < 0.12
    df["_pay_delay_mean"] = np.where(slow, rng.normal(35, 12, n), rng.normal(2, 6, n))
    # Pareto weights -> realistic revenue concentration (top-20 view becomes meaningful)
    df["_weight"] = (rng.pareto(1.3, n) + 1) * df["segment"].map(SEGMENT_WEIGHT).to_numpy()
    df["_size"] = rng.lognormal(0, 0.35, n)
    return df


def _costcentres(fake: Faker) -> pd.DataFrame:
    rows = []
    for d, (dept, _, _) in enumerate(DEPARTMENTS):
        for r, region in enumerate(REGIONS):
            rows.append({
                "costcentre_code": f"CC-{4000 + (d + 1) * 100 + r + 1}",
                "costcentre_name": f"{dept} {region}",
                "department": dept,
                "sales_region": region,
                "manager_name": fake.name(),
            })
    return pd.DataFrame(rows)


def _invoices(rng: np.random.Generator, cust: pd.DataFrame, cc: pd.DataFrame) -> pd.DataFrame:
    n = N_INVOICES
    # Invoice dates: growth trend + seasonality, almost nothing on weekends, Aug/Christmas dip
    days = pd.date_range(START, AS_OF, freq="D")
    t = (days - days[0]).days.to_numpy() / 365.25
    w = (1 + 0.12 * t) * (1 + 0.12 * np.cos(2 * np.pi * (days.dayofyear.to_numpy() - 80) / 365.25))
    w *= np.where(days.dayofweek.to_numpy() >= 5, 0.03, 1.0)
    w *= np.where((days.month == 8) | ((days.month == 12) & (days.day > 22)), 0.6, 1.0)
    inv_date = days.to_numpy().astype("datetime64[D]")[rng.choice(len(days), size=n, p=w / w.sum())]

    cidx = rng.choice(len(cust), size=n, p=(cust["_weight"] / cust["_weight"].sum()).to_numpy())
    seg = cust["segment"].to_numpy()[cidx]

    # Cost centre: department by mix, region = customer's region (90%) else random
    dept_p = np.array([d[2] for d in DEPARTMENTS])
    dept = rng.choice(len(DEPARTMENTS), size=n, p=dept_p / dept_p.sum())
    cust_region = pd.Series(cust["sales_region"].to_numpy()[cidx]).map({r: i for i, r in enumerate(REGIONS)}).to_numpy()
    region = np.where(rng.random(n) < 0.9, cust_region, rng.integers(0, 4, n))
    cc_code = cc["costcentre_code"].to_numpy()[dept * 4 + region]

    mu = pd.Series(seg).map(SEGMENT_MU).to_numpy()
    dept_mult = np.array([d[1] for d in DEPARTMENTS])[dept]
    net = np.round(rng.lognormal(mu, 0.85) * dept_mult * cust["_size"].to_numpy()[cidx], 2)
    net = np.clip(net, 25, None)

    terms = cust["payment_terms_days"].to_numpy()[cidx].astype("timedelta64[D]")
    due = inv_date + terms
    delay = np.round(cust["_pay_delay_mean"].to_numpy()[cidx] + rng.normal(0, 6, n)).astype(int)
    pay = np.maximum(due + delay.astype("timedelta64[D]"), inv_date + np.timedelta64(1, "D"))
    bad_debt = rng.random(n) < 0.008              # never paid -> feeds the 90+ bucket
    is_open = (pay > np.datetime64(AS_OF)) | bad_debt

    df = pd.DataFrame({
        "customer_number": cust["customer_number"].to_numpy()[cidx],
        "costcentre_code": cc_code,
        "invoice_date": pd.to_datetime(inv_date),
        "due_date": pd.to_datetime(due),
        "payment_date": pd.to_datetime(np.where(is_open, np.datetime64("NaT"), pay)),
        "payment_status": np.where(is_open, "OPEN", "PAID"),
        "net_amount": net,
    })
    df["vat_rate"] = VAT_RATE
    df["vat_amount"] = (df["net_amount"] * VAT_RATE).round(2)
    df["gross_amount"] = df["net_amount"] + df["vat_amount"]
    df["currency"] = "EUR"

    df = df.sort_values("invoice_date", kind="stable").reset_index(drop=True)
    year = df["invoice_date"].dt.year
    seq = df.groupby(year).cumcount() + 1
    df.insert(0, "invoice_number", "RE-" + year.astype(str) + "-" + seq.map("{:06d}".format))
    return df


def _inject_defects(rng: np.random.Generator, cust: pd.DataFrame, inv: pd.DataFrame):
    log = {}
    paid = inv.index[inv["payment_status"] == "PAID"].to_numpy()
    rng.shuffle(paid)

    # 1. ~2% of PAID invoices lose their payment_date (status still says PAID)
    n_null = int(len(paid) * 0.02)
    inv.loc[paid[:n_null], "payment_date"] = pd.NaT
    log["paid_with_null_payment_date"] = n_null

    # 4. payment_date before invoice_date (typo / wrong posting date)
    neg = paid[n_null:n_null + 120]
    inv.loc[neg, "payment_date"] = inv.loc[neg, "invoice_date"] - pd.to_timedelta(rng.integers(1, 31, len(neg)), unit="D")
    log["payment_before_invoice"] = len(neg)

    # 2a. invoice-number collisions: different invoices reusing a neighbour's number
    coll = rng.choice(np.arange(1, len(inv)), size=10, replace=False)
    inv.loc[coll, "invoice_number"] = inv.loc[coll - 1, "invoice_number"].to_numpy()
    # 2b. exact duplicate rows (same invoice imported twice)
    dup = inv.loc[rng.choice(inv.index, size=15, replace=False)]
    inv = pd.concat([inv, dup], ignore_index=True)
    log["invoice_number_collisions"] = 10
    log["exact_duplicate_rows"] = 15

    # 3. trailing whitespace in 3 customer names
    ws = rng.choice(cust.index, size=3, replace=False)
    cust.loc[ws, "customer_name"] = cust.loc[ws, "customer_name"] + np.array(["  ", " ", "   "])
    log["customer_names_trailing_ws"] = 3
    return cust, inv, log


def generate(seed: int = SEED, verbose: bool = True) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    fake = Faker("de_DE")
    Faker.seed(seed)
    cust = _customers(rng, fake)
    cc = _costcentres(fake)
    inv = _invoices(rng, cust, cc)
    cust, inv, log = _inject_defects(rng, cust, inv)
    if verbose:
        print("Injected defects:", ", ".join(f"{k}={v}" for k, v in log.items()))
    public = [c for c in cust.columns if not c.startswith("_")]
    return {"customers": cust[public], "costcentres": cc, "invoices": inv}


if __name__ == "__main__":
    out = Path(__file__).parent / "data" / "raw"
    out.mkdir(parents=True, exist_ok=True)
    for name, df in generate().items():
        df.to_csv(out / f"{name}.csv", index=False, date_format="%Y-%m-%d")
        print(f"{name:12s} {len(df):>7,} rows -> {out / (name + '.csv')}")
