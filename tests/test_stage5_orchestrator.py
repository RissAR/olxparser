"""
Unit tests for Stage 5: Multi-source Orchestrator and Web UI Integration.
"""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import app
from tests.test_app import ASGITestClient
import database
from orchestrator import (
    SUPPORTED_SOURCES,
    MultiSourceOrchestrator,
    get_orchestrator_instance,
)


class TestStage5Orchestrator(unittest.IsolatedAsyncioTestCase):
    """Test suite for MultiSourceOrchestrator lifecycle, source isolation, and API integration."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()
        database.init_db(self.db_path)

        self.orchestrator = MultiSourceOrchestrator(db_path=self.db_path)
        self.client = ASGITestClient(app)

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    @classmethod
    def tearDownClass(cls):
        pass

    async def test_orchestrator_initialization_and_status(self):
        """Verifies default state and presence of all 3 source scrapers."""
        self.assertFalse(self.orchestrator.is_running)
        self.assertEqual(self.orchestrator.status, "idle")
        self.assertIsNone(self.orchestrator.active_source)

        st = self.orchestrator.get_status()
        self.assertIn("sources_detail", st)
        self.assertIn("olx", st["sources_detail"])
        self.assertIn("rieltor", st["sources_detail"])
        self.assertIn("lun", st["sources_detail"])
        self.assertEqual(st["processed_count"], 0)

    async def test_source_normalization(self):
        """Verifies source normalization logic."""
        # Empty or 'all' -> all 3
        # We test normalization through run with mocked scrapers
        self.orchestrator.olx_scraper.run = AsyncMock(return_value={"total_found": 1, "processed_count": 1, "processed_residential": 1, "processed_commercial": 0, "errors_count": 0})
        self.orchestrator.rieltor_scraper.run = AsyncMock(return_value={"total_found": 2, "processed_count": 2, "processed_residential": 1, "processed_commercial": 1, "errors_count": 0})
        self.orchestrator.lun_scraper.run = AsyncMock(return_value={"total_found": 3, "processed_count": 3, "processed_residential": 2, "processed_commercial": 1, "errors_count": 0})

        # Test single source
        res = await self.orchestrator.run(sources=["lun"], category="residential")
        self.assertEqual(res["sources"], ["lun"])
        self.orchestrator.lun_scraper.run.assert_called_once()
        self.orchestrator.olx_scraper.run.assert_not_called()
        self.orchestrator.rieltor_scraper.run.assert_not_called()

    async def test_sequential_execution_and_metrics_aggregation(self):
        """Verifies that all 3 scrapers run sequentially and metrics aggregate correctly."""
        self.orchestrator.olx_scraper.run = AsyncMock(return_value={
            "total_found": 10,
            "processed_count": 10,
            "processed_residential": 6,
            "processed_commercial": 4,
            "errors_count": 0,
        })
        self.orchestrator.rieltor_scraper.run = AsyncMock(return_value={
            "total_found": 15,
            "processed_count": 15,
            "processed_residential": 10,
            "processed_commercial": 5,
            "errors_count": 0,
        })
        self.orchestrator.lun_scraper.run = AsyncMock(return_value={
            "total_found": 20,
            "processed_count": 20,
            "processed_residential": 12,
            "processed_commercial": 8,
            "errors_count": 1,
        })

        progress_calls = []
        def on_prog(st):
            progress_calls.append(st["current_slice"])

        final_st = await self.orchestrator.run(
            sources=["olx", "rieltor", "lun"],
            category="all",
            on_progress=on_prog,
        )

        self.assertEqual(final_st["status"], "completed")
        self.assertEqual(final_st["total_found"], 45)
        self.assertEqual(final_st["processed_count"], 45)
        self.assertEqual(final_st["processed_residential"], 28)
        self.assertEqual(final_st["processed_commercial"], 17)
        self.assertEqual(final_st["errors_count"], 1)

        # Verify calls
        self.orchestrator.olx_scraper.run.assert_called_once()
        self.orchestrator.rieltor_scraper.run.assert_called_once()
        self.orchestrator.lun_scraper.run.assert_called_once()

    async def test_orchestrator_stop_propagation(self):
        """Verifies that stop() halts execution and transitions state to 'stopped'."""
        async def mock_slow_run(*args, **kwargs):
            self.orchestrator.stop()
            return {"total_found": 5, "processed_count": 5, "processed_residential": 5, "processed_commercial": 0, "errors_count": 0}

        self.orchestrator.olx_scraper.run = AsyncMock(side_effect=mock_slow_run)
        self.orchestrator.rieltor_scraper.run = AsyncMock()
        self.orchestrator.lun_scraper.run = AsyncMock()

        res = await self.orchestrator.run(sources=["olx", "rieltor", "lun"])
        self.assertEqual(res["status"], "stopped")
        # Second and third scrapers should not run after stop
        self.orchestrator.rieltor_scraper.run.assert_not_called()
        self.orchestrator.lun_scraper.run.assert_not_called()

    async def test_error_isolation_between_sources(self):
        """Verifies that failure in one source does not crash the entire multi-source workflow."""
        self.orchestrator.olx_scraper.run = AsyncMock(side_effect=RuntimeError("OLX network timeout"))
        self.orchestrator.rieltor_scraper.run = AsyncMock(return_value={
            "total_found": 8,
            "processed_count": 8,
            "processed_residential": 8,
            "processed_commercial": 0,
            "errors_count": 0,
        })
        self.orchestrator.lun_scraper.run = AsyncMock(return_value={
            "total_found": 12,
            "processed_count": 12,
            "processed_residential": 12,
            "processed_commercial": 0,
            "errors_count": 0,
        })

        res = await self.orchestrator.run(sources=["olx", "rieltor", "lun"])
        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["processed_count"], 20)
        self.assertGreaterEqual(res["errors_count"], 1)
        self.assertEqual(self.orchestrator.source_statuses["olx"]["status"], "failed")
        self.assertEqual(self.orchestrator.source_statuses["rieltor"]["processed_count"], 8)
        self.assertEqual(self.orchestrator.source_statuses["lun"]["processed_count"], 12)

    async def test_api_scrape_start_with_sources(self):
        """Tests POST /api/scrape/start accepts sources parameter."""
        orchestrator = get_orchestrator_instance()
        orchestrator.is_running = False

        with patch("app.run_scraping_workflow", new=AsyncMock()) as mock_wf:
            resp = await self.client.post(
                "/api/scrape/start",
                json={
                    "sources": ["rieltor", "lun"],
                    "category": "residential",
                    "max_offers": 10,
                    "auto_export": False,
                },
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["status"], "started")
            self.assertEqual(data["sources"], ["rieltor", "lun"])
            self.assertEqual(data["category"], "residential")

    async def test_api_status_contains_multi_source_details(self):
        """Tests GET /api/status returns sources_detail with olx, rieltor, lun metrics."""
        resp = await self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertIn("sources_detail", data)
        self.assertIn("olx", data["sources_detail"])
        self.assertIn("rieltor", data["sources_detail"])
        self.assertIn("lun", data["sources_detail"])
        self.assertIn("db_stats", data)
        self.assertIn("residential_by_source", data["db_stats"])
        self.assertIn("commercial_by_source", data["db_stats"])

    async def test_api_export_with_source_filter(self):
        """Tests POST /api/export with source filter parameter."""
        resp = await self.client.post(
            "/api/export",
            json={
                "category": "residential",
                "source": "rieltor",
                "include_source": True,
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["source"], "rieltor")
        self.assertIn("exported_files", data)

    async def test_incremental_deduplication_and_seen_date_preservation(self):
        """Verifies get_existing_urls, seen_date preservation on conflict, and multi-session only_new."""
        s1 = database.create_scrape_session("all", db_path=self.db_path)
        s2 = database.create_scrape_session("all", db_path=self.db_path)

        database.upsert_residential_offer(
            {
                "url": "https://www.olx.ua/d/uk/obyavlenie/test-1-IDAAA.html",
                "seen_date": "2026-11-01",
                "price": 15000,
                "currency": "UAH",
                "rooms": 1,
                "area_total": 40.0,
                "district": "Печерський",
                "source": "olx",
            },
            session_id=s1,
            db_path=self.db_path,
        )

        # Upsert same URL in a later session with a newer seen_date -> original seen_date & first_session_id must be preserved
        database.upsert_residential_offer(
            {
                "url": "https://www.olx.ua/d/uk/obyavlenie/test-1-IDAAA.html",
                "seen_date": "2026-11-08",
                "price": 16000,
                "currency": "UAH",
                "rooms": 1,
                "area_total": 40.0,
                "district": "Печерський",
                "source": "olx",
            },
            session_id=s2,
            db_path=self.db_path,
        )

        # Add a genuinely new offer in session s2
        database.upsert_residential_offer(
            {
                "url": "https://rieltor.ua/flats-rent/view/123456/",
                "seen_date": "2026-11-08",
                "price": 20000,
                "currency": "UAH",
                "rooms": 2,
                "area_total": 55.0,
                "district": "Шевченківський",
                "source": "rieltor",
            },
            session_id=s2,
            db_path=self.db_path,
        )

        known = database.get_existing_urls(category="residential", db_path=self.db_path)
        self.assertIn("https://www.olx.ua/d/uk/obyavlenie/test-1-IDAAA.html", known)
        self.assertIn("https://rieltor.ua/flats-rent/view/123456/", known)

        # Check preserved seen_date on first offer
        olx_offers = database.get_all_residential_offers(db_path=self.db_path, source="olx")
        self.assertEqual(len(olx_offers), 1)
        self.assertEqual(olx_offers[0]["seen_date"], "2026-11-01")
        self.assertEqual(olx_offers[0]["price"], 16000)

        # Check only_new for session s2
        new_in_s2 = database.get_all_residential_offers(
            db_path=self.db_path, only_new=True, session_id=[s2]
        )
        self.assertEqual(len(new_in_s2), 1)
        self.assertEqual(new_in_s2[0]["url"], "https://rieltor.ua/flats-rent/view/123456/")

    async def test_monotonic_progress_pct_across_parallel_sources(self):
        """Verifies progress_pct never decreases even when total_found grows dynamically."""
        observed_pcts = []

        async def mock_olx_run(*args, **kwargs):
            on_prog = kwargs.get("on_progress")
            self.orchestrator.olx_scraper.total_slices = 10
            self.orchestrator.olx_scraper.completed_slices = 5
            self.orchestrator.olx_scraper.total_found = 10
            self.orchestrator.olx_scraper.processed_count = 10
            self.orchestrator.olx_scraper._update_progress_pct(None)
            if on_prog:
                on_prog(self.orchestrator.olx_scraper.get_status())
            # Now simulate discovering a huge new slice (total_found jumps from 10 to 500)
            self.orchestrator.olx_scraper.total_found = 500
            self.orchestrator.olx_scraper.processed_count = 15
            self.orchestrator.olx_scraper._update_progress_pct(None)
            if on_prog:
                on_prog(self.orchestrator.olx_scraper.get_status())
            return self.orchestrator.olx_scraper.get_status()

        self.orchestrator.olx_scraper.run = AsyncMock(side_effect=mock_olx_run)

        def record_prog(st):
            observed_pcts.append(st["progress_pct"])

        final_st = await self.orchestrator.run(
            sources=["olx"],
            category="residential",
            on_progress=record_prog,
        )
        self.assertEqual(final_st["progress_pct"], 100)
        # Ensure observed percentages are monotonically non-decreasing
        for i in range(1, len(observed_pcts)):
            self.assertGreaterEqual(observed_pcts[i], observed_pcts[i - 1])


if __name__ == "__main__":
    unittest.main()
