"""
Asynchronous Scraper and Slicing Module for OLX Kyiv Real Estate.

Features:
- Resilient HTTP client using curl_cffi AsyncSession with Chrome impersonation.
- Concurrency limiting (asyncio.Semaphore) and randomized polite delays (0.5–1.5s).
- Exponential backoff on rate limits (HTTP 429), server errors (5xx), and network timeouts.
- Multi-level Slicing Generator:
    * 10 Kyiv administrative districts
    * Room counts (residential) and subcategories (commercial)
    * Recursive price bisection (filter_float_price:from / to) when visible_total_count > 1000
    * Page-by-page traversal (offset = 0, 40, 80... up to max 1000)
- Offer filtering:
    * Rejection of seek/buy requests (offer_type == 'seek' or keywords)
    * Rejection of sales in rent categories
    * Rejection of multilots without fixed price or area
- Batch persistence into SQLite with continuous deduplication by URL.
- Scrape session progress tracking for integration with Web UI and CLI.
"""

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple

from curl_cffi.requests import AsyncSession

from database import (
    create_scrape_session,
    init_db,
    update_scrape_session,
    upsert_commercial_offers_batch,
    upsert_residential_offers_batch,
)
from normalizer import (
    normalize_commercial_offer,
    normalize_residential_offer,
)

logger = logging.getLogger("olx_scraper")
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


BASE_API_URL = "https://www.olx.ua/api/v1/offers"
CITY_ID_KYIV = 268
PAGE_LIMIT = 40
MAX_OFFSET_LIMIT = 1000

# 10 Official Kyiv administrative districts with IDs on OLX
KYIV_DISTRICTS: List[Tuple[int, str]] = [
    (1, "Голосіївський"),
    (3, "Дарницький"),
    (5, "Деснянський"),
    (7, "Дніпровський"),
    (9, "Оболонський"),
    (11, "Печерський"),
    (13, "Подільський"),
    (15, "Святошинський"),
    (17, "Солом'янський"),
    (19, "Шевченківський"),
]

# Residential rent category ID on OLX
RESIDENTIAL_CATEGORY_ID = 1760

# Rooms filter keys on OLX for category 1760
RESIDENTIAL_ROOM_FILTERS: List[str] = [
    "odnokomnatnye",
    "dvuhkomnatnye",
    "trehkomnatnye",
    "chetyrehkomnatnye",
    "pyatikomnatnye",
]

# Commercial rental subcategories on OLX
COMMERCIAL_CATEGORY_IDS: List[int] = [
    3690,  # Оренда офісів
    3691,  # Оренда виробничих приміщень, ферм
    3692,  # Оренда торгових приміщень
    3693,  # Оренда складів, ангарів
    3694,  # Оренда кафе, ресторанів, кав'ярень
    3695,  # Оренда МАФів
    3696,  # Оренда СТО, автомийок, АЗС
    3697,  # Оренда салонів, кабінетів краси
    3698,  # Оренда залів, спортивних приміщень
    3699,  # Оренда медичних приміщень
    3700,  # Оренда приміщення вільного призначення
    3701,  # Оренда інших приміщень
]

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
}

# Regex to detect seek, buy, or sale listings accidentally posted in rent
RE_SEEK_OR_SALE = re.compile(
    r"\b(продам|продаж|продается|продажа|куплю|купимо|зніму|сниму|шукаю\s+в\s+оренду|ищу\s+в\s+аренду|шукаю\s+житло)\b",
    re.IGNORECASE,
)

# Regex to detect multilot range in title when total_area param is missing or range
RE_MULTILOT_RANGE = re.compile(
    r"(?:"
    r"\b(?:від|от)\s+\d+\s*(?:м[²2]|кв\.?\s*м|кв)?\s*(?:до|-)\s*\d+\s*(?:м[²2]|кв|кв\.?\s*м)\b"
    r"|"
    r"\b\d+\s*[-–—]\s*\d+\s*(?:м[²2]|кв|кв\.?\s*м)\b"
    r")",
    re.IGNORECASE,
)


def extract_price_value(offer: Dict[str, Any]) -> Optional[float]:
    """Extracts raw numeric price from offer parameters."""
    for param in offer.get("params", []):
        if param.get("key") == "price":
            val = param.get("value")
            if isinstance(val, dict):
                p = val.get("value")
                if p is not None:
                    try:
                        return float(p)
                    except (ValueError, TypeError):
                        pass
            elif isinstance(val, (int, float)):
                return float(val)
            elif isinstance(val, str):
                cleaned = re.sub(r"[^\d.]", "", val)
                if cleaned:
                    try:
                        return float(cleaned)
                    except ValueError:
                        pass
    return None


