"""
Web UI and API Management Service for OLX Kyiv Real Estate Scraper.

Built with:
- FastAPI for high-performance async REST API and Web UI serving.
- Jinja2 templates + Tailwind CSS (via CDN) for modern, responsive PWA dashboard.
- APScheduler for weekly scheduled scraping jobs (Mondays at 03:00 Kyiv time).
- Endpoints for scraping lifecycle, status polling, file downloads, and system cron triggers.
"""

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from database import get_stats, init_db
from exporter import (
    DEFAULT_COMMERCIAL_FILENAME,
    DEFAULT_EXPORTS_DIR,
    DEFAULT_RESIDENTIAL_FILENAME,
    export_all,
    export_commercial,
    export_residential,
    list_exported_files,
)
from scraper import OLXScraper, get_scraper_instance

# Setup logging
logger = logging.getLogger("olx_web_app")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [app]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Base directories
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Scheduler singleton
scheduler = AsyncIOScheduler()
WEEKLY_JOB_ID = "weekly_scrape_job"

# Active background scraping task reference
_current_scrape_task: Optional[asyncio.Task] = None


async def run_scraping_workflow(
    category: str = "all",
    max_offers: Optional[int] = None,
    max_slices: Optional[int] = None,
    auto_export: bool = True,
) -> None:
    """
    Executes the scraping workflow in the background.
    Automatically runs CSV exporter upon successful completion if auto_export is True.
    """
    scraper = get_scraper_instance()
    logger.info(
        "Starting background scrape workflow: category=%s, max_offers=%s, max_slices=%s, auto_export=%s",
        category,
        max_offers,
        max_slices,
        auto_export,
    )
    try:
        await scraper.run(
            category=category,
            max_offers=max_offers,
            max_slices=max_slices,
        )
        if auto_export and scraper.status in ("completed", "stopped"):
            logger.info("Scraper finished (%s). Running automatic CSV export...", scraper.status)
            export_all()
            logger.info("Automatic CSV export completed successfully.")
    except asyncio.CancelledError:
        logger.info("Scrape task was cancelled.")
        scraper.stop()
    except Exception as exc:
        logger.exception("Error occurred in background scrape task: %s", exc)


