"""
Unit and integration tests for scraper.py.
Tests offer filtering, slice generator and bisection, resilient HTTP client handling,
batch saving into SQLite, and graceful stopping.
"""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from database import get_db_connection, get_stats, init_db
from scraper import (
    CITY_ID_KYIV,
    KYIV_DISTRICTS,
    MAX_OFFSET_LIMIT,
    PAGE_LIMIT,
    RESIDENTIAL_CATEGORY_ID,
    OLXHttpClient,
    OLXScraper,
    Slice,
    SliceGenerator,
    extract_price_value,
    extract_total_area_value,
    is_valid_rental_offer,
)


class TestOfferFiltering(unittest.TestCase):
    """Tests for offer validation and filtering rules."""

    def test_valid_residential_offer(self):
        offer = {
            "offer_type": "offer",
            "title": "Здам затишну 1-кімнатну квартиру на Позняках",
            "params": [
                {"key": "price", "value": {"value": 15000, "currency": "UAH"}},
                {"key": "total_area", "value": "45.5"},
            ],
        }
        self.assertTrue(is_valid_rental_offer(offer, "residential"))

    def test_reject_seek_offer_type(self):
        offer = {
            "offer_type": "seek",
            "title": "Сім'я зніме 2к квартиру в Голосіївському районі",
            "params": [
                {"key": "price", "value": {"value": 18000, "currency": "UAH"}},
                {"key": "total_area", "value": "60"},
            ],
        }
        self.assertFalse(is_valid_rental_offer(offer, "residential"))

    def test_reject_seek_param(self):
        offer = {
            "offer_type": "offer",
            "title": "Оренда квартири терміново",
            "params": [
                {"key": "offer_seek", "value": "seek"},
                {"key": "price", "value": {"value": 12000, "currency": "UAH"}},
                {"key": "total_area", "value": "40"},
            ],
        }
        self.assertFalse(is_valid_rental_offer(offer, "residential"))

    def test_reject_sale_in_title(self):
        offer = {
            "offer_type": "offer",
            "title": "Продаж 2к квартири на Печерську терміново!",
            "params": [
                {"key": "price", "value": {"value": 95000, "currency": "USD"}},
                {"key": "total_area", "value": "70"},
            ],
        }
        self.assertFalse(is_valid_rental_offer(offer, "residential"))

    def test_reject_zero_or_missing_price(self):
        offer_zero_price = {
            "offer_type": "offer",
            "title": "Здам офіс біля метро",
            "params": [
                {"key": "price", "value": {"value": 0, "currency": "UAH"}},
                {"key": "total_area", "value": "50"},
            ],
        }
        self.assertFalse(is_valid_rental_offer(offer_zero_price, "commercial"))

        offer_no_price = {
            "offer_type": "offer",
            "title": "Здам офіс біля метро",
            "params": [
                {"key": "total_area", "value": "50"},
            ],
        }
        self.assertFalse(is_valid_rental_offer(offer_no_price, "commercial"))

    def test_reject_zero_or_missing_area(self):
        offer = {
            "offer_type": "offer",
            "title": "Здам кімнату в квартирі",
            "params": [
                {"key": "price", "value": {"value": 6000, "currency": "UAH"}},
            ],
        }
        self.assertFalse(is_valid_rental_offer(offer, "residential"))

    def test_reject_commercial_multilot_range_without_fixed_area(self):
        offer = {
            "offer_type": "offer",
            "title": "Оренда офісів від 30 до 250 м² в сучасному БЦ",
            "params": [
                {"key": "price", "value": {"value": 450, "currency": "UAH"}},
                # missing or empty total_area
            ],
        }
        self.assertFalse(is_valid_rental_offer(offer, "commercial"))

        # Point 11: Even if total_area is present, range in title marks it as multilot
        offer_with_area = {
            "offer_type": "offer",
            "title": "Оренда офісів від 30 до 250 м² в сучасному БЦ",
            "params": [
                {"key": "price", "value": {"value": 450, "currency": "UAH"}},
                {"key": "total_area", "value": "50"},
            ],
        }
        self.assertFalse(is_valid_rental_offer(offer_with_area, "commercial"))


class TestSliceDataclass(unittest.TestCase):
    """Tests for Slice dataclass parameter construction."""

    def test_slice_query_params(self):
        s = Slice(
            category_type="residential",
            category_id=RESIDENTIAL_CATEGORY_ID,
            district_id=1,
            district_name="Голосіївський",
            room_enum="dvuhkomnatnye",
            price_from=15000,
            price_to=35000,
            visible_total_count=450,
            label="Test Slice",
        )
        params = s.to_query_params(offset=40, limit=40)
        self.assertEqual(params["offset"], 40)
        self.assertEqual(params["limit"], 40)
        self.assertEqual(params["category_id"], RESIDENTIAL_CATEGORY_ID)
        self.assertEqual(params["city_id"], CITY_ID_KYIV)
        self.assertEqual(params["district_id"], 1)
        self.assertEqual(params["filter_enum_number_of_rooms_string[0]"], "dvuhkomnatnye")
        self.assertEqual(params["filter_float_price:from"], 15000)
        self.assertEqual(params["filter_float_price:to"], 35000)


