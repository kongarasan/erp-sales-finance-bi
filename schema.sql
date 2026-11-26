-- =============================================================================
-- ERP Sales & Finance reporting - star schema
--
--   dim_customer ─┐
--   dim_costcentre ┼──< fact_invoice >── dim_date (role-playing: invoice / due / payment)
--
-- Grain of the fact: ONE ROW PER INVOICE (header level, no line items).
--
-- Rule of thumb used below:
--   * Fact  = numeric, additive measurements and the event's own dates/state,
--             i.e. things that are different for every invoice.
--   * Dimension = descriptive attributes you filter/group BY, i.e. things that
--             are the same for every invoice of that customer / cost centre / day.
--
-- Idempotent: drops and recreates everything (load.py does truncate-and-reload
-- by simply re-running this file inside one transaction).
-- =============================================================================

DROP TABLE IF EXISTS fact_invoice   CASCADE;   -- CASCADE also drops dependent views
DROP TABLE IF EXISTS dim_customer   CASCADE;
DROP TABLE IF EXISTS dim_costcentre CASCADE;
DROP TABLE IF EXISTS dim_date       CASCADE;

-- -----------------------------------------------------------------------------
-- dim_date: one row per calendar day. Uses a "smart" integer key YYYYMMDD:
-- still a surrogate (meaningless to joins), but human-readable and stable,
-- so it doesn't need a lookup during the load.
-- -----------------------------------------------------------------------------
CREATE TABLE dim_date (
    date_key        INTEGER     PRIMARY KEY,          -- 20240315
    full_date       DATE        NOT NULL UNIQUE,
    year            SMALLINT    NOT NULL,
    quarter         SMALLINT    NOT NULL,
    month           SMALLINT    NOT NULL,
    month_name      TEXT        NOT NULL,
    year_month      CHAR(7)     NOT NULL,             -- '2024-03', sortable text label
    iso_week        SMALLINT    NOT NULL,
    day_of_month    SMALLINT    NOT NULL,
    day_of_week     SMALLINT    NOT NULL,             -- ISO: 1 = Monday ... 7 = Sunday
    day_name        TEXT        NOT NULL,
    is_weekend      BOOLEAN     NOT NULL
    -- Calendar attributes live here, not on the fact: they are a pure function
    -- of the date, so storing them on 60k fact rows would just repeat them.
);

-- Populate a generous range: payments can precede 2023 (dirty rows) and
-- due dates extend into 2027. generate_series emits one row per day.
INSERT INTO dim_date
SELECT
    to_char(d, 'YYYYMMDD')::INT,
    d::DATE,
    EXTRACT(YEAR    FROM d),
    EXTRACT(QUARTER FROM d),
    EXTRACT(MONTH   FROM d),
    trim(to_char(d, 'Month')),       -- to_char pads month names to 9 chars -> trim
    to_char(d, 'YYYY-MM'),
    EXTRACT(WEEK    FROM d),         -- ISO 8601 week number
    EXTRACT(DAY     FROM d),
    EXTRACT(ISODOW  FROM d),
    trim(to_char(d, 'Day')),
    EXTRACT(ISODOW  FROM d) IN (6, 7)
FROM generate_series(DATE '2022-01-01', DATE '2027-12-31', INTERVAL '1 day') AS g(d);

-- -----------------------------------------------------------------------------
-- dim_customer (SCD Type 1: attributes are overwritten, no history kept)
-- -----------------------------------------------------------------------------
CREATE TABLE dim_customer (
    customer_key        INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- surrogate key
    customer_number     TEXT          NOT NULL UNIQUE,  -- natural/business key from the ERP
    customer_name       TEXT          NOT NULL,         -- NOT trimmed on purpose (dirty data)
    segment             TEXT          NOT NULL,
    industry            TEXT          NOT NULL,
    city                TEXT          NOT NULL,
    postcode            TEXT          NOT NULL,         -- text: German PLZ can start with 0
    federal_state       TEXT          NOT NULL,
    sales_region        TEXT          NOT NULL,
    country             CHAR(2)       NOT NULL,
    payment_terms_days  SMALLINT      NOT NULL,         -- contractual DEFAULT terms of the customer
    credit_limit        NUMERIC(12,2) NOT NULL,
    customer_since      DATE          NOT NULL
    -- Why on the dimension: name, segment, region, terms and credit limit describe
    -- the customer, not an individual invoice. Putting them on the fact would
    -- duplicate them 60k times and make a rename an update of thousands of rows.
    -- Why a surrogate key: the ERP number could be reused/re-keyed, and an
    -- integer key keeps the fact narrow and joins fast. It also enables SCD2 later.
);

