-- =============================================================================
-- Analytics views on top of the star schema.
--
-- Reference date: the data is synthetic and ends on a fixed day, so instead of
-- CURRENT_DATE (which would make every number drift as real time passes) the
-- views use the latest invoice_date in the fact as "as-of date".
--
-- NOTE: these views run on the RAW (dirty) data. Duplicates inflate revenue
-- slightly; rows that break a calculation are excluded and called out inline.
-- After you clean the data, the numbers will shift - a good interview story.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1) Revenue by month
--    Revenue = net amount (excl. VAT) recognised on invoice date (accrual view).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_revenue_by_month AS
SELECT
    d.year,
    d.month,
    d.year_month,
    make_date(d.year, d.month, 1)            AS month_start,     -- real DATE for charting axes
    COUNT(*)                                  AS invoice_count,
    COUNT(DISTINCT f.customer_key)            AS active_customers,
    SUM(f.net_amount)                         AS net_revenue,
    SUM(f.gross_amount)                       AS gross_revenue,

    -- Window function over an aggregate: SUM(SUM(x)) OVER (...).
    -- The inner SUM is the GROUP BY aggregate (revenue of this month); the outer
    -- SUM ... OVER runs AFTER grouping and adds up the monthly rows.
    -- PARTITION BY year restarts the running total every January -> YTD.
    SUM(SUM(f.net_amount)) OVER (PARTITION BY d.year ORDER BY d.month) AS net_revenue_ytd,

    -- LAG(x, 12) = value 12 rows earlier in the given order = same month last year.
    -- Valid because every month has invoices (no gaps); with gaps you'd join on
    -- year-1 instead. NULL for 2023, as there is no prior year.
    LAG(SUM(f.net_amount), 12) OVER (ORDER BY d.year, d.month)        AS net_revenue_prev_year,

    -- NULLIF(x, 0) turns a 0 denominator into NULL, so division yields NULL
    -- instead of a "division by zero" error.
    ROUND(100.0 * (SUM(f.net_amount) - LAG(SUM(f.net_amount), 12) OVER (ORDER BY d.year, d.month))
          / NULLIF(LAG(SUM(f.net_amount), 12) OVER (ORDER BY d.year, d.month), 0), 1) AS yoy_pct
FROM fact_invoice f
JOIN dim_date d ON d.date_key = f.invoice_date_key
GROUP BY d.year, d.month, d.year_month;


-- -----------------------------------------------------------------------------
-- 2) DSO by customer
--    Two complementary definitions:
--    a) dso_days (balance-based, the classic finance KPI):
--         open receivables / gross revenue of the last 365 days * 365
--       "How many days of sales are currently sitting unpaid?"
--       Gross on both sides: receivables include VAT, so revenue must too.
--    b) avg_days_to_pay (behaviour-based): average payment_date - invoice_date,
--       weighted by amount so one big late invoice counts more than a small one.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_dso_by_customer AS
WITH as_of AS (                       -- CTE: a named sub-query, here a one-row "parameter"
    SELECT MAX(invoice_date) AS as_of_date FROM fact_invoice
),
per_customer AS (
    SELECT
        f.customer_key,
        -- FILTER (WHERE ...) = conditional aggregate; equivalent to
        -- SUM(CASE WHEN cond THEN x END) but more readable (Postgres/SQL:2003).
        SUM(f.gross_amount) FILTER (WHERE f.invoice_date > a.as_of_date - 365)  AS gross_revenue_365d,
        SUM(f.gross_amount) FILTER (WHERE f.payment_status = 'OPEN')            AS open_receivables,
        COUNT(*)            FILTER (WHERE f.payment_status = 'OPEN')            AS open_invoices,

        -- DATE - DATE in Postgres returns an INTEGER number of days.
        -- Rows with NULL payment_date are ignored by SUM automatically;
        -- rows paid "before" invoicing (dirty) are excluded explicitly.
        SUM((f.payment_date - f.invoice_date) * f.gross_amount)
            FILTER (WHERE f.payment_date >= f.invoice_date)
          / NULLIF(SUM(f.gross_amount) FILTER (WHERE f.payment_date >= f.invoice_date), 0)
                                                                                AS avg_days_to_pay,
        COUNT(*) FILTER (WHERE f.payment_date < f.invoice_date)                 AS invalid_payment_rows
    FROM fact_invoice f
    CROSS JOIN as_of a                -- CROSS JOIN with a 1-row CTE = attach the parameter to every row
    GROUP BY f.customer_key
)
SELECT
    c.customer_key,
    c.customer_number,
    c.customer_name,
    c.segment,
    c.payment_terms_days,
    p.gross_revenue_365d,
    COALESCE(p.open_receivables, 0)                       AS open_receivables,  -- NULL (no open items) -> 0
    p.open_invoices,
    ROUND(COALESCE(p.open_receivables, 0) / NULLIF(p.gross_revenue_365d, 0) * 365, 1) AS dso_days,
    ROUND(p.avg_days_to_pay, 1)                           AS avg_days_to_pay,
    -- Paying later than the agreed terms? Positive = late on average.
    ROUND(p.avg_days_to_pay - c.payment_terms_days, 1)    AS avg_days_beyond_terms,
    p.invalid_payment_rows
