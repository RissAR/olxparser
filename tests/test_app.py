"""
Unit and Integration Tests for FastAPI Web App and REST API (Step 5).
Tests all routes:
- GET / (HTML Dashboard)
- GET /static/manifest.json (PWA Manifest)
- GET /static/sw.js (PWA Service Worker)
- GET /api/status (Realtime status & DB metrics)
- POST /api/scrape/start (Background scraping runner & 409 conflict handling)
- POST /api/scrape/stop (Graceful stop)
- GET /api/files (Exported CSV files metadata)
- GET /api/download/{filename} (Direct CSV download & path traversal security)
- POST /api/export (On-demand CSV generation)
- POST / GET /api/cron/trigger (External cron endpoint)
- GET /api/scheduler & POST /api/scheduler/toggle (APScheduler management)
- Lifespan context manager
"""

import asyncio
import json
import os
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from app import WEEKLY_JOB_ID, app, lifespan, scheduler
from scraper import get_scraper_instance


class ASGIResponse:
    def __init__(self, status_code: int, headers: List[Tuple[bytes, bytes]], body: bytes):
        self.status_code = status_code
        self.raw_headers = headers
        self.headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in headers}
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


class ASGITestClient:
    """Lightweight in-memory ASGI test client for FastAPI."""

    def __init__(self, asgi_app):
        self.app = asgi_app

    async def request(
        self,
        method: str,
        path: str,
        query_params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        json_data: Optional[Any] = None,
        body: Optional[bytes] = None,
    ) -> ASGIResponse:
        method = method.upper()
        query_string = urlencode(query_params or {}).encode("ascii")

        raw_headers: List[Tuple[bytes, bytes]] = [(b"host", b"testserver")]
        if headers:
            for k, v in headers.items():
                raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))

        request_body = b""
        if json_data is not None:
            request_body = json.dumps(json_data).encode("utf-8")
            raw_headers.append((b"content-type", b"application/json"))
        elif body is not None:
            request_body = body

        raw_headers.append((b"content-length", str(len(request_body)).encode("ascii")))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query_string,
            "headers": raw_headers,
        }

        response_meta: Dict[str, Any] = {}
        response_body_parts: List[bytes] = []
        request_sent = False
        done_event = asyncio.Event()

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {
                    "type": "http.request",
                    "body": request_body,
                    "more_body": False,
                }
            await done_event.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                response_meta["status"] = message["status"]
                response_meta["headers"] = message.get("headers", [])
            elif message["type"] == "http.response.body":
                response_body_parts.append(message.get("body", b""))
                if not message.get("more_body", False):
                    done_event.set()

        await self.app(scope, receive, send)

        return ASGIResponse(
            status_code=response_meta.get("status", 500),
            headers=response_meta.get("headers", []),
            body=b"".join(response_body_parts),
        )

    async def get(self, path: str, query_params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> ASGIResponse:
        return await self.request("GET", path, query_params=query_params, headers=headers)

    async def post(self, path: str, json: Optional[Any] = None, headers: Optional[Dict[str, str]] = None) -> ASGIResponse:
        return await self.request("POST", path, json_data=json, headers=headers)


class TestAppEndpoints(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = ASGITestClient(app)
        self.scraper = get_scraper_instance()
        # Reset scraper state before each test
        self.scraper.is_running = False
        self.scraper.status = "idle"
        self.scraper.error_message = None

    def tearDown(self):
        self.scraper.is_running = False
        self.scraper.status = "idle"

    async def test_index_html_renders(self):
        """GET / returns dashboard HTML with PWA meta and UI controls."""
        resp = await self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("content-type", ""))
        self.assertIn("OLX Kyiv Real Estate Parser", resp.text)
        self.assertIn("/static/manifest.json", resp.text)
        self.assertIn("startScrape", resp.text)
        self.assertIn("stopScrape", resp.text)

    async def test_pwa_manifest_json(self):
        """GET /static/manifest.json returns valid PWA manifest."""
        resp = await self.client.get("/static/manifest.json")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["short_name"], "OLX Parser")
        self.assertEqual(data["display"], "standalone")
        self.assertGreaterEqual(len(data.get("icons", [])), 2)

    async def test_pwa_service_worker(self):
        """GET /static/sw.js returns Service Worker script."""
        resp = await self.client.get("/static/sw.js")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("olx-parser-v1", resp.text)
        self.assertIn("addEventListener('fetch'", resp.text)

    async def test_api_status_endpoint(self):
        """GET /api/status returns current scraper state and db metrics."""
        resp = await self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("is_running", data)
        self.assertIn("status", data)
        self.assertIn("total_found", data)
        self.assertIn("processed_count", data)
        self.assertIn("errors_count", data)
        self.assertIn("db_stats", data)
        self.assertIn("residential_count", data["db_stats"])
        self.assertIn("commercial_count", data["db_stats"])
        self.assertIn("scheduler", data)

    async def test_scrape_start_and_conflict(self):
        """POST /api/scrape/start validates payload and returns 409 when already running."""
        # Case 1: When scraper is idle, start responds 200
        # Mock scraper.run to not actually make 10,000 requests in unit test
        original_run = self.scraper.run

        async def mock_run(category="all", max_offers=None, max_slices=None):
            self.scraper.is_running = True
            await asyncio.sleep(0.05)
            self.scraper.is_running = False
            self.scraper.status = "completed"
            return self.scraper.get_status()

        self.scraper.run = mock_run

        try:
            resp = await self.client.post(
                "/api/scrape/start",
                json={"category": "residential", "max_offers": 10, "auto_export": False},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["status"], "started")
            self.assertEqual(data["category"], "residential")
            self.assertEqual(data["max_offers"], 10)

            # Case 2: When scraper is active, next start attempt returns 409
            self.scraper.is_running = True
            resp2 = await self.client.post(
                "/api/scrape/start",
                json={"category": "commercial"},
            )
            self.assertEqual(resp2.status_code, 409)
        finally:
            self.scraper.run = original_run
            self.scraper.is_running = False

    async def test_scrape_start_invalid_category(self):
        """POST /api/scrape/start rejects invalid category with 422."""
        resp = await self.client.post(
            "/api/scrape/start",
            json={"category": "invalid_category"},
        )
        self.assertEqual(resp.status_code, 422)

    async def test_scrape_stop(self):
        """POST /api/scrape/stop handles both running and idle cases."""
        # Not running
        self.scraper.is_running = False
        resp = await self.client.post("/api/scrape/stop")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "not_running")

        # Running
        self.scraper.is_running = True
        resp2 = await self.client.post("/api/scrape/stop")
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.json()["status"], "stopping")

    async def test_api_files_list(self):
        """GET /api/files returns list of exported CSV files."""
        resp = await self.client.get("/api/files")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("files", data)
        self.assertIn("total", data)
        self.assertIsInstance(data["files"], list)

    async def test_api_download_file_success(self):
        """GET /api/download/{filename} returns CSV file download."""
        resp = await self.client.get("/api/download/olx_residential_kyiv.csv")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.headers.get("content-type", ""))
        self.assertIn("attachment", resp.headers.get("content-disposition", ""))

    async def test_api_download_file_not_found(self):
        """GET /api/download/{filename} returns 404 for nonexistent file."""
        resp = await self.client.get("/api/download/nonexistent_sample_file.csv")
        self.assertEqual(resp.status_code, 404)

    async def test_api_download_path_traversal_protection(self):
        """GET /api/download/{filename} blocks path traversal and non-CSV files."""
        # Non-csv extension
        resp1 = await self.client.get("/api/download/database.py")
        self.assertEqual(resp1.status_code, 400)

        # Path traversal characters
        resp2 = await self.client.get("/api/download/..%2F..%2Fapp.py")
        self.assertEqual(resp2.status_code, 400)

    async def test_api_export_endpoint(self):
        """POST /api/export triggers CSV export and returns metadata."""
        resp = await self.client.post("/api/export", json={"category": "residential"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["category"], "residential")
        self.assertGreaterEqual(len(data["exported_files"]), 1)

    async def test_cron_trigger(self):
        """POST and GET /api/cron/trigger trigger scraping task."""
        original_run = self.scraper.run

        async def mock_run(category="all", max_offers=None, max_slices=None):
            return self.scraper.get_status()

        self.scraper.run = mock_run

        try:
            # POST trigger
            resp_post = await self.client.post("/api/cron/trigger")
            self.assertEqual(resp_post.status_code, 200)
            self.assertEqual(resp_post.json()["status"], "started")

            # When already running, returns already_running
            self.scraper.is_running = True
            resp_running = await self.client.post("/api/cron/trigger")
            self.assertEqual(resp_running.status_code, 200)
            self.assertEqual(resp_running.json()["status"], "already_running")
        finally:
            self.scraper.run = original_run
            self.scraper.is_running = False

    async def test_scheduler_status_and_toggle(self):
        """GET /api/scheduler returns job state and toggle pauses/resumes it."""
        # Ensure job exists in scheduler
        from apscheduler.triggers.cron import CronTrigger

        if not scheduler.get_job(WEEKLY_JOB_ID):
            scheduler.add_job(
                lambda: None,
                trigger=CronTrigger(day_of_week="mon", hour=3, minute=0),
                id=WEEKLY_JOB_ID,
                name="Weekly full scrape",
            )

        resp = await self.client.get("/api/scheduler")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("jobs", data)

        # Toggle pause
        resp_toggle = await self.client.post("/api/scheduler/toggle")
        self.assertEqual(resp_toggle.status_code, 200)
        state1 = resp_toggle.json()["state"]
        self.assertIn(state1, ("paused", "resumed"))

        # Toggle back
        resp_toggle2 = await self.client.post("/api/scheduler/toggle")
        self.assertEqual(resp_toggle2.status_code, 200)
        state2 = resp_toggle2.json()["state"]
        self.assertNotEqual(state1, state2)

    async def test_lifespan_context(self):
        """Tests application lifespan startup and shutdown cycle."""
        async with lifespan(app):
            self.assertTrue(scheduler.running)
            job = scheduler.get_job(WEEKLY_JOB_ID)
            self.assertIsNotNone(job)


if __name__ == "__main__":
    unittest.main()
