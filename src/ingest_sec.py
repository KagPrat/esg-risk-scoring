"""
ingest_sec.py — Pull governance metrics from SEC EDGAR.

Uses the free EDGAR Full-Text Search and Company Facts APIs:
  https://efts.sec.gov/LATEST/search-index?q=...     (full-text search)
  https://data.sec.gov/api/xbrl/companyfacts/{CIK}.json  (structured XBRL)

No API key required. SEC Fair Access Policy: max 10 requests/second.
We use a 0.15s delay between calls to stay well within limits.

Governance metrics extracted from XBRL company facts
-----------------------------------------------------
  EntityCommonStockSharesOutstanding — total shares outstanding
  AuditFees                          — annual audit fees paid
  DirectorsCompensation              — total board compensation
  NumberOfEmployees                  — headcount (proxy for social as well)
  RevenueFromContractWithCustomer    — revenue (for ratio calculations)
  IncomeTaxesPaid                    — taxes paid (governance transparency)

Board composition data (DEF 14A proxy statements) requires full-text parsing
and is included as a stub with clear TODO notes for a Phase 2 NLP pass.
"""

import requests
import pandas as pd
import time
import logging
from db import get_connection, init_db

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

EDGAR_BASE      = "https://data.sec.gov"
EDGAR_SEARCH    = "https://efts.sec.gov/LATEST/search-index"
USER_AGENT      = "ESG-Risk-Scoring-Portfolio pratik@example.com"  # SEC requires this header

# XBRL tags we want, mapped to human-readable metric names
XBRL_METRICS: dict[str, dict] = {
    "EntityCommonStockSharesOutstanding": {
        "metric_name": "shares_outstanding",
        "metric_unit": "shares",
        "pillar":      "G",
    },
    "AuditFees": {
        "metric_name": "audit_fees_usd",
        "metric_unit": "USD",
        "pillar":      "G",
    },
    "DirectorsCompensation": {
        "metric_name": "board_compensation_usd",
        "metric_unit": "USD",
        "pillar":      "G",
    },
    "NumberOfEmployees": {
        "metric_name": "num_employees",
        "metric_unit": "count",
        "pillar":      "S",
    },
    "Revenues": {
        "metric_name": "revenue_usd",
        "metric_unit": "USD",
        "pillar":      "G",
    },
    "IncomeTaxesPaid": {
        "metric_name": "income_taxes_paid_usd",
        "metric_unit": "USD",
        "pillar":      "G",
    },
}

# Sample companies — mix of sectors for a diverse portfolio demo
# Format: (ticker, CIK, display_name)
# Find CIKs at: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=...
DEFAULT_COMPANIES = [
    ("AAPL",  "0000320193",  "Apple Inc."),
    ("MSFT",  "0000789019",  "Microsoft Corporation"),
    ("XOM",   "0000034088",  "Exxon Mobil Corporation"),
    ("JPM",   "0000019617",  "JPMorgan Chase & Co."),
    ("JNJ",   "0000200406",  "Johnson & Johnson"),
    ("TSLA",  "0001318605",  "Tesla Inc."),
    ("PG",    "0000080424",  "Procter & Gamble Co."),
    ("BA",    "0000012927",  "Boeing Co."),
    ("WMT",   "0000104169",  "Walmart Inc."),
    ("CVX",   "0000093410",  "Chevron Corporation"),
]


def _headers() -> dict:
    """SEC EDGAR requires a descriptive User-Agent header."""
    return {"User-Agent": USER_AGENT, "Accept": "application/json"}