async def scheduled_weekly_job() -> None:
    """Triggered weekly by APScheduler."""
    scraper = get_scraper_instance()
    if scraper.is_running:
        logger.warning("Weekly scheduled scrape skipped: Scraper is already running.")
        return
    logger.info("APScheduler executing weekly full scrape & export job...")
    global _current_scrape_task
    _current_scrape_task = asyncio.create_task(
        run_scraping_workflow(category="all", auto_export=True)
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager: starts scheduler and DB on boot, cleans up on exit."""
    # Startup
    logger.info("Initializing OLX Real Estate Parser application...")
    init_db()

    # Configure weekly job: Every Monday at 03:00 (Kyiv time)
    scheduler.add_job(
        scheduled_weekly_job,
        trigger=CronTrigger(day_of_week="mon", hour=3, minute=0, timezone="Europe/Kyiv"),
        id=WEEKLY_JOB_ID,
        name="Weekly full scrape and CSV export",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("APScheduler started with weekly job scheduled for Mondays at 03:00 Europe/Kyiv.")

    yield

    # Shutdown
    logger.info("Shutting down application...")
    scraper = get_scraper_instance()
    if scraper.is_running:
        logger.info("Stopping active scraper on server shutdown...")
        scraper.stop()

    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler shut down.")


# FastAPI application instance
app = FastAPI(
    title="OLX Kyiv Real Estate Parser",
    description="Web UI and REST API for OLX Kyiv rental housing and commercial real estate monitoring.",
    version="1.0.0",
    lifespan=lifespan,
)

# Static and Templates mounting
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# -----------------------------------------------------------------------------
# Request & Response Schemas
# -----------------------------------------------------------------------------
class ScrapeRequest(BaseModel):
    category: str = Field(default="all", pattern="^(all|residential|commercial)$")
    max_offers: Optional[int] = Field(default=None, ge=1)
    max_slices: Optional[int] = Field(default=None, ge=1)
    auto_export: bool = True


class ExportRequest(BaseModel):
    category: str = Field(default="all", pattern="^(all|residential|commercial)$")


# -----------------------------------------------------------------------------
# Web UI Route
# -----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index_view(request: Request):
    """Renders the main dashboard Web UI."""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "title": "OLX Kyiv Real Estate Parser",
        },
    )


# -----------------------------------------------------------------------------
# REST API Endpoints
# -----------------------------------------------------------------------------
@app.post("/api/scrape/start")
async def start_scraping(req: ScrapeRequest):
    """
    POST /api/scrape/start
    Starts a background scraping task for residential, commercial, or both categories.
    """
    scraper = get_scraper_instance()
    if scraper.is_running:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Scraper is already running a session.",
        )

    global _current_scrape_task
    _current_scrape_task = asyncio.create_task(
        run_scraping_workflow(
            category=req.category,
            max_offers=req.max_offers,
            max_slices=req.max_slices,
            auto_export=req.auto_export,
        )
    )

    return {
        "status": "started",
        "category": req.category,
        "max_offers": req.max_offers,
        "max_slices": req.max_slices,
        "auto_export": req.auto_export,
    }


@app.post("/api/scrape/stop")
async def stop_scraping():
    """
    POST /api/scrape/stop
    Sends graceful stop signal to the active scraper task.
    """
    scraper = get_scraper_instance()
    if not scraper.is_running:
        return {
            "status": "not_running",
            "message": "Scraper is not running.",
        }

    scraper.stop()
    return {
        "status": "stopping",
        "message": "Stop signal sent to scraper.",
    }


@app.get("/api/status")
async def get_status():
    """
    GET /api/status
    Returns realtime state (idle/running/stopping/completed/failed), counters,
    database metrics, and scheduler next run information.
    """
    scraper = get_scraper_instance()
    st = scraper.get_status()

    # Get DB statistics
    try:
        db_stats = get_stats()
    except Exception as exc:
        logger.error("Failed to fetch database stats: %s", exc)
        db_stats = {"residential_count": 0, "commercial_count": 0, "latest_session": None}

    # Get APScheduler status
    job = scheduler.get_job(WEEKLY_JOB_ID)
    next_run_dt = getattr(job, "next_run_time", None) if job else None
    next_run = next_run_dt.isoformat() if next_run_dt else None

    st["db_stats"] = db_stats
    st["scheduler"] = {
        "running": scheduler.running,
        "weekly_job_active": job is not None,
        "next_run_time": next_run,
    }
    return st


@app.get("/api/files")
async def get_files():
    """
    GET /api/files
    Returns list of generated CSV files with metadata (rows count, size, timestamps).
    """
    files = list_exported_files()
    return {
        "files": files,
        "total": len(files),
    }


@app.get("/api/download/{filename}")
async def download_file(filename: str):
    """
    GET /api/download/{filename}
    Direct download of generated CSV files with strict validation against path traversal.
    """
    # Strict validation of filename
    if not re.match(r"^[\w\.-]+\.csv$", filename) or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename specified.",
        )

    # Search in project root and exports/
    potential_paths = [
        BASE_DIR / filename,
        BASE_DIR / DEFAULT_EXPORTS_DIR / filename,
    ]

    target_file: Optional[Path] = None
    for p in potential_paths:
        if p.is_file():
            target_file = p
            break

    if not target_file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File '{filename}' not found.",
        )

    return FileResponse(
        path=str(target_file),
        filename=filename,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/export")
async def trigger_export(req: Optional[ExportRequest] = None):
    """
    POST /api/export
    Triggers on-demand CSV export of current SQLite database records.
    """
    cat = req.category if req else "all"
    exported_results: List[Dict[str, Any]] = []

    try:
        if cat == "residential":
            info = export_residential()
            exported_results.append(info)
        elif cat == "commercial":
            info = export_commercial()
            exported_results.append(info)
        else:
            res = export_all()
            if "residential" in res:
                exported_results.append(res["residential"])
            if "commercial" in res:
                exported_results.append(res["commercial"])

        return {
            "status": "success",
            "category": cat,
            "exported_files": exported_results,
        }
    except Exception as exc:
        logger.exception("Manual export failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Export failed: {str(exc)}",
        )


@app.api_route("/api/cron/trigger", methods=["GET", "POST"])
async def cron_trigger():
    """
    GET / POST /api/cron/trigger
    Trigger endpoint for Linux system crontab on VDS.
    Starts background scrape for all categories with auto-export.
    """
    scraper = get_scraper_instance()
    if scraper.is_running:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "already_running",
                "message": "Scraper is currently running. Cron trigger ignored.",
            },
        )

    global _current_scrape_task
    _current_scrape_task = asyncio.create_task(
        run_scraping_workflow(category="all", auto_export=True)
    )
    return {
        "status": "started",
        "source": "system_cron",
        "category": "all",
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/scheduler")
async def get_scheduler_status():
    """
    GET /api/scheduler
    Returns detailed scheduler state and configured jobs.
    """
    jobs = []
    for j in scheduler.get_jobs():
        nrt = getattr(j, "next_run_time", None)
        jobs.append({
            "id": j.id,
            "name": j.name,
            "next_run_time": nrt.isoformat() if nrt else None,
            "trigger": str(j.trigger),
        })

    return {
        "running": scheduler.running,
        "jobs": jobs,
    }


@app.post("/api/scheduler/toggle")
async def toggle_scheduler_job():
    """
    POST /api/scheduler/toggle
    Pauses or resumes the weekly scheduled job.
    """
    job = scheduler.get_job(WEEKLY_JOB_ID)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Weekly job not found in scheduler.",
        )

    nrt = getattr(job, "next_run_time", None)
    if nrt is not None:
        job.pause()
        state = "paused"
    else:
        job.resume()
        state = "resumed"

    new_nrt = getattr(job, "next_run_time", None)
    return {
        "job_id": WEEKLY_JOB_ID,
        "state": state,
        "next_run_time": new_nrt.isoformat() if new_nrt else None,
    }


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
