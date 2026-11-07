"""
Asynchronous Scraper for Rieltor.ua Kyiv Real Estate (Stage 3).

Features:
- Resilient HTTP client using curl_cffi AsyncSession with Chrome impersonation.
- Concurrency limiting (asyncio.Semaphore) and randomized polite delays (0.5–1.5s).
- Exponential backoff on rate limits (HTTP 429), server errors (5xx), and network timeouts.
- Kyiv District Slicing across all 10 official Kyiv administrative districts.
- Support for Residential ('flats-rent') and Commercial ('commercials-rent') real estate.
- Robust semantic HTML card parser extracting:
    * Exact listing ID and canonical URL
    * Precise address (street + house) resolved via KyivAddressParser
    * Residential complex (ЖК) detection and Stage 2 fallback rule
    * Price and currency (UAH, USD, EUR)
    * Rooms, total area, living area, kitchen area, floor, total floors, year built
    * Description feature extraction (generator, inverter, renovation, appliances, pets, kids)
    * Commercial attributes (property_type, price_basis, business center name, power, ceiling, etc.)
- Strict conditional clearing for commercial properties according to customer's ML schema.
- Continuous batch persistence into SQLite with deduplication and source='rieltor'.
- Full scrape session lifecycle and progress tracking for Web UI and Orchestrator.
"""

import asyncio
from datetime import datetime, timedelta
import html
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.parse

from curl_cffi.requests import AsyncSession

from database import (
    create_scrape_session,
    init_db,
    update_scrape_session,
    upsert_commercial_offers_batch,
    upsert_residential_offers_batch,
)
from normalizer import (
    RieltorNormalizer,
    get_address_parser,
    normalize_rieltor_commercial_offer,
    normalize_rieltor_residential_offer,
)

logger = logging.getLogger("rieltor_scraper")
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


# 10 Official Kyiv administrative districts with Rieltor IDs and URL slugs
RIELTOR_KYIV_DISTRICTS: List[Tuple[int, str, str]] = [
    (81, "Голосіївський", "Голосіївський-d81"),
    (85, "Дарницький", "Дарницький-d85"),
    (83, "Деснянський", "Деснянський-d83"),
    (84, "Дніпровський", "Дніпровський-d84"),
    (76, "Оболонський", "Оболонський-d76"),
    (82, "Печерський", "Печерський-d82"),
    (78, "Подільський", "Подільський-d78"),
    (86, "Святошинський", "Святошинський-d86"),
    (80, "Солом'янський", "Солом'янський-d80"),
    (79, "Шевченківський", "Шевченківський-d79"),
]

BASE_URL_RESIDENTIAL = "https://rieltor.ua/flats-rent/"
BASE_URL_COMMERCIAL = "https://rieltor.ua/commercials-rent/"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
}


def clean_html_text(text: Optional[str]) -> str:
    """Strips HTML tags and normalizes consecutive whitespace."""
    if not text:
        return ""
    s = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(s).split())


def parse_rieltor_price(price_str: Optional[str]) -> Tuple[Optional[float], str]:
    """Extracts numeric price and currency ISO code from Rieltor price string."""
    if not price_str:
        return None, "UAH"
    cleaned = price_str.lower().replace(" ", "").replace("\xa0", "")
    cur = "UAH"
    if "$" in cleaned or "usd" in cleaned or "дол" in cleaned:
        cur = "USD"
    elif "€" in cleaned or "eur" in cleaned or "євр" in cleaned:
        cur = "EUR"
    elif "грн" in cleaned or "uah" in cleaned:
        cur = "UAH"

    digits = re.search(r"(\d+(?:\.\d+)?)", cleaned.replace(",", "."))
    val = float(digits.group(1)) if digits else None
    return val, cur


def parse_rieltor_update_date(update_str: Optional[str]) -> str:
    """Parses Rieltor date relative expressions into YYYY-MM-DD."""
    today = datetime.now()
    if not update_str:
        return today.strftime("%Y-%m-%d")
    u = update_str.lower().strip()
    if "сьогодні" in u:
        return today.strftime("%Y-%m-%d")
    if "вчора" in u:
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")

    months = {
        "січ": 1, "лют": 2, "бер": 3, "квіт": 4, "трав": 5, "черв": 6,
        "лип": 7, "серп": 8, "вер": 9, "жовт": 10, "лист": 11, "груд": 12
    }
    m = re.search(r"(\d{1,2})\s+([а-яіїє]+)", u)
    if m:
        day = int(m.group(1))
        m_name = m.group(2)[:4]
        for k, v in months.items():
            if m_name.startswith(k):
                try:
                    return datetime(today.year, v, day).strftime("%Y-%m-%d")
                except ValueError:
                    pass
    return today.strftime("%Y-%m-%d")