def extract_total_area_value(offer: Dict[str, Any]) -> Optional[float]:
    """Extracts raw total area from offer parameters."""
    for param in offer.get("params", []):
        if param.get("key") == "total_area":
            val = param.get("value")
            if isinstance(val, dict):
                a = val.get("value") or val.get("key")
            else:
                a = val
            if a is not None:
                cleaned = re.sub(r"[^\d.]", "", str(a).replace(",", "."))
                if cleaned:
                    try:
                        return float(cleaned)
                    except ValueError:
                        pass
    return None


def is_valid_rental_offer(
    offer: Dict[str, Any],
    category_type: str = "residential",
    target_district: Optional[str] = None
) -> bool:
    """
    Filters offers according to requirements:
    1. Rejects seek / buy / sale listings (offer_type == 'seek', or seek/sale keywords).
    2. Rejects multilots without fixed price or fixed area.
    3. Rejects promoted/sponsored ads injected from other districts into this slice.
    """
    # Check top-level offer type
    if offer.get("offer_type") == "seek":
        return False

    # Check params for offer_seek
    for param in offer.get("params", []):
        if param.get("key") == "offer_seek":
            return False

    title = offer.get("title", "")
    desc = offer.get("description", "")

    # Check for obvious seek or sale in title
    if RE_SEEK_OR_SALE.search(title):
        return False

    # Check price
    price = extract_price_value(offer)
    if price is None or price <= 0:
        return False

    # Check area
    area = extract_total_area_value(offer)
    if area is None or area <= 0:
        return False

    # Check multilot range in title for commercial offers
    if category_type == "commercial" and RE_MULTILOT_RANGE.search(title):
        return False

    # Filter out promoted top-ads from different districts injected into this slice
    if target_district:
        promotion = offer.get("promotion") or {}
        is_promoted = bool(promotion.get("top_ad") or promotion.get("highlighted") or offer.get("is_promoted"))
        if is_promoted:
            loc = offer.get("location") or {}
            dist_obj = loc.get("district") or {}
            offer_district = dist_obj.get("name")
            if offer_district and offer_district != target_district:
                return False

    return True


@dataclass
class Slice:
    """Search query segment created by the slicing algorithm."""

    category_type: str  # 'residential' or 'commercial'
    category_id: int
    district_id: int
    district_name: str
    room_enum: Optional[str] = None
    price_from: Optional[int] = None
    price_to: Optional[int] = None
    visible_total_count: int = 0
    label: str = ""

    def to_query_params(self, offset: int = 0, limit: int = PAGE_LIMIT) -> Dict[str, Any]:
        """Converts slice configuration into OLX API query parameters."""
        params: Dict[str, Any] = {
            "offset": offset,
            "limit": limit,
            "category_id": self.category_id,
            "city_id": CITY_ID_KYIV,
            "district_id": self.district_id,
        }
        if self.room_enum:
            params["filter_enum_number_of_rooms_string[0]"] = self.room_enum
        if self.price_from is not None and self.price_from > 0:
            params["filter_float_price:from"] = self.price_from
        if self.price_to is not None:
            params["filter_float_price:to"] = self.price_to
        return params