-- -----------------------------------------------------------------------------
-- dim_costcentre
-- -----------------------------------------------------------------------------
CREATE TABLE dim_costcentre (
    costcentre_key   INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    costcentre_code  TEXT NOT NULL UNIQUE,        -- natural key, e.g. CC-4101
    costcentre_name  TEXT NOT NULL,
    department       TEXT NOT NULL,               -- hierarchy level 1
    sales_region     TEXT NOT NULL,               -- hierarchy level 2
    manager_name     TEXT NOT NULL
    -- The department/region hierarchy is flattened into this one table
    -- (star, not snowflake): Power BI handles wide dimensions better than
    -- chains of small lookup tables.
);

-- -----------------------------------------------------------------------------
-- fact_invoice
-- -----------------------------------------------------------------------------
CREATE TABLE fact_invoice (
    invoice_key       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Degenerate dimension: an identifier with no attributes of its own, so it
    -- stays on the fact instead of getting a table. Deliberately NOT UNIQUE:
    -- the source contains duplicates, which you are supposed to find and clean.
    invoice_number    TEXT    NOT NULL,

    -- Foreign keys to the dimensions (the "who / where")
    customer_key      INTEGER NOT NULL REFERENCES dim_customer(customer_key),
    costcentre_key    INTEGER NOT NULL REFERENCES dim_costcentre(costcentre_key),

    -- Role-playing date keys: the same dim_date is used in three roles.
    -- In Power BI only one relationship can be active (invoice date);
    -- the others are activated per measure with USERELATIONSHIP().
    invoice_date_key  INTEGER NOT NULL REFERENCES dim_date(date_key),
    due_date_key      INTEGER NOT NULL REFERENCES dim_date(date_key),
    payment_date_key  INTEGER          REFERENCES dim_date(date_key),   -- NULL = not paid (or dirty)

    -- The actual DATE values are kept as well. Redundant with the keys, but
    -- date arithmetic (days-to-pay, days overdue) is the core of DSO/ageing,
    -- and "payment_date - invoice_date" is far clearer than joining dim_date 3x.
    invoice_date      DATE    NOT NULL,
    due_date          DATE    NOT NULL,     -- invoice-specific: terms could be negotiated per deal
    payment_date      DATE,

    -- Status of THIS invoice -> fact. (Not a CHECK on payment_date consistency:
    -- PAID rows with NULL payment_date are an intended data-quality issue.)
    payment_status    TEXT    NOT NULL CHECK (payment_status IN ('PAID', 'OPEN')),

    -- Measures: numeric and additive across every dimension -> always fact.
    net_amount        NUMERIC(14,2) NOT NULL,
    vat_rate          NUMERIC(4,3)  NOT NULL,   -- rate is per invoice (19% / 7% / reverse charge)
    vat_amount        NUMERIC(14,2) NOT NULL,
    gross_amount      NUMERIC(14,2) NOT NULL,
    currency          CHAR(3)       NOT NULL DEFAULT 'EUR'
);

-- -----------------------------------------------------------------------------
-- Indexes. Postgres does NOT automatically index foreign-key columns
-- (only PK/UNIQUE get indexes), so we add them for join/filter performance.
-- -----------------------------------------------------------------------------
CREATE INDEX ix_fact_invoice_customer     ON fact_invoice (customer_key);
CREATE INDEX ix_fact_invoice_costcentre   ON fact_invoice (costcentre_key);
CREATE INDEX ix_fact_invoice_invoice_date ON fact_invoice (invoice_date_key);
CREATE INDEX ix_fact_invoice_payment_date ON fact_invoice (payment_date_key);
CREATE INDEX ix_fact_invoice_number       ON fact_invoice (invoice_number);  -- duplicate hunting / lookups

-- Partial index: only the (few) OPEN invoices are indexed. Ageing and DSO
-- queries filter on open items, so this index is tiny but exactly what they need.
CREATE INDEX ix_fact_invoice_open_due
    ON fact_invoice (due_date)
    WHERE payment_status = 'OPEN';