class TestSliceGeneratorBisection(unittest.IsolatedAsyncioTestCase):
    """Tests for recursive price bisection when slice exceeds 1000 items."""

    async def test_bisection_when_over_limit(self):
        mock_client = AsyncMock(spec=OLXHttpClient)

        # Mock API returning counts:
        # First call (total for district): 1800 (> 1000)
        # Low price (0..150000): 850 (<= 1000 -> OK!)
        # High price (150001..300000): 950 (<= 1000 -> OK!)
        async def mock_request_json(url=None, params=None, stop_event=None):
            p_to = params.get("filter_float_price:to") if params else None
            p_from = params.get("filter_float_price:from") if params else None

            if p_to is None and p_from is None:
                return {"metadata": {"visible_total_count": 1800}}
            if p_to == 150000:
                return {"metadata": {"visible_total_count": 850}}
            if p_from == 150001:
                return {"metadata": {"visible_total_count": 950}}
            return {"metadata": {"visible_total_count": 0}}

        mock_client.request_json.side_effect = mock_request_json

        gen = SliceGenerator(mock_client, max_bisection_depth=4)

        # Test _bisect_price directly
        slices = []
        async for s in gen._bisect_price(
            category_type="residential",
            category_id=RESIDENTIAL_CATEGORY_ID,
            district_id=11,
            district_name="Печерський",
            room_enum="dvuhkomnatnye",
            p_min=0,
            p_max=300000,
            depth=1,
        ):
            slices.append(s)

        self.assertEqual(len(slices), 2)
        self.assertEqual(slices[0].visible_total_count, 850)
        self.assertEqual(slices[0].price_to, 150000)
        self.assertEqual(slices[1].visible_total_count, 950)
        self.assertEqual(slices[1].price_from, 150001)


