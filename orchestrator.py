"""
Multi-Source Asynchronous Scraping Orchestrator (Stage 5).

Coordinates scraping across multiple Kyiv real estate data sources:
- OLX.ua (olx_scraper / OLXScraper)
- Rieltor.ua (rieltor_scraper / RieltorScraper)
- LUN.ua (lun_scraper / LunScraper)

Features:
- Single-source, multi-source, or unified all-sources execution.
- Unified progress reporting and metrics aggregation for Web UI and REST API.
- Coordinated graceful stop signaling across all active scraper sub-processes.
- Detailed per-source progress tracking and error isolation.
- Automatic database session correlation and multi-source CSV export triggering.
"""

import asyncio
from datetime import datetime
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional, Union

from database import init_db
from lun_scraper import LunScraper
from rieltor_scraper import RieltorScraper
from scraper import OLXScraper, get_scraper_instance

logger = logging.getLogger("multi_orchestrator")
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

SUPPORTED_SOURCES: List[str] = ["olx", "rieltor", "lun"]


class MultiSourceOrchestrator:
    """
    Coordinates and monitors multi-source scraping runs across OLX, Rieltor.ua, and LUN.ua.
    """

    def __init__(self, db_path: str = "kyiv_rent.db"):
        self.db_path = db_path
        self.olx_scraper = get_scraper_instance(db_path=self.db_path)
        self.rieltor_scraper = RieltorScraper(db_path=self.db_path)
        self.lun_scraper = LunScraper(db_path=self.db_path)
        self.stop_event = asyncio.Event()

        # Operational state
        self.is_running: bool = False
        self.status: str = "idle"  # idle, running, stopping, completed, failed
        self.sources: List[str] = []
        self.active_source: Optional[str] = None
        self.category: str = "all"
        self.total_found: int = 0
        self.processed_count: int = 0
        self.processed_residential: int = 0
        self.processed_commercial: int = 0
        self.errors_count: int = 0
        self.current_slice: str = ""
        self.error_message: Optional[str] = None
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.source_statuses: Dict[str, Dict[str, Any]] = {
            "olx": {},
            "rieltor": {},
            "lun": {},
        }

    def stop(self) -> None:
        """Signals graceful stop across all underlying scrapers."""
        logger.info("MultiSourceOrchestrator stop signal received.")
        self.stop_event.set()
        self.status = "stopping"

        if self.olx_scraper.is_running:
            self.olx_scraper.stop()
        if self.rieltor_scraper.is_running:
            self.rieltor_scraper.stop()
        if self.lun_scraper.is_running:
            self.lun_scraper.stop()

    def get_status(self) -> Dict[str, Any]:
        """Returns unified aggregate status and per-source status breakdown."""
        active_sub_session_id = None
        if self.active_source == "olx":
            active_sub_session_id = self.olx_scraper.session_id
        elif self.active_source == "rieltor":
            active_sub_session_id = self.rieltor_scraper.session_id
        elif self.active_source == "lun":
            active_sub_session_id = self.lun_scraper.session_id

        return {
            "is_running": self.is_running,
            "status": self.status,
            "sources": self.sources,
            "active_source": self.active_source,
            "session_id": active_sub_session_id,
            "category": self.category,
            "total_found": self.total_found,
            "processed_count": self.processed_count,
            "processed_residential": self.processed_residential,
            "processed_commercial": self.processed_commercial,
            "errors_count": self.errors_count,
            "current_slice": self.current_slice,
            "error_message": self.error_message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "sources_detail": {
                "olx": self.olx_scraper.get_status(),
                "rieltor": self.rieltor_scraper.get_status(),
                "lun": self.lun_scraper.get_status(),
            },
        }

    async def run(
        self,
        sources: Optional[Union[List[str], str]] = None,
        category: str = "all",
        max_offers: Optional[int] = None,
        max_slices: Optional[int] = None,
        proxy: Optional[str] = None,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Executes scraping across specified sources sequentially.
        sources: List of 'olx', 'rieltor', 'lun', or 'all' (default: all supported)
        category: 'residential', 'commercial', or 'all'
        max_offers: optional max offers per source or overall limit
        max_slices: optional max slices per source
        proxy: optional proxy URL
        on_progress: progress callback receiving aggregated status dict
        """
        if self.is_running:
            raise RuntimeError("MultiSourceOrchestrator is already running a session.")

        # Normalize sources
        selected_sources: List[str] = []
        if not sources or sources == "all":
            selected_sources = list(SUPPORTED_SOURCES)
        elif isinstance(sources, str):
            parts = [s.strip().lower() for s in sources.split(",") if s.strip()]
            selected_sources = [s for s in parts if s in SUPPORTED_SOURCES]
        elif isinstance(sources, list):
            selected_sources = [s.strip().lower() for s in sources if s.strip().lower() in SUPPORTED_SOURCES]

        if not selected_sources:
            selected_sources = list(SUPPORTED_SOURCES)

        init_db(self.db_path)
        self.stop_event.clear()
        self.is_running = True
        self.status = "running"
        self.sources = selected_sources
        self.category = category
        self.active_source = None
        self.total_found = 0
        self.processed_count = 0
        self.processed_residential = 0
        self.processed_commercial = 0
        self.errors_count = 0
        self.current_slice = ""
        self.error_message = None
        self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.finished_at = None

        logger.info(
            "MultiSourceOrchestrator starting run: sources=%s, category=%s, max_offers=%s, max_slices=%s",
            self.sources, category, max_offers, max_slices
        )

        accumulated_found = 0
        accumulated_processed = 0
        accumulated_res = 0
        accumulated_comm = 0
        accumulated_errors = 0

        try:
            for src in self.sources:
                if self.stop_event.is_set():
                    break

                self.active_source = src
                logger.info(">>> Orchestrator activating source: %s", src.upper())

                # Select scraper
                if src == "olx":
                    scraper = self.olx_scraper
                elif src == "rieltor":
                    scraper = self.rieltor_scraper
                elif src == "lun":
                    scraper = self.lun_scraper
                else:
                    continue

                def on_sub_progress(sub_st: Dict[str, Any]) -> None:
                    self.total_found = accumulated_found + sub_st.get("total_found", 0)
                    self.processed_count = accumulated_processed + sub_st.get("processed_count", 0)
                    self.processed_residential = accumulated_res + sub_st.get("processed_residential", 0)
                    self.processed_commercial = accumulated_comm + sub_st.get("processed_commercial", 0)
                    self.errors_count = accumulated_errors + sub_st.get("errors_count", 0)
                    self.current_slice = f"[{src.upper()}] {sub_st.get('current_slice', '')}"

                    if on_progress:
                        try:
                            on_progress(self.get_status())
                        except Exception:
                            pass

                # Run scraper for this source
                try:
                    res = await scraper.run(
                        category=category,
                        max_offers=max_offers,
                        max_slices=max_slices,
                        proxy=proxy,
                        on_progress=on_sub_progress,
                    )
                    self.source_statuses[src] = res
                    accumulated_found += res.get("total_found", 0)
                    accumulated_processed += res.get("processed_count", 0)
                    accumulated_res += res.get("processed_residential", 0)
                    accumulated_comm += res.get("processed_commercial", 0)
                    accumulated_errors += res.get("errors_count", 0)
                except Exception as src_ex:
                    accumulated_errors += 1
                    logger.exception("Error executing scraper for source '%s': %s", src, src_ex)
                    self.source_statuses[src] = {
                        "status": "failed",
                        "error_message": str(src_ex),
                    }

                # Update running totals
                self.total_found = accumulated_found
                self.processed_count = accumulated_processed
                self.processed_residential = accumulated_res
                self.processed_commercial = accumulated_comm
                self.errors_count = accumulated_errors

                if on_progress:
                    try:
                        on_progress(self.get_status())
                    except Exception:
                        pass

                if self.stop_event.is_set():
                    break

            if self.stop_event.is_set():
                self.status = "stopped"
            else:
                self.status = "completed"

        except Exception as fatal_ex:
            self.status = "failed"
            self.error_message = str(fatal_ex)
            self.errors_count += 1
            logger.exception("Fatal error in MultiSourceOrchestrator: %s", fatal_ex)
        finally:
            self.is_running = False
            self.active_source = None
            self.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info(
                "MultiSourceOrchestrator finished with status '%s'. Processed: %s (Res: %s, Comm: %s), Errors: %s",
                self.status, self.processed_count, self.processed_residential, self.processed_commercial, self.errors_count
            )

        return self.get_status()


# Module-level singleton instance for orchestrator
_orchestrator_instance: Optional[MultiSourceOrchestrator] = None


def get_orchestrator_instance(db_path: Optional[str] = None) -> MultiSourceOrchestrator:
    """Returns the singleton instance of MultiSourceOrchestrator."""
    global _orchestrator_instance
    if _orchestrator_instance is None:
        _orchestrator_instance = MultiSourceOrchestrator(db_path=db_path or "kyiv_rent.db")
    return _orchestrator_instance