def fetch_company_facts(cik: str) -> dict | None:
    """
    Fetch all XBRL company facts for a given CIK.
    Returns the raw JSON dict, or None on failure.
    """
    # CIK must be zero-padded to 10 digits
    cik_padded = cik.lstrip("0").zfill(10)
    url = f"{EDGAR_BASE}/api/xbrl/companyfacts/CIK{cik_padded}.json"
    try:
        resp = requests.get(url, headers=_headers(), timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error(f"Failed to fetch facts for CIK {cik}: {e}")
        return None


def extract_metrics(
    facts_json: dict,
    ticker: str,
    company_name: str,
    cik: str,
    target_years: list[int],
) -> list[dict]:
    """
    Walk the XBRL facts JSON and extract our target metrics for each fiscal year.

    EDGAR facts structure:
      facts → { "us-gaap": { TAG: { "units": { "USD": [ {...} ] } } } }
    """
    rows = []
    us_gaap = facts_json.get("facts", {}).get("us-gaap", {})
    dei     = facts_json.get("facts", {}).get("dei", {})   # EntityCommonStock lives here

    for xbrl_tag, config in XBRL_METRICS.items():
        # EntityCommonStockSharesOutstanding is in dei namespace, rest in us-gaap
        namespace = dei if xbrl_tag == "EntityCommonStockSharesOutstanding" else us_gaap
        tag_data  = namespace.get(xbrl_tag, {})
        units     = tag_data.get("units", {})

        # Pick the right unit key (USD for money, shares/pure for counts)
        unit_key  = config["metric_unit"] if config["metric_unit"] in units else (
            next(iter(units), None)
        )
        if unit_key is None:
            continue

        filings = units.get(unit_key, [])

        # Keep only annual 10-K filings (form = "10-K") for clean year-over-year data
        annual = [
            f for f in filings
            if f.get("form") in ("10-K", "10-K/A")
            and f.get("end", "")[:4].isdigit()
        ]

        # For each target year, take the most recent filing whose end date falls in that year
        for year in target_years:
            year_filings = [f for f in annual if f["end"].startswith(str(year))]
            if not year_filings:
                continue
            # Take the latest one if there are amendments (10-K/A)
            best = sorted(year_filings, key=lambda f: f.get("filed", ""))[-1]
            rows.append({
                "ticker":       ticker,
                "company_name": company_name,
                "cik":          cik,
                "form_type":    best.get("form", "10-K"),
                "filing_date":  best.get("filed", ""),
                "fiscal_year":  year,
                "metric_name":  config["metric_name"],
                "metric_value": best.get("val"),
                "metric_unit":  config["metric_unit"],
                "source_url":   f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik.zfill(10)}.json",
            })

    return rows


def ingest(
    companies: list[tuple] | None = None,
    target_years: list[int] | None = None,
) -> int:
    """
    Main entry point. Fetches XBRL facts for each company and upserts to SQLite.
    Returns total rows inserted/updated.
    """
    if companies is None:
        companies = DEFAULT_COMPANIES
    if target_years is None:
        target_years = list(range(2018, 2024))

    init_db()
    conn = get_connection()
    total_rows = 0
    errors = []

    log.info(f"Fetching SEC EDGAR facts for {len(companies)} companies "
             f"(years: {target_years[0]}–{target_years[-1]})")

    for ticker, cik, name in companies:
        log.info(f"  {ticker} ({name})")
        facts = fetch_company_facts(cik)
        time.sleep(0.15)  # SEC fair-use rate limit

        if facts is None:
            errors.append(ticker)
            continue

        records = extract_metrics(facts, ticker, name, cik, target_years)
        if not records:
            log.warning(f"    No target metrics found for {ticker}")
            continue

        rows_before = conn.execute("SELECT COUNT(*) FROM sec_filings").fetchone()[0]
        with conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO sec_filings
                  (ticker, company_name, cik, form_type, filing_date, fiscal_year,
                   metric_name, metric_value, metric_unit, source_url)
                VALUES
                  (:ticker, :company_name, :cik, :form_type, :filing_date, :fiscal_year,
                   :metric_name, :metric_value, :metric_unit, :source_url)
                """,
                records,
            )
        rows_after  = conn.execute("SELECT COUNT(*) FROM sec_filings").fetchone()[0]
        added       = rows_after - rows_before
        total_rows += added
        log.info(f"    +{added} rows ({len(records)} extracted, {added} new/updated)")

    # Log the run
    status = "success" if not errors else ("partial" if total_rows > 0 else "error")
    msg    = f"Errors on: {errors}" if errors else None
    with conn:
        conn.execute(
            "INSERT INTO ingestion_log (source, status, rows_added, message) VALUES (?,?,?,?)",
            ("SEC EDGAR", status, total_rows, msg),
        )

    conn.close()
    log.info(f"SEC EDGAR ingestion complete — {total_rows} rows written ({status})")
    return total_rows


# ---------------------------------------------------------------------------
# TODO (Phase 2 — NLP): Parse DEF 14A proxy statements for board composition.
#
# Board independence % and gender diversity are strong G-pillar signals but
# aren't available as structured XBRL tags. They live in plain-text proxy
# statements. The approach:
#   1. Use EDGAR full-text search to find DEF 14A filings per CIK/year.
#   2. Download the .htm filing document.
#   3. Use spaCy or a fine-tuned HuggingFace model to extract:
#         - total board seats
#         - independent director count
#         - female director count
#   4. Compute ratios and upsert into sec_filings with metric_name =
#      'board_independence_pct', 'board_gender_diversity_pct', etc.
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    ingest()