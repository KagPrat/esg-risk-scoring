"""
tests/test_ingest.py — Unit tests for Stage 1 ingestion.

Run with:  python -m pytest tests/ -v
"""

import sys
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from db import get_connection, init_db, DB_PATH


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Redirect DB_PATH to a temp file for each test."""
    test_db = tmp_path / "test_esg.db"
    monkeypatch.setattr("db.DB_PATH", test_db)
    # Patch DB_PATH in each module that imports it
    for mod in ("ingest_worldbank", "ingest_sec"):
        try:
            import importlib
            m = importlib.import_module(mod)
            monkeypatch.setattr(m, "init_db", lambda: None)
        except ImportError:
            pass
    yield test_db


# ── DB schema tests ──────────────────────────────────────────────────────────

class TestDB:
    def test_init_creates_tables(self, tmp_db):
        import db as db_module
        db_module.DB_PATH = tmp_db
        db_module.init_db()
        conn = sqlite3.connect(tmp_db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "worldbank_indicators" in tables
        assert "sec_filings" in tables
        assert "ingestion_log" in tables
        conn.close()

    def test_idempotent_init(self, tmp_db):
        """Running init_db twice should not raise."""
        import db as db_module
        db_module.DB_PATH = tmp_db
        db_module.init_db()
        db_module.init_db()  # second call — no error

    def test_worldbank_upsert(self, tmp_db):
        import db as db_module
        db_module.DB_PATH = tmp_db
        db_module.init_db()
        conn = sqlite3.connect(tmp_db)
        conn.execute("""
            INSERT INTO worldbank_indicators
              (country_code, country_name, indicator_code, indicator_name, year, value)
            VALUES ('USA', 'United States', 'EN.ATM.CO2E.PC', 'CO2 emissions', 2022, 14.2)
        """)
        conn.commit()
        row = conn.execute(
            "SELECT value FROM worldbank_indicators WHERE country_code='USA' AND year=2022"
        ).fetchone()
        assert row[0] == pytest.approx(14.2)
        conn.close()


# ── World Bank parser tests ──────────────────────────────────────────────────

class TestWorldBankParser:
    """Test the response parsing logic without hitting the real API."""

    def _mock_response(self, records):
        """Build a fake World Bank API response list."""
        meta = {"pages": 1, "total": len(records)}
        return [meta, records]

    def test_parses_valid_records(self):
        from ingest_worldbank import fetch_indicator, INDICATORS
        sample_code = "EN.ATM.CO2E.PC"
        fake_records = [
            {"country": {"id": "USA", "value": "United States"}, "date": "2022", "value": 14.24},
            {"country": {"id": "DEU", "value": "Germany"},        "date": "2022", "value": 8.09},
        ]
        with patch("ingest_worldbank.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = self._mock_response(fake_records)
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            df = fetch_indicator(sample_code, ["USA", "DEU"], 2022, 2022)

        assert len(df) == 2
        assert set(df["country_code"]) == {"USA", "DEU"}
        assert df[df["country_code"] == "USA"]["value"].iloc[0] == pytest.approx(14.24)

    def test_skips_null_values(self):
        from ingest_worldbank import fetch_indicator
        fake_records = [
            {"country": {"id": "USA", "value": "United States"}, "date": "2022", "value": None},
            {"country": {"id": "DEU", "value": "Germany"},        "date": "2022", "value": 8.09},
        ]
        with patch("ingest_worldbank.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = [{"pages": 1}, fake_records]
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            df = fetch_indicator("EN.ATM.CO2E.PC", ["USA", "DEU"], 2022, 2022)

        assert len(df) == 1
        assert df.iloc[0]["country_code"] == "DEU"

    def test_handles_api_error_gracefully(self):
        from ingest_worldbank import fetch_indicator
        with patch("ingest_worldbank.requests.get") as mock_get:
            mock_get.side_effect = Exception("Connection timeout")
            df = fetch_indicator("EN.ATM.CO2E.PC", ["USA"], 2022, 2022)

        assert df.empty


# ── SEC EDGAR parser tests ───────────────────────────────────────────────────

class TestSECParser:
    def _fake_facts(self):
        """Minimal EDGAR company facts structure."""
        return {
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "USD": [
                                {"end": "2022-09-30", "val": 394328000000, "form": "10-K", "filed": "2022-10-28"},
                                {"end": "2021-09-30", "val": 365817000000, "form": "10-K", "filed": "2021-10-29"},
                            ]
                        }
                    },
                    "NumberOfEmployees": {
                        "units": {
                            "pure": [
                                {"end": "2022-09-30", "val": 164000, "form": "10-K", "filed": "2022-10-28"},
                            ]
                        }
                    },
                },
                "dei": {
                    "EntityCommonStockSharesOutstanding": {
                        "units": {
                            "shares": [
                                {"end": "2022-09-30", "val": 15943425000, "form": "10-K", "filed": "2022-10-28"},
                            ]
                        }
                    }
                }
            }
        }

    def test_extracts_revenue(self):
        from ingest_sec import extract_metrics
        rows = extract_metrics(self._fake_facts(), "AAPL", "Apple Inc.", "320193", [2022])
        revenue = [r for r in rows if r["metric_name"] == "revenue_usd"]
        assert len(revenue) == 1
        assert revenue[0]["metric_value"] == 394328000000

    def test_extracts_employees(self):
        from ingest_sec import extract_metrics
        rows = extract_metrics(self._fake_facts(), "AAPL", "Apple Inc.", "320193", [2022])
        employees = [r for r in rows if r["metric_name"] == "num_employees"]
        assert len(employees) == 1
        assert employees[0]["metric_value"] == 164000

    def test_skips_missing_years(self):
        from ingest_sec import extract_metrics
        rows = extract_metrics(self._fake_facts(), "AAPL", "Apple Inc.", "320193", [2019])
        # 2019 data doesn't exist in our fake facts
        assert all(r["fiscal_year"] != 2019 for r in rows)

    def test_handles_fetch_failure(self):
        from ingest_sec import fetch_company_facts
        with patch("ingest_sec.requests.get") as mock_get:
            mock_get.side_effect = Exception("Timeout")
            result = fetch_company_facts("0000320193")
        assert result is None