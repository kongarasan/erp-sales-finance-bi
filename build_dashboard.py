"""
Generates the Power BI dashboard (PBIR report files + small model tweaks) for
ERP_Reporting.pbip. Close Power BI before running, then reopen ERP_Reporting.pbip.

    .\.venv\Scripts\python build_dashboard.py
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "ERP_Reporting.Report"
MODEL = ROOT / "ERP_Reporting.SemanticModel" / "definition" / "tables"
FACT, DATE, CUST, CC = "public fact_invoice", "public dim_date", "public dim_customer", "public dim_costcentre"
V_AGE, V_DSO, V_TOP = "public vw_overdue_ageing", "public vw_dso_by_customer", "public vw_top20_customers"

VC_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.0.0/schema.json"
PAGE_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json"

# --------------------------------------------------------------------------- model tweaks (TMDL)
F, D = "'public fact_invoice'", "'public dim_date'"
MEASURES = {   # single-line DAX so the TMDL stays simple
    "Revenue": f"SUM ( {F}[net_amount] )",
    "Revenue Gross": f"SUM ( {F}[gross_amount] )",
    "Revenue PY": f"CALCULATE ( [Revenue], SAMEPERIODLASTYEAR ( {D}[full_date] ) )",
    # blank when there is no current OR prior-year revenue (avoids -100% for future years)
    "Revenue YoY %": "VAR _cur = [Revenue] VAR _py = [Revenue PY] RETURN IF ( NOT ISBLANK ( _cur ) && NOT ISBLANK ( _py ), DIVIDE ( _cur - _py, _py ) )",
    "Open Receivables": f"CALCULATE ( SUM ( {F}[gross_amount] ), {F}[payment_status] = \"OPEN\", REMOVEFILTERS ( {D} ) )",
    # anchor on the last INVOICE date in context (dim_date runs to 2027, which has no sales)
    "Revenue YTD YoY %": f"VAR _end = MAX ( {F}[invoice_date] ) VAR _start = DATE ( YEAR ( _end ), 1, 1 ) VAR _cur = CALCULATE ( [Revenue], DATESBETWEEN ( {D}[full_date], _start, _end ) ) VAR _py = CALCULATE ( [Revenue], DATESBETWEEN ( {D}[full_date], EDATE ( _start, -12 ), EDATE ( _end, -12 ) ) ) RETURN DIVIDE ( _cur - _py, _py )",
    "DSO": f"VAR _lastDate = MAX ( {F}[invoice_date] ) VAR _grossTTM = CALCULATE ( SUM ( {F}[gross_amount] ), DATESINPERIOD ( {D}[full_date], _lastDate, -365, DAY ) ) RETURN DIVIDE ( [Open Receivables], _grossTTM ) * 365",
    "Overdue Amount": f"VAR _asOf = CALCULATE ( MAX ( {F}[invoice_date] ), REMOVEFILTERS () ) RETURN CALCULATE ( SUM ( {F}[gross_amount] ), {F}[payment_status] = \"OPEN\", {F}[due_date] < _asOf, REMOVEFILTERS ( {D} ) )",
    "Overdue %": "DIVIDE ( [Overdue Amount], [Open Receivables] )",
    "Collection Rate": f"DIVIDE ( CALCULATE ( SUM ( {F}[gross_amount] ), {F}[payment_status] = \"PAID\" ), [Revenue Gross] )",
    "Cash Collected": f"CALCULATE ( SUM ( {F}[gross_amount] ), USERELATIONSHIP ( {F}[payment_date_key], {D}[date_key] ) )",
    "Invoice Count": f"COUNTROWS ( {F} )",
    "Active Customers": f"DISTINCTCOUNT ( {F}[customer_key] )",
    "Avg Invoice Value": "DIVIDE ( [Revenue], [Invoice Count] )",
}


def _q(name: str) -> str:
    return f"'{name}'" if re.search(r"[^A-Za-z0-9_]", name) else name
MEASURE_FORMATS = {
    "Revenue": "€#,0", "Revenue Gross": "€#,0", "Revenue PY": "€#,0",
    "Open Receivables": "€#,0", "Overdue Amount": "€#,0", "Cash Collected": "€#,0",
    "Avg Invoice Value": "€#,0", "Invoice Count": "#,0", "Active Customers": "#,0",
    "Revenue YoY %": "+0.0%;-0.0%;0.0%", "Revenue YTD YoY %": "+0.0%;-0.0%;0.0%", "Overdue %": "0.0%", "Collection Rate": "0.0%",
    "DSO": "0.0 \"days\"",
}
# (table, column) -> extra properties
COLUMN_PROPS = {
    (DATE, "month_name"): {"sortByColumn": "month"},
    (DATE, "year"): {"summarizeBy": "none"},
    (DATE, "month"): {"summarizeBy": "none"},
    ("public vw_overdue_ageing", "ageing_bucket"): {"sortByColumn": "bucket_order"},
    ("public vw_overdue_ageing", "overdue_amount"): {"formatString": "€#,0"},
    ("public vw_dso_by_customer", "open_receivables"): {"formatString": "€#,0"},
    ("public vw_dso_by_customer", "gross_revenue_365d"): {"formatString": "€#,0"},
    ("public vw_dso_by_customer", "dso_days"): {"formatString": "0.0", "summarizeBy": "none"},
    ("public vw_dso_by_customer", "avg_days_to_pay"): {"formatString": "0.0", "summarizeBy": "none"},
    ("public vw_dso_by_customer", "avg_days_beyond_terms"): {"formatString": "+0.0;-0.0;0.0", "summarizeBy": "none"},
    ("public vw_top20_customers", "net_revenue"): {"formatString": "€#,0"},
    ("public vw_top20_customers", "revenue_rank"): {"summarizeBy": "none"},
    ("public vw_top20_customers", "revenue_share_pct"): {"formatString": "0.00\"%\"", "summarizeBy": "none"},
    ("public vw_top20_customers", "cumulative_share_pct"): {"formatString": "0.0\"%\"", "summarizeBy": "none"},
}
HIDE_KEYS = {
    FACT: ["invoice_key", "customer_key", "costcentre_key", "invoice_date_key", "due_date_key", "payment_date_key"],
    CUST: ["customer_key"], CC: ["costcentre_key"], DATE: ["date_key"],
}


def _set_prop(block: str, prop: str, value: str | None) -> str:
    """Set/replace a property line (2 tabs indent) inside a column/measure block."""
    line = f"\t\t{prop}" + (f": {value}" if value is not None else "")
    pat = re.compile(rf"^\t\t{prop}(:.*)?$", re.M)
    if pat.search(block):
        return pat.sub(line.replace("\\", "\\\\"), block, count=1)
    head, _, rest = block.partition("\n")
    return f"{head}\n{line}\n{rest}"


def _edit_blocks(text: str, kind: str, name: str, fn) -> str:
    q = f"'{name}'" if re.search(r"[^A-Za-z0-9_]", name) else name
    pat = re.compile(rf"(^\t{kind} {re.escape(q)}(?=[ =\n]).*?)(?=^\t(?:column|measure|partition|annotation|hierarchy) |\Z)", re.M | re.S)
    m = pat.search(text)
    if not m:
        print(f"  ! {kind} {name} not found")
        return text
    return text[:m.start()] + fn(m.group(1)) + text[m.end():]


def tweak_model() -> None:
    for path in MODEL.glob("public *.tmdl"):
        table = path.stem
        text = path.read_text(encoding="utf-8")
        if table == FACT:
            # Rewrite the whole measure section (idempotent): everything between the
            # table header and the first column.
            head, sep, rest = text.partition("\n\tcolumn ")
            head_lines = head.split("\n")[:2]          # "table ..." + lineageTag
            block = "\n".join(f"\tmeasure {_q(n)} = {e}\n\t\tformatString: {MEASURE_FORMATS[n]}\n"
                              for n, e in MEASURES.items())
            text = "\n".join(head_lines) + "\n\n" + block + sep + rest
        for (t, col), props in COLUMN_PROPS.items():
            if t == table:
                def g(b, props=props):
                    for k, v in props.items():
                        b = _set_prop(b, k, v)
                    if "summarizeBy" in props:
                        b = re.sub(r"annotation SummarizationSetBy = Automatic", "annotation SummarizationSetBy = User", b)
                    return b
                text = _edit_blocks(text, "column", col, g)
        for col in HIDE_KEYS.get(table, []):
            text = _edit_blocks(text, "column", col, lambda b: _set_prop(b, "isHidden", None))
        path.write_text(text, encoding="utf-8")
    print("model: measures, formats, sort-by and hidden keys updated")


# --------------------------------------------------------------------------- report helpers
def lit(v):
    if isinstance(v, bool):
        s = "true" if v else "false"
    elif isinstance(v, (int, float)):
        s = f"{v}D"
    else:
        s = "'" + str(v).replace("'", "''") + "'"
    return {"expr": {"Literal": {"Value": s}}}


def col(entity, prop):
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def mea(prop, entity=FACT):
    return {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def agg(entity, prop, fn=0):  # 0 = Sum
    return {"Aggregation": {"Expression": col(entity, prop), "Function": fn}}


def _ref(field):
    if "Aggregation" in field:
        c = field["Aggregation"]["Expression"]["Column"]
        return f"Sum({c['Expression']['SourceRef']['Entity']}.{c['Property']})", c["Property"]
    k = "Measure" if "Measure" in field else "Column"
    return f"{field[k]['Expression']['SourceRef']['Entity']}.{field[k]['Property']}", field[k]["Property"]


def proj(field, display=None):
    qref, native = _ref(field)
    p = {"field": field, "queryRef": qref, "nativeQueryRef": native}
    if display:
        p["displayName"] = display
    return p


class Page:
    def __init__(self, name, display):
        self.name, self.display, self.visuals, self.z = name, display, [], 0

    def add(self, vid, vtype, x, y, w, h, roles=None, title=None, objects=None, sort=None, container=None):
        self.z += 1000
        visual = {"visualType": vtype}
        if roles:
            visual["query"] = {"queryState": {r: {"projections": [proj(*f) if isinstance(f, tuple) else proj(f) for f in fs]}
                                              for r, fs in roles.items()}}
            if sort:
                field, direction = sort
                visual["query"]["sortDefinition"] = {"sort": [{"field": field, "direction": direction}], "isDefaultSort": True}
        if objects:
            visual["objects"] = objects
        vco = dict(container or {})
        if title:
            vco["title"] = [{"properties": {"show": lit(True), "text": lit(title)}}]
        if vco:
            visual["visualContainerObjects"] = vco
        self.visuals.append({
            "$schema": VC_SCHEMA, "name": vid,
            "position": {"x": x, "y": y, "z": self.z, "height": h, "width": w, "tabOrder": self.z},
            "visual": visual,
        })

    def write(self):
        d = REPORT / "definition" / "pages" / self.name
        (d / "visuals").mkdir(parents=True, exist_ok=True)
        (d / "page.json").write_text(json.dumps({
            "$schema": PAGE_SCHEMA, "name": self.name, "displayName": self.display,
            "displayOption": "FitToPage", "height": 1080, "width": 1920}, indent=2), encoding="utf-8")
        for v in self.visuals:
            vd = d / "visuals" / v["name"]
            vd.mkdir(exist_ok=True)
            (vd / "visual.json").write_text(json.dumps(v, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- shared building blocks
M = 32          # outer margin
G = 24          # gutter
W = 1920 - 2 * M


def header(p: Page, subtitle: str):
    p.add("header", "textbox", M, 20, 1150, 84, objects={"general": [{"properties": {"paragraphs": [
        {"textRuns": [{"value": "ERP Sales & Finance  ", "textStyle": {"fontFamily": "Segoe UI Semibold", "fontSize": "22pt", "color": "#0F172A"}},
                      {"value": f"·  {p.display}", "textStyle": {"fontFamily": "Segoe UI", "fontSize": "22pt", "color": "#2F5BEA"}}]},
        {"textRuns": [{"value": subtitle, "textStyle": {"fontFamily": "Segoe UI", "fontSize": "11pt", "color": "#64748B"}}]},
    ]}}]})
    sl = {"data": [{"properties": {"mode": lit("Dropdown")}}],
          "header": [{"properties": {"fontColor": {"solid": {"color": "#64748B"}}, "textSize": lit(9)}}]}
    p.add("slicerYear", "slicer", 1236, 24, 200, 76, roles={"Values": [(col(DATE, "year"), "Year")]}, objects=sl)
    p.add("slicerSegment", "slicer", 1460, 24, 200, 76, roles={"Values": [(col(CUST, "segment"), "Segment")]}, objects=sl)
    p.add("slicerRegion", "slicer", 1684, 24, 204, 76, roles={"Values": [(col(CUST, "sales_region"), "Region")]}, objects=sl)


def kpis(p: Page, items):
    n = len(items)
    w = (W - (n - 1) * G) / n
    for i, (measure, label) in enumerate(items):
        p.add(f"kpi{i}", "card", round(M + i * (w + G)), 124, round(w), 128,
              roles={"Values": [mea(measure)]}, title=label,
              objects={"categoryLabels": [{"properties": {"show": lit(False)}}]})


def build_pages():
    pages_dir = REPORT / "definition" / "pages"
    for child in pages_dir.iterdir():
        if child.is_dir():
            shutil.rmtree(child)

    # ---- Page 1: Sales overview -------------------------------------------------------
    p1 = Page("salesOverview", "Sales Overview")
    header(p1, "Net revenue by invoice date · 2023 – Nov 2026 · synthetic German B2B data")
    kpis(p1, [("Revenue", "Revenue (net)"), ("Revenue YTD YoY %", "YTD vs. same period last year"),
              ("Invoice Count", "Invoices"), ("Active Customers", "Active customers"),
              ("Avg Invoice Value", "Avg. invoice value")])
    y2, h2 = 276, 390
    p1.add("revTrend", "lineChart", M, y2, 1180, h2,
           roles={"Category": [col(DATE, "year_month")], "Y": [mea("Revenue")]},
           title="Monthly revenue trend", sort=(col(DATE, "year_month"), "Ascending"))
    p1.add("revSegment", "donutChart", M + 1180 + G, y2, W - 1180 - G, h2,
           roles={"Category": [col(CUST, "segment")], "Y": [mea("Revenue")]},
           title="Revenue by customer segment")
    y3 = y2 + h2 + G
    h3 = 1080 - M - y3
    half = (W - G) // 2
    p1.add("revDept", "clusteredBarChart", M, y3, half, h3,
           roles={"Category": [col(CC, "department")], "Y": [mea("Revenue")]},
           title="Revenue by department", sort=(mea("Revenue"), "Descending"))
    p1.add("revIndustry", "clusteredBarChart", M + half + G, y3, W - half - G, h3,
           roles={"Category": [col(CUST, "industry")], "Y": [mea("Revenue")]},
           title="Revenue by industry", sort=(mea("Revenue"), "Descending"))

    # ---- Page 2: Receivables ---------------------------------------------------------
    p2 = Page("receivables", "Receivables")
    header(p2, "Open items as of 26 Nov 2026 · DSO = open AR ÷ gross revenue (365d) × 365")
    kpis(p2, [("Open Receivables", "Open receivables"), ("Overdue Amount", "Overdue amount"),
              ("Overdue %", "Overdue share"), ("DSO", "DSO"), ("Collection Rate", "Collection rate")])
    p2.add("ageing", "clusteredColumnChart", M, y2, half, h2,
           roles={"Category": [col(V_AGE, "ageing_bucket")], "Y": [(agg(V_AGE, "overdue_amount"), "Overdue amount")]},
           title="Overdue receivables by ageing bucket (days past due)",
           sort=(col(V_AGE, "ageing_bucket"), "Ascending"))
    p2.add("cashVsBilled", "lineChart", M + half + G, y2, W - half - G, h2,
           roles={"Category": [col(DATE, "year_month")], "Y": [mea("Revenue Gross"), mea("Cash Collected")]},
           title="Invoiced (gross) vs. cash collected", sort=(col(DATE, "year_month"), "Ascending"))
    p2.add("dsoTable", "tableEx", M, y3, W, h3,
           roles={"Values": [(col(V_DSO, "customer_name"), "Customer"), (col(V_DSO, "segment"), "Segment"),
                             (col(V_DSO, "payment_terms_days"), "Terms (days)"),
                             (col(V_DSO, "open_receivables"), "Open AR"), (col(V_DSO, "dso_days"), "DSO (days)"),
                             (col(V_DSO, "avg_days_to_pay"), "Avg days to pay"),
                             (col(V_DSO, "avg_days_beyond_terms"), "Days beyond terms")]},
           title="Customers by open receivables", sort=(col(V_DSO, "open_receivables"), "Descending"))

    # ---- Page 3: Customers -----------------------------------------------------------
    p3 = Page("customers", "Customers")
    header(p3, "Revenue concentration · top-20 accounts carry ~40% of revenue")
    left = 1120
    p3.add("top20", "tableEx", M, 124, left, 1080 - M - 124,
           roles={"Values": [(col(V_TOP, "revenue_rank"), "#"), (col(V_TOP, "customer_name"), "Customer"),
                             (col(V_TOP, "segment"), "Segment"), (col(V_TOP, "industry"), "Industry"),
                             (col(V_TOP, "net_revenue"), "Net revenue"),
                             (col(V_TOP, "revenue_share_pct"), "Share"),
                             (col(V_TOP, "cumulative_share_pct"), "Cumulative")]},
           title="Top-20 customers (all time)", sort=(col(V_TOP, "revenue_rank"), "Ascending"))
    rx, rw = M + left + G, W - left - G
    rh = (1080 - M - 124 - G) // 2
    p3.add("revRegion", "clusteredColumnChart", rx, 124, rw, rh,
           roles={"Category": [col(CUST, "sales_region")], "Y": [mea("Revenue")]},
           title="Revenue by sales region", sort=(mea("Revenue"), "Descending"))
    p3.add("revState", "clusteredBarChart", rx, 124 + rh + G, rw, 1080 - M - 124 - rh - G,
           roles={"Category": [col(CUST, "federal_state")], "Y": [mea("Revenue")]},
           title="Revenue by federal state", sort=(mea("Revenue"), "Descending"))

    pages = [p1, p2, p3]
    for p in pages:
        p.write()
    (pages_dir / "pages.json").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.1.0/schema.json",
        "pageOrder": [p.name for p in pages], "activePageName": pages[0].name}, indent=2), encoding="utf-8")
    print(f"report: {len(pages)} pages, {sum(len(p.visuals) for p in pages)} visuals written")


# --------------------------------------------------------------------------- theme
THEME = {
    "name": "ERP Clean",
    "dataColors": ["#2F5BEA", "#14B8A6", "#F59E0B", "#8B5CF6", "#EF4444", "#64748B", "#0EA5E9", "#22C55E"],
    "background": "#FFFFFF", "foreground": "#0F172A", "tableAccent": "#2F5BEA",
    "good": "#16A34A", "neutral": "#F59E0B", "bad": "#DC2626",
    "textClasses": {
        "callout": {"fontSize": 30, "fontFace": "Segoe UI Semibold", "color": "#0F172A"},
        "title": {"fontSize": 12, "fontFace": "Segoe UI Semibold", "color": "#334155"},
        "header": {"fontSize": 12, "fontFace": "Segoe UI Semibold", "color": "#0F172A"},
        "label": {"fontSize": 10, "fontFace": "Segoe UI", "color": "#64748B"},
    },
    "visualStyles": {
        "*": {"*": {
            "background": [{"show": True, "color": {"solid": {"color": "#FFFFFF"}}, "transparency": 0}],
            "border": [{"show": True, "color": {"solid": {"color": "#E5E9F0"}}, "radius": 12}],
            "dropShadow": [{"show": True, "color": {"solid": {"color": "#0F172A"}}, "transparency": 92,
                            "position": "Outer", "preset": "Custom", "shadowBlur": 14, "shadowDistance": 2, "angle": 90}],
            "title": [{"show": True, "fontColor": {"solid": {"color": "#334155"}}, "fontSize": 12,
                       "fontFamily": "Segoe UI Semibold", "alignment": "left"}],
            "subTitle": [{"show": False}],
            "padding": [{"top": 12, "bottom": 12, "left": 16, "right": 16}],
            "valueAxis": [{"gridlineColor": {"solid": {"color": "#EEF1F6"}}, "gridlineStyle": "solid"}],
            "categoryAxis": [{"showAxisTitle": False}],
        }},
        "page": {"*": {
            "background": [{"color": {"solid": {"color": "#F4F6FA"}}, "transparency": 0}],
            "outspace": [{"color": {"solid": {"color": "#F4F6FA"}}, "transparency": 0}],
        }},
        "textbox": {"*": {
            "background": [{"show": False}], "border": [{"show": False}], "dropShadow": [{"show": False}],
            "padding": [{"top": 0, "bottom": 0, "left": 0, "right": 0}],
        }},
        "card": {"*": {
            "labels": [{"color": {"solid": {"color": "#0F172A"}}, "fontSize": 28, "fontFamily": "Segoe UI Semibold"}],
            "categoryLabels": [{"show": False}],
            "title": [{"fontColor": {"solid": {"color": "#64748B"}}, "fontSize": 11, "fontFamily": "Segoe UI"}],
        }},
        "lineChart": {"*": {"lineStyles": [{"strokeWidth": 3}], "legend": [{"show": True, "position": "TopRight"}]}},
        "tableEx": {"*": {
            "grid": [{"gridHorizontal": True, "gridHorizontalColor": {"solid": {"color": "#EEF1F6"}},
                      "gridVertical": False, "rowPadding": 6, "textSize": 10}],
            "columnHeaders": [{"fontColor": {"solid": {"color": "#64748B"}}, "backColor": {"solid": {"color": "#FFFFFF"}},
                               "fontFamily": "Segoe UI Semibold", "fontSize": 10}],
            "values": [{"backColorPrimary": {"solid": {"color": "#FFFFFF"}},
                        "backColorSecondary": {"solid": {"color": "#F8FAFC"}}}],
        }},
    },
}


def write_theme():
    rr = REPORT / "StaticResources" / "RegisteredResources"
    rr.mkdir(parents=True, exist_ok=True)
    (rr / "ERPClean.json").write_text(json.dumps(THEME, indent=2), encoding="utf-8")
    rpath = REPORT / "definition" / "report.json"
    report = json.loads(rpath.read_text(encoding="utf-8"))
    base = report["themeCollection"]["baseTheme"]
    report["themeCollection"]["customTheme"] = {
        "name": "ERPClean.json", "reportVersionAtImport": base["reportVersionAtImport"], "type": "RegisteredResources"}
    pkgs = [p for p in report["resourcePackages"] if p["name"] != "RegisteredResources"]
    pkgs.append({"name": "RegisteredResources", "type": "RegisteredResources",
                 "items": [{"name": "ERPClean.json", "path": "ERPClean.json", "type": "CustomTheme"}]})
    report["resourcePackages"] = pkgs
    rpath.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("theme: ERP Clean registered")


if __name__ == "__main__":
    tweak_model()
    write_theme()
    build_pages()
