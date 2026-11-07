"""
Asynchronous Scraper for LUN.ua Kyiv Real Estate (Stage 4).

Features:
- Resilient HTTP client using curl_cffi AsyncSession with Chrome impersonation.
- Concurrency limiting (asyncio.Semaphore) and randomized polite delays (0.5–1.5s).
- Exponential backoff on rate limits (HTTP 429), server errors (5xx), and network timeouts.
- Kyiv District Slicing across all 10 official Kyiv administrative districts.
- Support for Residential ('flats') and Commercial ('commercial') real estate.
- Robust Next.js App Router RSC payload card extractor and parser:
    * Bracket-counting string scanner extracting cards array from embedded __next_f JSON chunks
    * Unescaping and strict=False JSON parsing
    * Canonical URL protection (https://lun.ua/uk/realty/{id}) preventing third-party collision
    * Full geoEntities address parsing (street, house, district, residential complex)
    * Stage 2 ЖК fallback rule application
    * Full residential 23-column ML normalization
    * Full commercial 35-column ML normalization with aim mapping and conditional field clearing
- Continuous batch persistence into SQLite with deduplication and source='lun'.
- Full scrape session lifecycle and progress tracking for Web UI and Orchestrator.
"""

import asyncio
from datetime import datetime
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from curl_cffi.requests import AsyncSession

from database import (
    DEFAULT_DB_PATH,
    create_scrape_session,
    init_db,
    update_scrape_session,
    upsert_commercial_offers_batch,
    upsert_residential_offers_batch,
)
from normalizer import (
    LunNormalizer,
    get_address_parser,
    normalize_lun_commercial_offer,
    normalize_lun_residential_offer,
)

logger = logging.getLogger("lun_scraper")
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


# 10 Official Kyiv administrative districts with LUN URL slugs and Ukrainian names
LUN_KYIV_DISTRICTS: List[Tuple[str, str]] = [
    ("holosiivskyi", "Голосіївський"),
    ("darnytskyi", "Дарницький"),
    ("desnianskyi", "Деснянський"),
    ("dniprovskyi", "Дніпровський"),
    ("obolonskyi", "Оболонський"),
    ("pecherskyi", "Печерський"),
    ("podilskyi", "Подільський"),
    ("sviatoshynskyi", "Святошинський"),
    ("solomianskyi", "Солом'янський"),
    ("shevchenkivskyi", "Шевченківський"),
]

BASE_URL_RESIDENTIAL = "https://lun.ua/rent/kyiv/flats"
BASE_URL_COMMERCIAL = "https://lun.ua/rent/kyiv/commercial"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
}


@dataclass
class LunSlice:
    """Represents an atomic scrape slice (a category and a Kyiv district) on LUN.ua."""

    category_type: str  # 'residential' or 'commercial'
    district_slug: str  # e.g. 'holosiivskyi'
    district_name: str  # e.g. 'Голосіївський'
    label: str

    def get_url(self, page: int = 1) -> str:
        base = (
            f"https://lun.ua/rent/kyiv/flats-{self.district_slug}-district"
            if self.category_type == "residential"
            else f"https://lun.ua/rent/kyiv/commercial-{self.district_slug}-district"
        )
        if page <= 1:
            return base
        return f"{base}?page={page}"