class OLXHttpClient:
    """
    Resilient asynchronous HTTP client with Chrome TLS fingerprint impersonation,
    semaphore concurrency control, randomized polite pauses, and exponential backoff.
    """

    def __init__(
        self,
        max_concurrency: int = 3,
        min_delay: float = 0.5,
        max_delay: float = 1.5,
        max_retries: int = 5,
        backoff_factor: float = 2.0,
        timeout: float = 20.0,
    ):
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.timeout = timeout
        self.session: Optional[AsyncSession] = None

    async def get_session(self) -> AsyncSession:
        if self.session is None:
            self.session = AsyncSession(
                impersonate="chrome",
                headers=DEFAULT_HEADERS,
                timeout=self.timeout,
            )
        return self.session

    async def close(self) -> None:
        if self.session is not None:
            try:
                await self.session.close()
            except Exception:
                pass
            self.session = None

    async def request_json(
        self,
        url: str = BASE_API_URL,
        params: Optional[Dict[str, Any]] = None,
        stop_event: Optional[asyncio.Event] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Executes an HTTP GET request with retry backoff and rate limit handling.
        Returns parsed JSON dict or None if request failed or stop requested.
        """
        if stop_event and stop_event.is_set():
            return None

        async with self.semaphore:
            if stop_event and stop_event.is_set():
                return None

            # Polite randomized delay before firing request
            delay = random.uniform(self.min_delay, self.max_delay)
            await asyncio.sleep(delay)

            session = await self.get_session()

            for attempt in range(1, self.max_retries + 1):
                if stop_event and stop_event.is_set():
                    return None

                try:
                    response = await session.get(url, params=params)

                    # Successful response
                    if response.status_code == 200:
                        return response.json()

                    # Too Many Requests (429) or Server Errors (5xx)
                    if response.status_code in (429, 500, 502, 503, 504):
                        sleep_time = (self.backoff_factor ** attempt) + random.uniform(0.5, 1.5)
                        logger.warning(
                            "HTTP %s on %s attempt %s/%s. Backing off for %.2fs...",
                            response.status_code,
                            params,
                            attempt,
                            self.max_retries,
                            sleep_time,
                        )
                        await asyncio.sleep(sleep_time)
                        continue

                    # Bad Request (400) or other unrecoverable status
                    logger.warning(
                        "HTTP %s for params: %s. Body: %s",
                        response.status_code,
                        params,
                        response.text[:200],
                    )
                    return None

                except Exception as exc:
                    sleep_time = (self.backoff_factor ** attempt) + random.uniform(0.5, 1.5)
                    logger.warning(
                        "Network/TLS error: %s on attempt %s/%s. Backing off for %.2fs...",
                        exc,
                        attempt,
                        self.max_retries,
                        sleep_time,
                    )
                    await asyncio.sleep(sleep_time)

            logger.error("Failed to fetch %s after %s attempts.", params, self.max_retries)
            return None


class SliceGenerator:
    """
    Generates search segments for OLX Kyiv Real Estate.
    Recursively bisects by price when total items exceed the 1000 items hard pagination limit.
    """

    def __init__(self, client: OLXHttpClient, max_bisection_depth: int = 6):
        self.client = client
        self.max_bisection_depth = max_bisection_depth

    async def get_count(
        self,
        category_id: int,
        district_id: int,
        room_enum: Optional[str] = None,
        price_from: Optional[int] = None,
        price_to: Optional[int] = None,
        stop_event: Optional[asyncio.Event] = None,
    ) -> int:
        """Retrieves visible_total_count for a slice candidate."""
        params: Dict[str, Any] = {
            "offset": 0,
            "limit": 1,
            "category_id": category_id,
            "city_id": CITY_ID_KYIV,
            "district_id": district_id,
        }
        if room_enum:
            params["filter_enum_number_of_rooms_string[0]"] = room_enum
        if price_from is not None and price_from > 0:
            params["filter_float_price:from"] = price_from
        if price_to is not None:
            params["filter_float_price:to"] = price_to

        res = await self.client.request_json(params=params, stop_event=stop_event)
        if res and "metadata" in res:
            return int(res["metadata"].get("visible_total_count", 0))
        return 0

    async def _bisect_price(
        self,
        category_type: str,
        category_id: int,
        district_id: int,
        district_name: str,
        room_enum: Optional[str],
        p_min: int,
        p_max: int,
        depth: int,
        stop_event: Optional[asyncio.Event] = None,
        is_top_range: bool = True,
    ) -> AsyncGenerator[Slice, None]:
        """Recursively splits price range until slice count <= 1000 or depth exceeded."""
        if stop_event and stop_event.is_set():
            return

        mid = (p_min + p_max) // 2

        # Sub-range 1: p_min .. mid (upper bound is strictly mid, so is_top_range=False)
        count_low = await self.get_count(
            category_id, district_id, room_enum,
            price_from=p_min if p_min > 0 else None,
            price_to=mid,
            stop_event=stop_event
        )

        if count_low > 0:
            label_low = (
                f"{district_name} | {room_enum or category_id} | "
                f"ціна {p_min or 0}..{mid} (оголошень: {count_low})"
            )
            if count_low <= MAX_OFFSET_LIMIT or depth >= self.max_bisection_depth or (mid - p_min) <= 1000:
                yield Slice(
                    category_type=category_type,
                    category_id=category_id,
                    district_id=district_id,
                    district_name=district_name,
                    room_enum=room_enum,
                    price_from=p_min if p_min > 0 else None,
                    price_to=mid,
                    visible_total_count=count_low,
                    label=label_low,
                )
            else:
                async for s in self._bisect_price(
                    category_type, category_id, district_id, district_name,
                    room_enum, p_min, mid, depth + 1, stop_event,
                    is_top_range=False,
                ):
                    yield s

        if stop_event and stop_event.is_set():
            return

        # Sub-range 2: mid + 1 .. p_max (if is_top_range, price_to is None to cover all expensive listings)
        high_price_to = None if is_top_range else p_max
        count_high = await self.get_count(
            category_id, district_id, room_enum,
            price_from=mid + 1,
            price_to=high_price_to,
            stop_event=stop_event
        )

        if count_high > 0:
            if is_top_range:
                label_high = (
                    f"{district_name} | {room_enum or category_id} | "
                    f"ціна від {mid + 1} (оголошень: {count_high})"
                )
            else:
                label_high = (
                    f"{district_name} | {room_enum or category_id} | "
                    f"ціна {mid + 1}..{p_max} (оголошень: {count_high})"
                )

            if count_high <= MAX_OFFSET_LIMIT or depth >= self.max_bisection_depth or (p_max - mid) <= 1000:
                yield Slice(
                    category_type=category_type,
                    category_id=category_id,
                    district_id=district_id,
                    district_name=district_name,
                    room_enum=room_enum,
                    price_from=mid + 1,
                    price_to=high_price_to,
                    visible_total_count=count_high,
                    label=label_high,
                )
            else:
                async for s in self._bisect_price(
                    category_type, category_id, district_id, district_name,
                    room_enum, mid + 1, p_max, depth + 1, stop_event,
                    is_top_range=is_top_range,
                ):
                    yield s

    async def generate_slices(
        self,
        category: str = "all",
        stop_event: Optional[asyncio.Event] = None,
    ) -> AsyncGenerator[Slice, None]:
        """
        Generates all slices for residential and/or commercial real estate in Kyiv.
        """
        # 1. Residential real estate
        if category in ("all", "residential"):
            for district_id, district_name in KYIV_DISTRICTS:
                if stop_event and stop_event.is_set():
                    return
                for room_enum in RESIDENTIAL_ROOM_FILTERS:
                    if stop_event and stop_event.is_set():
                        return
                    count = await self.get_count(
                        category_id=RESIDENTIAL_CATEGORY_ID,
                        district_id=district_id,
                        room_enum=room_enum,
                        stop_event=stop_event,
                    )
                    if count == 0:
                        continue

                    label = f"{district_name} | {room_enum} (оголошень: {count})"
                    if count <= MAX_OFFSET_LIMIT:
                        yield Slice(
                            category_type="residential",
                            category_id=RESIDENTIAL_CATEGORY_ID,
                            district_id=district_id,
                            district_name=district_name,
                            room_enum=room_enum,
                            visible_total_count=count,
                            label=label,
                        )
                    else:
                        logger.info(
                            "Slice exceeds %s items (%s: %s). Bisecting by price...",
                            MAX_OFFSET_LIMIT, label, count
                        )
                        async for s in self._bisect_price(
                            category_type="residential",
                            category_id=RESIDENTIAL_CATEGORY_ID,
                            district_id=district_id,
                            district_name=district_name,
                            room_enum=room_enum,
                            p_min=0,
                            p_max=300000,
                            depth=1,
                            stop_event=stop_event,
                        ):
                            yield s

        # 2. Commercial real estate
        if category in ("all", "commercial"):
            for district_id, district_name in KYIV_DISTRICTS:
                if stop_event and stop_event.is_set():
                    return
                for comm_cat_id in COMMERCIAL_CATEGORY_IDS:
                    if stop_event and stop_event.is_set():
                        return
                    count = await self.get_count(
                        category_id=comm_cat_id,
                        district_id=district_id,
                        stop_event=stop_event,
                    )
                    if count == 0:
                        continue

                    label = f"{district_name} | комерція {comm_cat_id} (оголошень: {count})"
                    if count <= MAX_OFFSET_LIMIT:
                        yield Slice(
                            category_type="commercial",
                            category_id=comm_cat_id,
                            district_id=district_id,
                            district_name=district_name,
                            visible_total_count=count,
                            label=label,
                        )
                    else:
                        logger.info(
                            "Commercial slice exceeds %s items (%s: %s). Bisecting by price...",
                            MAX_OFFSET_LIMIT, label, count
                        )
                        async for s in self._bisect_price(
                            category_type="commercial",
                            category_id=comm_cat_id,
                            district_id=district_id,
                            district_name=district_name,
                            room_enum=None,
                            p_min=0,
                            p_max=500000,
                            depth=1,
                            stop_event=stop_event,
                        ):
                            yield s


class OLXScraper:
    """
    Main background scraping manager.
    Coordinates HTTP client, slice generator, data extraction, normalization, and SQLite persistence.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        max_concurrency: int = 3,
        min_delay: float = 0.5,
        max_delay: float = 1.5,
    ):
        self.db_path = db_path
        self.client = OLXHttpClient(
            max_concurrency=max_concurrency,
            min_delay=min_delay,
            max_delay=max_delay,
        )
        self.slice_generator = SliceGenerator(self.client)
        self.stop_event = asyncio.Event()

        # State tracking
        self.is_running: bool = False
        self.session_id: Optional[int] = None
        self.category: str = "idle"
        self.status: str = "idle"  # idle, running, completed, stopped, failed
        self.total_found: int = 0
        self.processed_count: int = 0
        self.errors_count: int = 0
        self.current_slice_label: str = ""
        self.error_message: Optional[str] = None
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None

    def stop(self) -> None:
        """Signals the running scrape task to stop gracefully."""
        if self.is_running:
            logger.info("Scraper stop requested by user/system.")
            self.stop_event.set()
            self.status = "stopping"

    def get_status(self) -> Dict[str, Any]:
        """Returns the current state dictionary for API / UI polling."""
        return {
            "is_running": self.is_running,
            "session_id": self.session_id,
            "category": self.category,
            "status": self.status,
            "total_found": self.total_found,
            "processed_count": self.processed_count,
            "errors_count": self.errors_count,
            "current_slice": self.current_slice_label,
            "error_message": self.error_message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    async def run(
        self,
        category: str = "all",
        max_offers: Optional[int] = None,
        max_slices: Optional[int] = None,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Executes a scrape run for the requested category.
        category: 'residential', 'commercial', or 'all'
        max_offers: optional cap on processed offers (for testing/verification)
        max_slices: optional cap on number of slices to process
        """
        if self.is_running:
            raise RuntimeError("Scraper is already running a session.")

        self.is_running = True
        self.stop_event.clear()
        self.category = category
        self.status = "running"
        self.total_found = 0
        self.processed_count = 0
        self.errors_count = 0
        self.error_message = None
        self.started_at = datetime.now().isoformat()
        self.finished_at = None

        # Ensure DB schema exists
        init_db(self.db_path)

        # Create session record in SQLite
        self.session_id = create_scrape_session(category=category, db_path=self.db_path)
        logger.info("Started scrape session #%s for category '%s'", self.session_id, category)

        slice_counter = 0

        try:
            async for s in self.slice_generator.generate_slices(category=category, stop_event=self.stop_event):
                if self.stop_event.is_set():
                    break

                slice_counter += 1
                self.current_slice_label = s.label
                self.total_found += s.visible_total_count
                logger.info(
                    "Processing Slice #%s: %s (Total found so far: %s)",
                    slice_counter, s.label, self.total_found
                )

                # Update session progress in DB
                update_scrape_session(
                    session_id=self.session_id,
                    total_found=self.total_found,
                    processed_count=self.processed_count,
                    errors_count=self.errors_count,
                    status="running",
                    db_path=self.db_path,
                )
                if on_progress:
                    on_progress(self.get_status())

                # Traverse pages in slice (0, 40, 80... up to visible_total_count or MAX_OFFSET_LIMIT)
                max_offset = min(s.visible_total_count, MAX_OFFSET_LIMIT)
                for offset in range(0, max_offset, PAGE_LIMIT):
                    if self.stop_event.is_set():
                        break

                    query_params = s.to_query_params(offset=offset, limit=PAGE_LIMIT)
                    response_json = await self.client.request_json(
                        params=query_params,
                        stop_event=self.stop_event
                    )

                    if not response_json or "data" not in response_json:
                        break

                    offers = response_json.get("data", [])
                    if not offers:
                        break

                    # Batch normalize and persist
                    res_batch = []
                    comm_batch = []

                    for raw_offer in offers:
                        try:
                            # Apply validation & filtering (including promo ad district guard)
                            if not is_valid_rental_offer(raw_offer, s.category_type, target_district=s.district_name):
                                continue

                            raw_json = json.dumps(raw_offer, ensure_ascii=False)
                            promo = raw_offer.get("promotion") or {}
                            is_promoted = bool(promo.get("top_ad") or promo.get("highlighted") or raw_offer.get("is_promoted"))

                            if s.category_type == "residential":
                                norm_data = normalize_residential_offer(raw_offer)
                                if is_promoted and s.district_name and norm_data.get("district") and norm_data.get("district") != s.district_name:
                                    continue
                                res_batch.append((norm_data, raw_json))
                            else:
                                norm_data = normalize_commercial_offer(raw_offer)
                                if is_promoted and s.district_name and norm_data.get("district") and norm_data.get("district") != s.district_name:
                                    continue
                                comm_batch.append((norm_data, raw_json))

                        except Exception as parse_err:
                            self.errors_count += 1
                            logger.error(
                                "Error normalizing offer %s: %s",
                                raw_offer.get("url"), parse_err
                            )

                    # Save batches to SQLite
                    if res_batch:
                        upsert_residential_offers_batch(res_batch, db_path=self.db_path)
                        self.processed_count += len(res_batch)

                    if comm_batch:
                        upsert_commercial_offers_batch(comm_batch, db_path=self.db_path)
                        self.processed_count += len(comm_batch)

                    # Update session
                    update_scrape_session(
                        session_id=self.session_id,
                        total_found=self.total_found,
                        processed_count=self.processed_count,
                        errors_count=self.errors_count,
                        status="running",
                        db_path=self.db_path,
                    )
                    if on_progress:
                        on_progress(self.get_status())

                    # Check max offers limit for verification/tests
                    if max_offers and self.processed_count >= max_offers:
                        logger.info("Reached target max_offers limit: %s", max_offers)
                        self.stop_event.set()
                        break

                if max_slices and slice_counter >= max_slices:
                    logger.info("Reached target max_slices limit: %s", max_slices)
                    break

            if self.stop_event.is_set() and self.status == "stopping":
                self.status = "stopped"
            else:
                self.status = "completed"
                if max_offers and self.processed_count >= max_offers:
                    self.total_found = self.processed_count
                else:
                    self.total_found = max(self.total_found, self.processed_count)

        except Exception as exc:
            logger.exception("Scraper session failed with exception: %s", exc)
            self.status = "failed"
            self.error_message = str(exc)

        finally:
            self.is_running = False
            self.finished_at = datetime.now().isoformat()
            await self.client.close()

            update_scrape_session(
                session_id=self.session_id,
                total_found=self.total_found,
                processed_count=self.processed_count,
                errors_count=self.errors_count,
                status=self.status,
                error_message=self.error_message,
                db_path=self.db_path,
            )
            logger.info(
                "Scrape session #%s finished with status '%s'. Processed: %s, Errors: %s",
                self.session_id, self.status, self.processed_count, self.errors_count
            )
            if on_progress:
                on_progress(self.get_status())

        return self.get_status()


# Module-level singleton instance for application use
_scraper_instance: Optional[OLXScraper] = None


def get_scraper_instance(db_path: Optional[str] = None) -> OLXScraper:
    """Returns the singleton instance of OLXScraper."""
    global _scraper_instance
    if _scraper_instance is None:
        _scraper_instance = OLXScraper(db_path=db_path)
    return _scraper_instance


if __name__ == "__main__":
    import argparse

    cli_parser = argparse.ArgumentParser(description="OLX Kyiv Real Estate Scraper")
    cli_parser.add_argument(
        "--category",
        choices=["residential", "commercial", "all"],
        default="all",
        help="Category to scrape",
    )
    cli_parser.add_argument(
        "--max-offers",
        type=int,
        default=None,
        help="Optional maximum number of offers to process (for testing)",
    )
    cli_parser.add_argument(
        "--max-slices",
        type=int,
        default=None,
        help="Optional maximum number of slices to process",
    )
    args = cli_parser.parse_args()

    scraper = OLXScraper()
    asyncio.run(
        scraper.run(
            category=args.category,
            max_offers=args.max_offers,
            max_slices=args.max_slices,
        )
    )