@dataclass
class RieltorSlice:
    """Represents an atomic scrape slice (a category and a Kyiv district)."""
    category_type: str  # 'residential' or 'commercial'
    district_id: int
    district_name: str
    district_slug: str
    label: str

    def get_url(self, page: int = 1) -> str:
        base = BASE_URL_RESIDENTIAL if self.category_type == "residential" else BASE_URL_COMMERCIAL
        slug_encoded = urllib.parse.quote(self.district_slug)
        if page <= 1:
            return f"{base}{slug_encoded}/"
        return f"{base}{slug_encoded}/?page={page}"


class RieltorHttpClient:
    """
    Resilient asynchronous HTTP client for Rieltor.ua using curl_cffi with Chrome TLS impersonation.
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
        self.proxy: Optional[str] = proxy or os.getenv("RIELTOR_PROXY") or os.getenv("OLX_PROXY")
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
                            "Rieltor HTTP %s on %s attempt %s/%s. Backing off for %.2fs...",
                            response.status_code,
                            url,
                            attempt,
                            self.max_retries,
                            sleep_time,
                        )
                        await asyncio.sleep(sleep_time)
                        continue

                    # 404 Not Found (e.g. beyond last page)
                    if response.status_code == 404:
                        return None

                    logger.error("Rieltor HTTP error %s for %s", response.status_code, url)
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


class RieltorCardParser:
    """
    Parses Rieltor HTML listing catalog cards into structured data and normalized models.
    """

    def __init__(self, normalizer: Optional[RieltorNormalizer] = None):
        self.normalizer = normalizer or RieltorNormalizer(get_address_parser())

    @staticmethod
    def extract_cards(html_content: str) -> List[str]:
        """Extracts individual .catalog-card HTML chunks from page HTML."""
        if not html_content:
            return []
        chunks = re.split(r"(?=<div class=[\"']catalog-card[\s\"'])", html_content)
        return [c for c in chunks if c.startswith("<div class=\"catalog-card") or c.startswith("<div class='catalog-card")]

    def parse_card(
        self,
        card_html: str,
        category_type: str,
        district_hint: str = "",
        seen_date: Optional[str] = None,
    ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """
        Parses a single .catalog-card HTML snippet.
        Returns a tuple: (normalized_offer_dict, raw_card_dict).
        """
        m_id = re.search(r"data-catalog-item-id=[\"'](\d+)[\"']", card_html)
        item_id = m_id.group(1) if m_id else ""

        cat_slug = "flats-rent" if category_type == "residential" else "commercials-rent"
        m_url = re.search(r"<a[^>]+href=[\"'](https://rieltor\.ua/[^\"']+)[\"'][^>]+class=[\"'][^\"']*catalog-card-media", card_html)
        if not m_url:
            m_url = re.search(r"<a[^>]+class=[\"'][^\"']*catalog-card-media[^\"']*[^>]+href=[\"'](https://rieltor\.ua/[^\"']+)[\"']", card_html)
        url = m_url.group(1) if m_url else (f"https://rieltor.ua/{cat_slug}/view/{item_id}/" if item_id else "")

        if not url:
            return None

        m_price = re.search(r"<strong[^>]+class=[\"'][^\"']*catalog-card-price-title[^\"']*[\"'][^>]*>(.*?)</strong>", card_html, re.DOTALL)
        price_text = clean_html_text(m_price.group(1)) if m_price else ""
        price_val, currency = parse_rieltor_price(price_text)

        m_addr = re.search(r"<div[^>]+class=[\"'][^\"']*catalog-card-address[^\"']*[\"'][^>]*>(.*?)</div>", card_html, re.DOTALL)
        address_text = clean_html_text(m_addr.group(1)) if m_addr else ""

        m_reg = re.search(r"<div[^>]+class=[\"'][^\"']*catalog-card-region[^\"']*[\"'][^>]*>(.*?)</div>", card_html, re.DOTALL)
        region_text = clean_html_text(m_reg.group(1)) if m_reg else ""

        chips = [clean_html_text(c) for c in re.findall(r"<a[^>]+class=[\"'][^\"']*catalog-card-chip[^\"']*[\"'][^>]*>(.*?)</a>", card_html, re.DOTALL)]
        details = [clean_html_text(d) for d in re.findall(r"<div[^>]+class=[\"'][^\"']*catalog-card-details-row[^\"']*[\"'][^>]*>(.*?)</div>", card_html, re.DOTALL)]

        m_desc = re.search(r"<div[^>]+class=[\"'][^\"']*catalog-card-description[^\"']*[\"'][^>]*>(.*?)</div>", card_html, re.DOTALL)
        description = clean_html_text(m_desc.group(1)) if m_desc else ""

        m_upd = re.search(r"<div[^>]+class=[\"'][^\"']*catalog-card-update[^\"']*[\"'][^>]*>(.*?)</div>", card_html, re.DOTALL)
        update_text = clean_html_text(m_upd.group(1)) if m_upd else ""
        effective_seen_date = seen_date or parse_rieltor_update_date(update_text)

        m_author = re.search(r"<div[^>]+class=[\"'][^\"']*catalog-card-author[^\"']*[\"'][^>]*>(.*?)</div>", card_html, re.DOTALL)
        author = clean_html_text(m_author.group(1)) if m_author else ""

        m_agency = re.search(r"<div[^>]+class=[\"'][^\"']*catalog-card-agency[^\"']*[\"'][^>]*>(.*?)</div>", card_html, re.DOTALL)
        agency = clean_html_text(m_agency.group(1)) if m_agency else ""

        m_lat = re.search(r"data-latitude=[\"']([^\"']+)[\"']", card_html)
        m_lon = re.search(r"data-longitude=[\"']([^\"']+)[\"']", card_html)
        latitude = m_lat.group(1) if m_lat else ""
        longitude = m_lon.group(1) if m_lon else ""

        # Determine district hint: if region_text has a district, use it; otherwise use passed district_hint
        effective_district = district_hint
        for d in ("Голосіївський", "Дарницький", "Деснянський", "Дніпровський", "Оболонський", "Печерський", "Подільський", "Святошинський", "Солом", "Шевченківський"):
            if d.lower() in region_text.lower():
                effective_district = "Солом'янський" if d == "Солом" else d
                break

        # Parse rooms, area, floor, built_year from details list
        rooms = None
        area_total = None
        area_kitchen = None
        floor = None
        floors_total = None
        built_year = None
        property_type_raw = ""

        for d_item in details:
            # Rooms
            m_r = re.search(r"(\d+)\s*кімнат", d_item)
            if m_r:
                rooms = int(m_r.group(1))

            # Area
            m_a = re.search(r"(\d+(?:\.\d+)?)(?:\s*/\s*(\d+(?:\.\d+)?))?(?:\s*/\s*(\d+(?:\.\d+)?))?\s*м[²2]?", d_item)
            if m_a:
                area_total = float(m_a.group(1))
                if m_a.group(3):
                    area_kitchen = float(m_a.group(3))

            # Floor
            m_fl = re.search(r"поверх\s*(\d+)(?:\s*з\s*(\d+))?", d_item)
            if m_fl:
                floor = int(m_fl.group(1))
                if m_fl.group(2):
                    floors_total = int(m_fl.group(2))

            # Built year
            m_yr = re.search(r"(\d{4})\s*рік", d_item)
            if m_yr:
                built_year = int(m_yr.group(1))
            elif category_type == "commercial" and not m_a and not m_fl:
                property_type_raw = d_item

        raw_card = {
            "id": item_id,
            "url": url,
            "price": price_val,
            "currency": currency,
            "price_text": price_text,
            "address_text": address_text,
            "region_text": region_text,
            "district_hint": effective_district,
            "chips": chips,
            "details": details,
            "description": description,
            "seen_date": effective_seen_date,
            "author": author,
            "agency": agency,
            "rooms": rooms,
            "area_total": area_total,
            "area_kitchen": area_kitchen,
            "floor": floor,
            "floors_total": floors_total,
            "built_year": built_year,
            "property_type_raw": property_type_raw,
            "latitude": latitude,
            "longitude": longitude,
        }

        if category_type == "residential":
            normalized = self.normalizer.normalize_residential(raw_card, effective_seen_date)
        else:
            normalized = self.normalizer.normalize_commercial(raw_card, effective_seen_date)

        return normalized, raw_card


class RieltorScraper:
    """
    Main background scraping manager for Rieltor.ua.
    Coordinates HTTP client, district slice traversal, card parsing, normalization, and SQLite persistence.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        max_concurrency: int = 2,
        min_delay: float = 0.5,
        max_delay: float = 1.5,
        proxy: Optional[str] = None,
    ):
        self.db_path = db_path
        self.client = RieltorHttpClient(
            max_concurrency=max_concurrency,
            min_delay=min_delay,
            max_delay=max_delay,
            proxy=proxy,
        )
        self.parser = RieltorCardParser()
        self.stop_event = asyncio.Event()

        # State tracking
        self.is_running: bool = False
        self.session_id: Optional[int] = None
        self.category: str = "idle"
        self.status: str = "idle"  # idle, running, completed, stopped, failed
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
        """Signals the running scrape task to stop gracefully."""
        if self.is_running:
            logger.info("Rieltor scraper stop requested by user/system.")
            self.stop_event.set()
            self.status = "stopping"

    def get_status(self) -> Dict[str, Any]:
        """Returns the current state dictionary for API / UI polling."""
        masked_proxy = None
        if self.client.proxy:
            p = self.client.proxy
            if "@" in p:
                prefix, hostpart = p.split("@", 1)
                proto = prefix.split("://")[0] if "://" in prefix else "http"
                masked_proxy = f"{proto}://***@{hostpart}"
            else:
                masked_proxy = p

        return {
            "source": "rieltor",
            "is_running": self.is_running,
            "session_id": self.session_id,
            "category": self.category,
            "status": self.status,
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

    def generate_slices(self, category: str = "all") -> List[RieltorSlice]:
        """Generates the list of slices based on the requested category."""
        slices: List[RieltorSlice] = []

        if category in ("all", "residential"):
            for d_id, d_name, d_slug in RIELTOR_KYIV_DISTRICTS:
                label = f"{d_name} (житлова)"
                slices.append(RieltorSlice(
                    category_type="residential",
                    district_id=d_id,
                    district_name=d_name,
                    district_slug=d_slug,
                    label=label,
                ))

        if category in ("all", "commercial"):
            for d_id, d_name, d_slug in RIELTOR_KYIV_DISTRICTS:
                label = f"{d_name} (комерція)"
                slices.append(RieltorSlice(
                    category_type="commercial",
                    district_id=d_id,
                    district_name=d_name,
                    district_slug=d_slug,
                    label=label,
                ))

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
        Executes a scrape run on Rieltor.ua for the requested category.
        category: 'residential', 'commercial', or 'all'
        max_offers: optional cap on processed offers (for testing/verification)
        max_slices: optional cap on number of slices to process
        on_progress: optional callback function triggered when stats update
        proxy: optional proxy URL
        """
        if self.is_running:
            raise RuntimeError("Rieltor scraper is already running a session.")

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
            self.session_id = create_scrape_session(f"rieltor_{category}", self.db_path)
        except Exception as ex:
            logger.error("Failed to create scrape session: %s", ex)
            self.session_id = None

        logger.info(
            "Started Rieltor scrape session #%s for category '%s'",
            self.session_id, category
        )

        slices = self.generate_slices(category)
        if max_slices is not None:
            slices = slices[:max_slices]

        slices_processed = 0

        try:
            for s_idx, current_slice in enumerate(slices, start=1):
                if self.stop_event.is_set():
                    break

                self.current_slice_label = current_slice.label
                logger.info("Processing Slice #%s: %s", s_idx, current_slice.label)

                page = 1
                while not self.stop_event.is_set():
                    page_url = current_slice.get_url(page)
                    html_content = await self.client.request_html(page_url, self.stop_event)

                    if not html_content:
                        break

                    card_snippets = self.parser.extract_cards(html_content)
                    if not card_snippets:
                        # Reached the end of this slice
                        break

                    self.total_found += len(card_snippets)

                    batch_offers: List[Tuple[Dict[str, Any], str]] = []
                    for c_html in card_snippets:
                        try:
                            parsed_tuple = self.parser.parse_card(
                                c_html,
                                category_type=current_slice.category_type,
                                district_hint=current_slice.district_name,
                            )
                            if parsed_tuple:
                                norm_dict, raw_dict = parsed_tuple
                                raw_json = json.dumps(raw_dict, ensure_ascii=False)
                                batch_offers.append((norm_dict, raw_json))
                        except Exception as parse_err:
                            self.errors_count += 1
                            logger.warning("Error parsing Rieltor card: %s", parse_err)

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
                            logger.error("Failed to upsert Rieltor batch into database: %s", db_err)

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
                    if max_offers is not None and self.processed_count >= max_offers:
                        logger.info("Reached target max_offers limit: %s", max_offers)
                        break

                    page += 1

                slices_processed += 1
                if max_offers is not None and self.processed_count >= max_offers:
                    break

            if self.stop_event.is_set():
                self.status = "stopped"
            else:
                self.status = "completed"

        except Exception as fatal_ex:
            self.status = "failed"
            self.error_message = str(fatal_ex)
            self.errors_count += 1
            logger.exception("Fatal error in Rieltor scrape workflow: %s", fatal_ex)
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
                "Rieltor scrape session #%s finished with status '%s'. Processed: %s, Errors: %s",
                self.session_id, self.status, self.processed_count, self.errors_count
            )

        return self.get_status()
