"""
ingest_worldbank.py — Pull ESG-relevant indicators from the World Bank API.

World Bank API docs: https://datahelpdesk.worldbank.org/knowledgebase/articles/889392
No API key required. Rate limit is generous (~1000 req/min).

ESG mapping
-----------
Environmental:
  EN.ATM.CO2E.PC   — CO2 emissions (metric tons per capita)
  EG.USE.PCAP.KG.OE — Energy use (kg of oil equivalent per capita)
  AG.LND.FRST.ZS   — Forest area (% of land area)
  ER.H2O.FWST.ZS   — Freshwater withdrawal (% of internal resources)

Social:
  SL.UEM.TOTL.ZS   — Unemployment (% of total labor force)
  SP.DYN.LE00.IN   — Life expectancy at birth (years)
  SE.ADT.LITR.ZS   — Literacy rate, adults (% ages 15+)
  SH.STA.MMRT      — Maternal mortality ratio (per 100k live births)

Governance:
  IC.REG.DURS      — Time to start a business (days)
  CC.EST            — Control of Corruption (World Governance Indicators)
  RL.EST            — Rule of Law (World Governance Indicators)
  GE.EST            — Government Effectiveness (World Governance Indicators)
"""

import requests
import pandas as pd
import time
import logging
from db import get_connection, init_db

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

BASE_URL = "https://api.worldbank.org/v2"

# Indicators to fetch, grouped by ESG pillar for documentation clarity
INDICATORS: dict[str, dict] = {
    # --- Environmental ---
    "EN.ATM.CO2E.PC":    {"name": "CO2 emissions (metric tons per capita)",           "pillar": "E"},
    "EG.USE.PCAP.KG.OE": {"name": "Energy use (kg oil eq. per capita)",               "pillar": "E"},
    "AG.LND.FRST.ZS":    {"name": "Forest area (% of land)",                          "pillar": "E"},
    "ER.H2O.FWST.ZS":    {"name": "Freshwater withdrawal (% of internal resources)",  "pillar": "E"},
    # --- Social ---
    "SL.UEM.TOTL.ZS":    {"name": "Unemployment rate (%)",                            "pillar": "S"},
    "SP.DYN.LE00.IN":    {"name": "Life expectancy at birth (years)",                 "pillar": "S"},
    "SE.ADT.LITR.ZS":    {"name": "Literacy rate, adults (%)",                        "pillar": "S"},
    "SH.STA.MMRT":       {"name": "Maternal mortality ratio (per 100k births)",       "pillar": "S"},
    # --- Governance ---
    "IC.REG.DURS":       {"name": "Days to start a business",                         "pillar": "G"},
    "CC.EST":            {"name": "Control of Corruption estimate",                   "pillar": "G"},
    "RL.EST":            {"name": "Rule of Law estimate",                             "pillar": "G"},
    "GE.EST":            {"name": "Government Effectiveness estimate",                "pillar": "G"},
}

# G20 country codes — a sensible default scope for a portfolio project
DEFAULT_COUNTRIES = [
    "ARG", "AUS", "BRA", "CAN", "CHN", "DEU", "FRA", "GBR", "IDN", "IND",
    "ITA", "JPN", "KOR", "MEX", "RUS", "SAU", "TUR", "USA", "ZAF", "EUU",
]


def fetch_indicator(
    indicator_code: str,
    countries: list[str],
    start_year: int = 2015,
    end_year: int = 2023,
) -> pd.DataFrame:
    """
    Fetch a single indicator for a list of countries over a year range.
    Returns a tidy DataFrame with columns:
      country_code, country_name, indicator_code, indicator_name, year, value
    """
    country_str = ";".join(countries)
    url = f"{BASE_URL}/country/{country_str}/indicator/{indicator_code}"
    params = {
        "date":      f"{start_year}:{end_year}",
        "format":    "json",
        "per_page":  500,   # max per page; enough for 20 countries × 9 years
        "mrv":       end_year - start_year + 1,
    }

    rows = []
    page = 1
    while True:
        params["page"] = page
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            log.error(f"Request failed for {indicator_code}: {e}")
            break

        data = resp.json()
        if not isinstance(data, list) or len(data) < 2:
            log.warning(f"Unexpected response for {indicator_code}: {data}")
            break

        meta, records = data[0], data[1]
        if not records:
            break

        for rec in records:
            if rec.get("value") is None:
                continue  # skip missing observations
            rows.append({
                "country_code":   rec["country"]["id"],
                "country_name":   rec["country"]["value"],
                "indicator_code": indicator_code,
                "indicator_name": INDICATORS[indicator_code]["name"],
                "year":           int(rec["date"]),
                "value":          float(rec["value"]),
            })

        # Paginate if needed
        total_pages = meta.get("pages", 1)
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.2)  # polite delay between pages

    return pd.DataFrame(rows)


def ingest(
    countries: list[str] | None = None,
    start_year: int = 2015,
    end_year: int = 2023,
) -> int:
    """
    Main entry point. Fetches all indicators for all countries and upserts
    into SQLite. Returns total rows inserted/updated.
    """
    if countries is None:
        countries = DEFAULT_COUNTRIES

    init_db()
    conn = get_connection()
    total_rows = 0
    errors = []

    log.info(f"Fetching {len(INDICATORS)} indicators for {len(countries)} countries "
             f"({start_year}–{end_year})")

    for code, meta in INDICATORS.items():
        log.info(f"  [{meta['pillar']}] {code} — {meta['name']}")
        df = fetch_indicator(code, countries, start_year, end_year)

        if df.empty:
            log.warning(f"    No data returned for {code}")
            errors.append(code)
            continue

        # Upsert: INSERT OR REPLACE handles re-runs gracefully
        rows_before = conn.execute("SELECT COUNT(*) FROM worldbank_indicators").fetchone()[0]
        with conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO worldbank_indicators
                  (country_code, country_name, indicator_code, indicator_name, year, value)
                VALUES
                  (:country_code, :country_name, :indicator_code, :indicator_name, :year, :value)
                """,
                df.to_dict("records"),
            )
        rows_after = conn.execute("SELECT COUNT(*) FROM worldbank_indicators").fetchone()[0]
        added = rows_after - rows_before
        total_rows += added
        log.info(f"    +{added} rows (total so far: {total_rows})")
        time.sleep(0.3)  # be gentle with the public API

    # Write run to ingestion log
    status = "success" if not errors else ("partial" if total_rows > 0 else "error")
    msg = f"Errors on: {errors}" if errors else None
    with conn:
        conn.execute(
            "INSERT INTO ingestion_log (source, status, rows_added, message) VALUES (?,?,?,?)",
            ("World Bank API", status, total_rows, msg),
        )

    conn.close()
    log.info(f"World Bank ingestion complete — {total_rows} rows written ({status})")
    return total_rows


if __name__ == "__main__":
    ingest()