class LunHttpClient:
    """
    Resilient asynchronous HTTP client for LUN.ua using curl_cffi with Chrome TLS impersonation.
    """

    def __init__(
        self,
        max_concurrency: int = 2,
        min_delay: float = 0.5,
        max_delay: float = 1.5,
        max_retries: int = 5,
        backoff_factor: float = 2.0,
        timeout: float = 25.0,
        proxy: Optional[str] = None,
    ):
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.timeout = timeout
        self.proxy: Optional[str] = (
            proxy or os.getenv("LUN_PROXY") or os.getenv("RIELTOR_PROXY") or os.getenv("OLX_PROXY")
        )
        self.session: Optional[AsyncSession] = None

    async def set_proxy(self, proxy: Optional[str]) -> None:
        """Updates proxy settings; if session exists and proxy changes, closes session for re-initialization."""
        clean_proxy = proxy.strip() if proxy and proxy.strip() else None
        if clean_proxy != self.proxy:
            self.proxy = clean_proxy
            await self.close()

    async def get_session(self) -> AsyncSession:
        if self.session is None:
            kwargs: Dict[str, Any] = {
                "impersonate": "chrome",
                "headers": DEFAULT_HEADERS,
                "timeout": self.timeout,
            }
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self.session = AsyncSession(**kwargs)
        return self.session

    async def close(self) -> None:
        if self.session is not None:
            try:
                await self.session.close()
            except Exception:
                pass
            self.session = None

    async def request_html(
        self,
        url: str,
        stop_event: Optional[asyncio.Event] = None,
    ) -> Optional[str]:
        """
        Executes an HTTP GET request with retry backoff and rate limit handling.
        Returns HTML string or None if request failed or stop requested.
        """
        if stop_event and stop_event.is_set():
            return None

        async with self.semaphore:
            if stop_event and stop_event.is_set():
                return None

            # Polite randomized delay before request
            delay = random.uniform(self.min_delay, self.max_delay)
            await asyncio.sleep(delay)

            session = await self.get_session()

            for attempt in range(1, self.max_retries + 1):
                if stop_event and stop_event.is_set():
                    return None

                try:
                    response = await session.get(url)

                    # Successful response
                    if response.status_code == 200:
                        return response.text

                    # Rate limited (429) or Server error (5xx)
                    if response.status_code in (429, 500, 502, 503, 504):
                        sleep_time = (self.backoff_factor ** attempt) + random.uniform(0.5, 1.5)
                        logger.warning(
                            "LUN HTTP %s on %s attempt %s/%s. Backing off for %.2fs...",
                            response.status_code,
                            url,
                            attempt,
                            self.max_retries,
                            sleep_time,
                        )
                        await asyncio.sleep(sleep_time)
                        continue

                    # 404 Not Found
                    if response.status_code == 404:
                        return None

                    logger.error("LUN HTTP error %s for %s", response.status_code, url)
                    return None

                except Exception as ex:
                    if stop_event and stop_event.is_set():
                        return None
                    sleep_time = (self.backoff_factor ** attempt) + random.uniform(0.5, 1.0)
                    logger.warning(
                        "Network error on %s attempt %s/%s: %s. Retrying in %.2fs...",
                        url, attempt, self.max_retries, ex, sleep_time
                    )
                    await asyncio.sleep(sleep_time)

            logger.error("Exceeded max retries (%s) for %s", self.max_retries, url)
            return None


