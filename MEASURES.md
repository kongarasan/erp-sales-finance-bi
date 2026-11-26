# DAX measures for Power BI

Model assumptions (set these up first):

- Relationships: `fact_invoice[customer_key] → dim_customer`, `fact_invoice[costcentre_key] → dim_costcentre`,
  `fact_invoice[invoice_date_key] → dim_date[date_key]` **active**, plus `due_date_key` and `payment_date_key`
  → `dim_date[date_key]` **inactive** (role-playing dates, activated per measure with `USERELATIONSHIP`).
- Mark `dim_date` as a date table (column `full_date`), otherwise time-intelligence functions misbehave.
- Put all measures in an empty `_Measures` table so they don't hang off a random table.

Do the cleaning first (dedupe invoice numbers, fix/flag the payment dates). These measures assume clean data.

---

### Revenue
```dax
Revenue =
SUM ( fact_invoice[net_amount] )
```
**Question:** How much did we invoice (net of VAT) in the selected period, customer, or cost centre?
Revenue is recognised on the invoice date because that is the active relationship.

### Revenue PY (helper)
```dax
Revenue PY =
CALCULATE ( [Revenue], SAMEPERIODLASTYEAR ( dim_date[full_date] ) )
```

### Revenue YoY %
```dax
Revenue YoY % =
VAR _cur = [Revenue]
VAR _py  = [Revenue PY]
RETURN
    IF ( NOT ISBLANK ( _py ), DIVIDE ( _cur - _py, _py ) )
```
**Question:** Are we growing compared with the same period last year?
`DIVIDE` returns BLANK when dividing by zero. The `ISBLANK` guard hides the measure for 2023, which has no prior year.
The current year is only partial, so compare YTD with YTD (`TOTALYTD`) or filter to complete months.

### Open Receivables (helper)
```dax
Open Receivables =
CALCULATE (
    SUM ( fact_invoice[gross_amount] ),
    fact_invoice[payment_status] = "OPEN",
    REMOVEFILTERS ( dim_date )          -- a balance as of today, not "invoiced in the selected period"
)
```

### DSO (Days Sales Outstanding)
```dax
DSO =
VAR _days      = 365
VAR _lastDate  = MAX ( dim_date[full_date] )
VAR _grossTTM  =
    CALCULATE (
        SUM ( fact_invoice[gross_amount] ),
        DATESINPERIOD ( dim_date[full_date], _lastDate, -_days, DAY )
    )
RETURN
    DIVIDE ( [Open Receivables], _grossTTM ) * _days
```
**Question:** On average, how many days of sales are tied up in unpaid invoices? (Lower means cash comes in faster.)
Receivables include VAT, so the revenue in the denominator is gross too. `Open Receivables` is the balance on the
dataset's as-of date, so read DSO at the latest date, or by customer or segment. Don't plot it as a historical trend.

### Overdue %
```dax
Overdue Amount =
VAR _asOf = CALCULATE ( MAX ( fact_invoice[invoice_date] ), REMOVEFILTERS () )
RETURN
    CALCULATE (
        SUM ( fact_invoice[gross_amount] ),
        fact_invoice[payment_status] = "OPEN",
        fact_invoice[due_date] < _asOf,
        REMOVEFILTERS ( dim_date )
    )

Overdue % =
DIVIDE ( [Overdue Amount], [Open Receivables] )
```
**Question:** What share of the money customers owe us is already past its due date? This tells you about credit risk
and how well dunning is working.

### Collection Rate
```dax
Collection Rate =
VAR _invoiced = [Revenue Gross]
VAR _collected =
    CALCULATE (
        SUM ( fact_invoice[gross_amount] ),
        fact_invoice[payment_status] = "PAID"
    )
RETURN
    DIVIDE ( _collected, _invoiced )

Revenue Gross =
SUM ( fact_invoice[gross_amount] )
```
**Question:** Of what we invoiced in the selected period, how much has already been paid?
This uses invoice-date cohorts, so recent months are naturally lower because those invoices are not due yet.

To see *cash received* in a period (by payment date) instead, use the inactive relationship:
```dax
Cash Collected =
CALCULATE (
    SUM ( fact_invoice[gross_amount] ),
    USERELATIONSHIP ( fact_invoice[payment_date_key], dim_date[date_key] )
)
```