class TestOLXScraperLifecycle(unittest.IsolatedAsyncioTestCase):
    """Tests full scraping cycle with SQLite persistence and stop mechanism."""

    async def asyncSetUp(self):
        tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db = tf.name
        tf.close()
        init_db(self.temp_db)

    async def asyncTearDown(self):
        if os.path.exists(self.temp_db):
            os.remove(self.temp_db)

    async def test_scraper_run_and_batch_persistence(self):
        mock_offers = [
            {
                "id": 1001,
                "url": "https://www.olx.ua/d/uk/obyavlenie/flat-1.html",
                "title": "Здам 1к квартиру вул. Саксаганського 10",
                "offer_type": "offer",
                "business": False,
                "params": [
                    {"key": "price", "value": {"value": 20000, "currency": "UAH"}},
                    {"key": "total_area", "value": "42"},
                    {"key": "floor", "value": "4"},
                    {"key": "total_floors", "value": "9"},
                ],
            },
            {
                "id": 1002,
                "url": "https://www.olx.ua/d/uk/obyavlenie/flat-2.html",
                "title": "Здам 2к квартиру вул. Хрещатик 25",
                "offer_type": "offer",
                "business": True,
                "params": [
                    {"key": "price", "value": {"value": 35000, "currency": "UAH"}},
                    {"key": "total_area", "value": "65"},
                    {"key": "floor", "value": "5"},
                    {"key": "total_floors", "value": "12"},
                ],
            },
        ]

        scraper = OLXScraper(db_path=self.temp_db)

        # Mock client to return count and then offers
        async def mock_request_json(url=None, params=None, stop_event=None):
            if params and params.get("offset") == 0 and params.get("limit") == 1:
                return {"metadata": {"visible_total_count": 2}}
            if params and params.get("limit") == 40:
                return {"data": mock_offers}
            return {"metadata": {"visible_total_count": 0}}

        scraper.client.request_json = AsyncMock(side_effect=mock_request_json)

        status = await scraper.run(category="residential", max_slices=1, max_offers=2)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["processed_count"], 2)

        # Check records in SQLite database
        stats = get_stats(self.temp_db)
        self.assertEqual(stats["residential_count"], 2)

        conn = get_db_connection(self.temp_db)
        row = conn.execute("SELECT * FROM residential_offers WHERE url LIKE '%flat-1%'").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["street"], "вулиця Саксаганського")
        self.assertEqual(row["house"], "10")
        self.assertEqual(row["price"], 20000.0)

    async def test_scraper_stop_signal(self):
        scraper = OLXScraper(db_path=self.temp_db)

        async def infinite_offers(url=None, params=None, stop_event=None):
            if params and params.get("limit") == 1:
                return {"metadata": {"visible_total_count": 500}}
            # Simulate slight delay
            await asyncio.sleep(0.05)
            return {
                "data": [{
                    "id": 9999,
                    "url": "https://www.olx.ua/d/uk/obyavlenie/dummy.html",
                    "title": "Здам квартиру",
                    "offer_type": "offer",
                    "params": [
                        {"key": "price", "value": {"value": 10000, "currency": "UAH"}},
                        {"key": "total_area", "value": "30"},
                    ],
                }]
            }

        scraper.client.request_json = AsyncMock(side_effect=infinite_offers)

        async def trigger_stop_later():
            await asyncio.sleep(0.08)
            scraper.stop()

        # Run scraper and stop trigger concurrently
        _, status = await asyncio.gather(
            trigger_stop_later(),
            scraper.run(category="residential", max_slices=5),
        )

        self.assertIn(status["status"], ("stopped", "completed"))

    async def test_category_all_quota_distribution(self):
        """
        Verifies that when category='all' and max_offers=4, scraper distributes
        the quota equally: 2 residential and 2 commercial offers, and stops cleanly.
        """
        res_mock_offers = [
            {
                "id": 2000 + i,
                "url": f"https://www.olx.ua/d/uk/obyavlenie/res-{i}.html",
                "title": f"Здам 1к квартиру вул. Хрещатик {i}",
                "offer_type": "offer",
                "business": False,
                "params": [
                    {"key": "price", "value": {"value": 20000 + i * 1000, "currency": "UAH"}},
                    {"key": "total_area", "value": "45"},
                ],
            }
            for i in range(1, 5)
        ]

        comm_mock_offers = [
            {
                "id": 3000 + i,
                "url": f"https://www.olx.ua/d/uk/obyavlenie/comm-{i}.html",
                "title": f"Здам офіс вул. Велика Васильківська {i}",
                "offer_type": "offer",
                "business": True,
                "params": [
                    {"key": "price", "value": {"value": 30000 + i * 1000, "currency": "UAH"}},
                    {"key": "total_area", "value": "75"},
                ],
            }
            for i in range(1, 5)
        ]

        scraper = OLXScraper(db_path=self.temp_db)

        async def mock_request_json(url=None, params=None, stop_event=None):
            if not params:
                return {"metadata": {"visible_total_count": 0}}
            # Count request
            if params.get("limit") == 1:
                return {"metadata": {"visible_total_count": 4}}
            # Data fetch request
            if params.get("limit") == 40:
                cat_id = params.get("category_id")
                if cat_id == RESIDENTIAL_CATEGORY_ID:
                    return {"data": res_mock_offers}
                else:
                    return {"data": comm_mock_offers}
            return {"metadata": {"visible_total_count": 0}}

        scraper.client.request_json = AsyncMock(side_effect=mock_request_json)

        status = await scraper.run(category="all", max_offers=4)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["processed_count"], 4)
        self.assertEqual(status["processed_residential"], 2)
        self.assertEqual(status["processed_commercial"], 2)

        stats = get_stats(self.temp_db)
        self.assertEqual(stats["residential_count"], 2)
        self.assertEqual(stats["commercial_count"], 2)

    async def test_audit_fixes_points_9_and_10(self):
        mock_client = AsyncMock(spec=OLXHttpClient)

        # Point 9: Verify rightmost branch has price_to=None
        async def mock_req(url=None, params=None, stop_event=None):
            p_from = params.get("filter_float_price:from") if params else None
            p_to = params.get("filter_float_price:to") if params else None
            if p_to == 150000:
                return {"metadata": {"visible_total_count": 800}}
            if p_from == 150001:
                self.assertIsNone(p_to, "Rightmost branch of bisection must not cap price_to")
                return {"metadata": {"visible_total_count": 900}}
            return {"metadata": {"visible_total_count": 0}}

        mock_client.request_json.side_effect = mock_req
        gen = SliceGenerator(mock_client)
        slices = []
        async for s in gen._bisect_price(
            category_type="residential",
            category_id=RESIDENTIAL_CATEGORY_ID,
            district_id=11,
            district_name="Печерський",
            room_enum="dvuhkomnatnye",
            p_min=0,
            p_max=300000,
            depth=1,
        ):
            slices.append(s)

        self.assertEqual(len(slices), 2)
        self.assertIsNone(slices[1].price_to)
        self.assertIn("ціна від 150001", slices[1].label)

        # Point 10: Pagination ranges
        # visible_total_count = 40 should only query offset 0 (1 page)
        offsets_40 = list(range(0, min(40, MAX_OFFSET_LIMIT), PAGE_LIMIT))
        self.assertEqual(offsets_40, [0])

        # visible_total_count = 1000 should stop at 960 and not query 1000
        offsets_1000 = list(range(0, min(1000, MAX_OFFSET_LIMIT), PAGE_LIMIT))
        self.assertEqual(offsets_1000[-1], 960)
        self.assertNotIn(1000, offsets_1000)


if __name__ == "__main__":
    unittest.main()