class LunCardParser:
    """
    Parses LUN Next.js App Router RSC payload cards into structured data and normalized models.
    """

    def __init__(self, normalizer: Optional[LunNormalizer] = None):
        self.normalizer = normalizer or LunNormalizer(get_address_parser())

    @staticmethod
    def extract_cards(html_content: str) -> List[Dict[str, Any]]:
        """
        Extracts realties cards from LUN Next.js App Router RSC payload.
        The cards array is located inside:
        \"realties\":{\"cards\":[...]
        or
        "realties":{"cards":[...]
        """
        if not html_content:
            return []

        # Find starting marker of realties cards array
        m = re.search(r"(\\?\"realties\\?\":\s*\\?\{\s*\\?\"cards\\?\":\s*\[)", html_content)
        if not m:
            return []

        start_idx = m.end() - 1  # Points directly to the opening '['

        brace_depth = 0
        in_string = False
        escape = False
        end_idx = -1

        for i in range(start_idx, len(html_content)):
            c = html_content[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if not in_string:
                if c == "[":
                    brace_depth += 1
                elif c == "]":
                    brace_depth -= 1
                    if brace_depth == 0:
                        end_idx = i + 1
                        break

        if end_idx == -1:
            return []

        raw_json_chunk = html_content[start_idx:end_idx]
        # Clean escaped quotes and backslashes if embedded inside RSC string literal
        clean_json = raw_json_chunk.replace(r'\"', '"').replace(r"\\", "\\")
        try:
            cards = json.loads(clean_json, strict=False)
            if isinstance(cards, list):
                return cards
        except Exception as ex:
            logger.warning("Failed to parse LUN cards JSON chunk: %s", ex)

        return []

    def parse_card(
        self,
        card: Dict[str, Any],
        category_type: str,
        district_hint: str = "",
        seen_date: Optional[str] = None,
    ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """
        Parses a single LUN card dictionary.
        Returns a tuple: (normalized_offer_dict, raw_card_dict).
        """
        if not card or not isinstance(card, dict):
            return None

        card_id = str(card.get("id") or "")
        if not card_id:
            return None

        try:
            if category_type == "residential":
                norm_offer = self.normalizer.normalize_residential(card, seen_date=seen_date)
            else:
                norm_offer = self.normalizer.normalize_commercial(card, seen_date=seen_date)

            if not norm_offer.get("district") and district_hint:
                norm_offer["district"] = district_hint

            return norm_offer, card
        except Exception as ex:
            logger.warning("Error normalizing LUN card %s: %s", card_id, ex)
            return None


class LunScraper:
    """
    Main asynchronous scraping orchestrator for LUN.ua.
    Manages session lifecycle, district slicing, pagination, batch database persistence, and status.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        http_client: Optional[LunHttpClient] = None,
        parser: Optional[LunCardParser] = None,
    ):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.client = http_client or LunHttpClient()
        self.parser = parser or LunCardParser()
        self.stop_event = asyncio.Event()

        # Operational state
        self.is_running: bool = False
        self.status: str = "idle"
        self.session_id: Optional[int] = None
        self.category: str = "all"
        self.total_found: int = 0
        self.processed_count: int = 0
        self.processed_residential: int = 0
        self.processed_commercial: int = 0
        self.errors_count: int = 0
        self.current_slice_label: str = ""
        self.error_message: Optional[str] = None
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None

    def stop(self) -> None:
        """Signals the scraper to safely stop after current batch."""
        logger.info("Stop requested for LUN scraper.")
        self.stop_event.set()
        self.status = "stopping"

    def get_status(self) -> Dict[str, Any]:
        """Returns the current state dictionary for the Web UI or multi-orchestrator."""
        masked_proxy = None
        if self.client.proxy:
            masked_proxy = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", self.client.proxy)

        return {
            "is_running": self.is_running,
            "status": self.status,
            "session_id": self.session_id,
            "category": self.category,
            "total_found": self.total_found,
            "processed_count": self.processed_count,
            "processed_residential": self.processed_residential,
            "processed_commercial": self.processed_commercial,
            "errors_count": self.errors_count,
            "current_slice": self.current_slice_label,
            "error_message": self.error_message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "proxy": masked_proxy,
        }

    def generate_slices(self, category: str = "all") -> List[LunSlice]:
        """Generates the list of slices based on the requested category."""
        slices: List[LunSlice] = []

        if category in ("all", "residential"):
            for slug, name in LUN_KYIV_DISTRICTS:
                label = f"{name} (житлова)"
                slices.append(
                    LunSlice(
                        category_type="residential",
                        district_slug=slug,
                        district_name=name,
                        label=label,
                    )
                )

        if category in ("all", "commercial"):
            for slug, name in LUN_KYIV_DISTRICTS:
                label = f"{name} (комерція)"
                slices.append(
                    LunSlice(
                        category_type="commercial",
                        district_slug=slug,
                        district_name=name,
                        label=label,
                    )
                )

        return slices

    async def run(
        self,
        category: str = "all",
        max_offers: Optional[int] = None,
        max_slices: Optional[int] = None,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        proxy: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes a scrape run on LUN.ua for the requested category.
        category: 'residential', 'commercial', or 'all'
        max_offers: optional cap on processed offers (for testing/verification)
        max_slices: optional cap on number of slices to process
        on_progress: optional callback function triggered when stats update
        proxy: optional proxy URL
        """
        if self.is_running:
            raise RuntimeError("LUN scraper is already running a session.")

        if proxy is not None:
            await self.client.set_proxy(proxy.strip() if proxy.strip() else None)

        init_db(self.db_path)
        self.stop_event.clear()
        self.is_running = True
        self.category = category
        self.status = "running"
        self.total_found = 0
        self.processed_count = 0
        self.processed_residential = 0
        self.processed_commercial = 0
        self.errors_count = 0
        self.current_slice_label = ""
        self.error_message = None
        self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.finished_at = None

        # Create session record in database
        try:
            self.session_id = create_scrape_session(f"lun_{category}", self.db_path)
        except Exception as ex:
            logger.error("Failed to create scrape session: %s", ex)
            self.session_id = None

        logger.info(
            "Started LUN scrape session #%s for category '%s'",
            self.session_id, category
        )

        # Quotas for balanced category collection when category="all" and max_offers is set
        res_quota = max(1, max_offers // 2) if (category == "all" and max_offers is not None) else None
        comm_quota = (max_offers - res_quota) if (category == "all" and max_offers is not None) else None

        slices = self.generate_slices(category)
        if max_slices is not None:
            slices = slices[:max_slices]

        slices_processed = 0

        try:
            for s_idx, current_slice in enumerate(slices, start=1):
                if self.stop_event.is_set():
                    break

                if category == "all" and max_offers is not None:
                    if self.processed_residential >= res_quota and self.processed_commercial >= comm_quota:
                        break
                    if current_slice.category_type == "residential" and self.processed_residential >= res_quota:
                        continue
                    if current_slice.category_type == "commercial" and self.processed_commercial >= comm_quota:
                        continue

                self.current_slice_label = current_slice.label
                logger.info("Processing LUN Slice #%s: %s", s_idx, current_slice.label)

                page = 1
                seen_ids_in_slice: Set[str] = set()

                while not self.stop_event.is_set():
                    page_url = current_slice.get_url(page)
                    html_content = await self.client.request_html(page_url, self.stop_event)

                    if not html_content:
                        break

                    cards = self.parser.extract_cards(html_content)
                    if not cards:
                        # Reached the end of this slice
                        break

                    # Check for cycle / repeat pages
                    new_cards: List[Dict[str, Any]] = []
                    for c in cards:
                        cid = str(c.get("id") or "")
                        if cid and cid in seen_ids_in_slice:
                            continue
                        if cid:
                            seen_ids_in_slice.add(cid)
                        new_cards.append(c)

                    if not new_cards:
                        # All cards on this page were already processed in this slice
                        break

                    self.total_found += len(new_cards)

                    batch_offers: List[Tuple[Dict[str, Any], str]] = []
                    for c_dict in new_cards:
                        try:
                            parsed_tuple = self.parser.parse_card(
                                c_dict,
                                category_type=current_slice.category_type,
                                district_hint=current_slice.district_name,
                            )
                            if parsed_tuple:
                                norm_dict, raw_dict = parsed_tuple
                                raw_json = json.dumps(raw_dict, ensure_ascii=False)
                                batch_offers.append((norm_dict, raw_json))
                        except Exception as parse_err:
                            self.errors_count += 1
                            logger.warning("Error parsing LUN card: %s", parse_err)

                    # Cap batch if max_offers or category quota is set
                    if current_slice.category_type == "residential":
                        if category == "all" and res_quota is not None:
                            rem = max(0, res_quota - self.processed_residential)
                            if len(batch_offers) > rem:
                                batch_offers = batch_offers[:rem]
                        elif max_offers is not None:
                            rem = max(0, max_offers - self.processed_count)
                            if len(batch_offers) > rem:
                                batch_offers = batch_offers[:rem]
                    else:
                        if category == "all" and comm_quota is not None:
                            rem = max(0, comm_quota - self.processed_commercial)
                            if len(batch_offers) > rem:
                                batch_offers = batch_offers[:rem]
                        elif max_offers is not None:
                            rem = max(0, max_offers - self.processed_count)
                            if len(batch_offers) > rem:
                                batch_offers = batch_offers[:rem]

                    # Persist batch into database
                    if batch_offers:
                        try:
                            if current_slice.category_type == "residential":
                                upsert_residential_offers_batch(
                                    batch_offers,
                                    db_path=self.db_path,
                                    session_id=self.session_id,
                                )
                                self.processed_residential += len(batch_offers)
                            else:
                                upsert_commercial_offers_batch(
                                    batch_offers,
                                    db_path=self.db_path,
                                    session_id=self.session_id,
                                )
                                self.processed_commercial += len(batch_offers)

                            self.processed_count += len(batch_offers)
                        except Exception as db_err:
                            self.errors_count += 1
                            logger.error("Failed to upsert LUN batch into database: %s", db_err)

                    # Update database session
                    if self.session_id is not None:
                        try:
                            update_scrape_session(
                                session_id=self.session_id,
                                total_found=self.total_found,
                                processed_count=self.processed_count,
                                errors_count=self.errors_count,
                                status="running",
                                db_path=self.db_path,
                            )
                        except Exception:
                            pass

                    # Trigger progress callback
                    if on_progress:
                        try:
                            on_progress(self.get_status())
                        except Exception:
                            pass

                    # Check max_offers limit
                    if category == "all" and max_offers is not None:
                        if self.processed_residential >= res_quota and self.processed_commercial >= comm_quota:
                            logger.info("Reached target max_offers limit: %s", max_offers)
                            break
                        if current_slice.category_type == "residential" and self.processed_residential >= res_quota:
                            break
                        if current_slice.category_type == "commercial" and self.processed_commercial >= comm_quota:
                            break
                    elif max_offers is not None and self.processed_count >= max_offers:
                        logger.info("Reached target max_offers limit: %s", max_offers)
                        break

                    page += 1

                slices_processed += 1
                if category == "all" and max_offers is not None:
                    if self.processed_residential >= res_quota and self.processed_commercial >= comm_quota:
                        break
                elif max_offers is not None and self.processed_count >= max_offers:
                    break

            if self.stop_event.is_set():
                self.status = "stopped"
            else:
                self.status = "completed"

        except Exception as fatal_ex:
            self.status = "failed"
            self.error_message = str(fatal_ex)
            self.errors_count += 1
            logger.exception("Fatal error in LUN scrape workflow: %s", fatal_ex)
        finally:
            self.is_running = False
            self.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if self.session_id is not None:
                try:
                    update_scrape_session(
                        session_id=self.session_id,
                        total_found=self.total_found,
                        processed_count=self.processed_count,
                        errors_count=self.errors_count,
                        status=self.status,
                        error_message=self.error_message,
                        db_path=self.db_path,
                    )
                except Exception:
                    pass

            await self.client.close()
            logger.info(
                "LUN scrape session #%s finished with status '%s'. Processed: %s, Errors: %s",
                self.session_id, self.status, self.processed_count, self.errors_count
            )

        return self.get_status()