FROM per_customer p
JOIN dim_customer c ON c.customer_key = p.customer_key;


-- -----------------------------------------------------------------------------
-- 3) Overdue ageing buckets
--    Only OPEN invoices whose due date has passed. Days overdue are counted
--    from the DUE date (not the invoice date) - that's the standard AR ageing.
--    Known gap: PAID rows with NULL payment_date are trusted as paid here.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_overdue_ageing AS
WITH as_of AS (
    SELECT MAX(invoice_date) AS as_of_date FROM fact_invoice
),
overdue AS (
    SELECT
        f.gross_amount,
        a.as_of_date - f.due_date AS days_overdue
    FROM fact_invoice f
    CROSS JOIN as_of a
    WHERE f.payment_status = 'OPEN'
      AND f.due_date < a.as_of_date          -- uses the partial index ix_fact_invoice_open_due
),
bucketed AS (
    SELECT
        gross_amount,
        days_overdue,
        -- CASE is evaluated top-down and stops at the first match,
        -- so each branch only needs an upper bound.
        CASE
            WHEN days_overdue <= 30 THEN '0-30'
            WHEN days_overdue <= 60 THEN '31-60'
            WHEN days_overdue <= 90 THEN '61-90'
            ELSE '90+'
        END AS ageing_bucket,
        -- Text labels sort alphabetically. That happens to work for these four,
        -- but breaks as soon as a bucket like '120+' or 'Not due' is added, so
        -- ship an explicit numeric sort column (use "Sort by column" in Power BI).
        CASE
            WHEN days_overdue <= 30 THEN 1
            WHEN days_overdue <= 60 THEN 2
            WHEN days_overdue <= 90 THEN 3
            ELSE 4
        END AS bucket_order
    FROM overdue
)
SELECT
    ageing_bucket,
    bucket_order,
    COUNT(*)                 AS invoice_count,
    SUM(gross_amount)        AS overdue_amount,
    ROUND(AVG(days_overdue)) AS avg_days_overdue,
    -- Empty OVER () = window spanning the whole (grouped) result:
    -- the grand total, used to compute each bucket's share without a sub-query.
    ROUND(100.0 * SUM(gross_amount) / SUM(SUM(gross_amount)) OVER (), 1) AS pct_of_overdue
FROM bucketed
GROUP BY ageing_bucket, bucket_order;


-- -----------------------------------------------------------------------------
-- 4) Top-20 customers by net revenue (all time in the dataset)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_top20_customers AS
WITH revenue AS (
    SELECT customer_key,
           SUM(net_amount) AS net_revenue,
           COUNT(*)        AS invoice_count
    FROM fact_invoice
    GROUP BY customer_key
),
ranked AS (
    SELECT
        r.*,
        -- RANK(): ties share a rank and leave gaps (1,2,2,4). ROW_NUMBER() would
        -- break ties arbitrarily; DENSE_RANK() would not leave gaps (1,2,2,3).
        RANK() OVER (ORDER BY r.net_revenue DESC)              AS revenue_rank,
        r.net_revenue / SUM(r.net_revenue) OVER ()             AS revenue_share,
        -- Running total with an explicit ROWS frame. Without it, the default
        -- frame is RANGE, which would lump tied values into the same step.
        SUM(r.net_revenue) OVER (ORDER BY r.net_revenue DESC
                                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
          / SUM(r.net_revenue) OVER ()                         AS cumulative_share   -- Pareto / ABC analysis
    FROM revenue r
)
SELECT
    k.revenue_rank,
    c.customer_number,
    rtrim(c.customer_name)                 AS customer_name,   -- display-only trim; source stays dirty
    c.segment,
    c.industry,
    k.net_revenue,
    k.invoice_count,
    ROUND(100 * k.revenue_share, 2)        AS revenue_share_pct,
    ROUND(100 * k.cumulative_share, 2)     AS cumulative_share_pct
FROM ranked k
JOIN dim_customer c ON c.customer_key = k.customer_key
WHERE k.revenue_rank <= 20      -- window results can't be used in WHERE of the same level -> filter one level up
ORDER BY k.revenue_rank;        -- ORDER BY in a view is a convenience only; consumers should sort themselves
