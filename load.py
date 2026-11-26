"""
Idempotent loader: generate data -> (re)create star schema -> COPY data -> create views.

    docker compose up -d
    python load.py

Everything runs in ONE transaction: if any step fails, the database is left
exactly as it was before (no half-loaded tables).
"""
from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv

from generate_data import generate

ROOT = Path(__file__).resolve().parent


def connect(retries: int = 30) -> "psycopg2.extensions.connection":
    """Connect, retrying while the container is still starting up."""
    load_dotenv(ROOT / ".env")
    params = dict(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )
    for attempt in range(1, retries + 1):
        try:
            return psycopg2.connect(**params)
        except psycopg2.OperationalError as exc:
            if "authentication failed" in str(exc):
                sys.exit(f"Wrong credentials for {params['host']}:{params['port']} - is another "
                         f"PostgreSQL listening on that port? Check .env. ({exc})")
            if attempt == retries:
                sys.exit(f"Could not connect to Postgres at {params['host']}:{params['port']}: {exc}")
            print(f"Waiting for Postgres ({attempt}/{retries})...")
            time.sleep(1)


def copy_df(cur, df: pd.DataFrame, table: str, cols: list[str]) -> None:
    """Bulk-load a DataFrame with COPY (much faster than row-by-row INSERTs).
    Empty CSV fields become NULL; quoted fields keep trailing whitespace."""
    buf = io.StringIO()
    df[cols].to_csv(buf, index=False, header=False, date_format="%Y-%m-%d", na_rep="")
    buf.seek(0)
    cur.copy_expert(f"COPY {table} ({', '.join(cols)}) FROM STDIN WITH (FORMAT csv, NULL '')", buf)


def date_key(s: pd.Series) -> pd.Series:
    """2024-03-15 -> 20240315 as nullable integer (NaT -> <NA> -> NULL)."""
    return pd.to_numeric(s.dt.strftime("%Y%m%d"), errors="coerce").astype("Int64")


def main() -> None:
    data = generate()
    cust, cc, inv = data["customers"], data["costcentres"], data["invoices"].copy()

    conn = connect()
    try:
        with conn, conn.cursor() as cur:          # `with conn` = commit on success, rollback on error
            cur.execute((ROOT / "schema.sql").read_text(encoding="utf-8"))

            copy_df(cur, cust, "dim_customer", list(cust.columns))
            copy_df(cur, cc, "dim_costcentre", list(cc.columns))

            # Resolve natural keys -> surrogate keys assigned by Postgres
            cur.execute("SELECT customer_number, customer_key FROM dim_customer")
            inv["customer_key"] = inv["customer_number"].map(dict(cur.fetchall()))
            cur.execute("SELECT costcentre_code, costcentre_key FROM dim_costcentre")
            inv["costcentre_key"] = inv["costcentre_code"].map(dict(cur.fetchall()))
            for role in ("invoice", "due", "payment"):
                inv[f"{role}_date_key"] = date_key(inv[f"{role}_date"])

            copy_df(cur, inv, "fact_invoice", [
                "invoice_number", "customer_key", "costcentre_key",
                "invoice_date_key", "due_date_key", "payment_date_key",
                "invoice_date", "due_date", "payment_date", "payment_status",
                "net_amount", "vat_rate", "vat_amount", "gross_amount", "currency",
            ])

            cur.execute((ROOT / "analytics_views.sql").read_text(encoding="utf-8"))
            cur.execute("ANALYZE")                # refresh planner statistics after bulk load

        with conn.cursor() as cur:
            print("\nRow counts")
            for obj in ("dim_date", "dim_customer", "dim_costcentre", "fact_invoice",
                        "vw_revenue_by_month", "vw_dso_by_customer",
                        "vw_overdue_ageing", "vw_top20_customers"):
                cur.execute(f"SELECT COUNT(*) FROM {obj}")
                print(f"  {obj:22s} {cur.fetchone()[0]:>8,}")

            print("\nData-quality snapshot (left for you to clean)")
            checks = {
                "PAID but payment_date NULL":
                    "SELECT COUNT(*) FROM fact_invoice WHERE payment_status='PAID' AND payment_date IS NULL",
                "payment_date < invoice_date":
                    "SELECT COUNT(*) FROM fact_invoice WHERE payment_date < invoice_date",
                "duplicated invoice_numbers":
                    "SELECT COUNT(*) FROM (SELECT invoice_number FROM fact_invoice "
                    "GROUP BY invoice_number HAVING COUNT(*) > 1) d",
                "customer names w/ trailing ws":
                    "SELECT COUNT(*) FROM dim_customer WHERE customer_name <> rtrim(customer_name)",
            }
            for label, sql in checks.items():
                cur.execute(sql)
                print(f"  {label:30s} {cur.fetchone()[0]:>6,}")
    finally:
        conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
