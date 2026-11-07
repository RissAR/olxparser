"""
Unit tests for Stage 4: LUN.ua scraper, RSC payload parser, and normalizer.
"""

import asyncio
from datetime import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import database
from lun_scraper import (
    BASE_URL_COMMERCIAL,
    BASE_URL_RESIDENTIAL,
    LUN_KYIV_DISTRICTS,
    LunCardParser,
    LunHttpClient,
    LunScraper,
    LunSlice,
)
from normalizer import (
    LunNormalizer,
    get_address_parser,
    normalize_lun_commercial_offer,
    normalize_lun_residential_offer,
)

SAMPLE_RESIDENTIAL_CARD_DICT = {
    "id": 4731279531,
    "urlRaw": "https://lun.ua/uk/realty/4731279531",
    "price": 25000,
    "currency": "uah",
    "isOwner": False,
    "roomCount": 2,
    "areaTotal": 65.5,
    "areaKitchen": 14.0,
    "floor": 8,
    "floorCount": 25,
    "builtYear": 2018,
    "houseTypeName": "monolith",
    "header": "Оренда 2к квартири на вул. Дніпровська набережна, 14",
    "text": "Простора та світла квартира з ремонтом. Вся побутова техніка: посудомийна машина, бойлер, кондиціонер. Встановлено інвертор. У будинку працює генератор на ліфти.",
    "geoEntities": [
        {"type": "city", "name": "Київ"},
        {"type": "district", "name": "Дарницький"},
        {"type": "street", "name": "Дніпровська набережна"},
        {"type": "house", "name": "14"},
        {"type": "residential_complex", "name": "ЖК RiverStone"},
    ],
    "autonomyLift": True,
    "autonomyPower": True,
    "petsAllowedFull": "yes",
    "childrenAllowed": True,
    "withoutCommission": True,
    "insertTime": "2026-11-05 14:00:00",
}

SAMPLE_RESIDENTIAL_JK_FALLBACK_CARD_DICT = {
    "id": 4731350629,
    "urlRaw": "https://lun.ua/uk/realty/4731350629",
    "price": 1200,
    "currency": "usd",
    "isOwner": True,
    "roomCount": 1,
    "areaTotal": 48.0,
    "areaKitchen": 12.0,
    "floor": 12,
    "floorCount": 24,
    "builtYear": 2021,
    "header": "Оренда видової 1-кімнатної квартири",
    "text": "Квартира в престижному комплексі. Авторський ремонт. Без тварин. Без дітей.",
    "geoEntities": [
        {"type": "city", "name": "Київ"},
        {"type": "district", "name": "Голосіївський"},
        {"type": "street", "name": "вулиця Саперно-Слобідська"},
        {"type": "residential_complex", "name": "ЖК Саперно-Слобідський"},
    ],
    "petsAllowedFull": "no",
    "childrenAllowed": False,
    "withoutCommission": True,
    "insertTime": "2026-11-06 10:30:00",
}

SAMPLE_COMMERCIAL_CARD_DICT = {
    "id": 4731324937,
    "urlRaw": "https://rieltor.ua/commercials-rent/view/13200256/",  # third-party URL to test canonical protection
    "price": 50000,
    "currency": "uah",
    "isOwner": False,
    "areaTotal": 120.0,
    "floor": 1,
    "floorCount": 5,
    "ceilingHeight": 3.4,
    "aim": [{"name": "Офіс", "id": 1}],
    "header": "Оренда фасадного офісу 120 м²",
    "text": "Фасадне приміщення, перший поверх, вільне призначення. Потужність 30 кВт. Окремий вхід, панорамні вітрини. Встановлено генератор. Є підземне укриття.",
    "geoEntities": [
        {"type": "city", "name": "Київ"},
        {"type": "district", "name": "Шевченківський"},
        {"type": "street", "name": "вулиця Велика Васильківська"},
        {"type": "house", "name": "25"},
    ],
    "shelter": [{"name": "Паркінг-укриття"}],
    "autonomyPower": True,
    "withoutCommission": True,
    "insertTime": "2026-11-06 18:20:00",
}

# Next.js RSC payload sample chunk with escaped quotes
SAMPLE_RSC_HTML = (
    r'<!DOCTYPE html><html><head></head><body>'
    r'<script>self.__next_f.push([1,"7:[\"$\",\"$L8\",null,{\"realties\":{\"cards\":['
    + json.dumps(SAMPLE_RESIDENTIAL_CARD_DICT, ensure_ascii=False).replace('"', r'\"')
    + r']}]);"])</script></body></html>'
)

SAMPLE_RSC_COMMERCIAL_HTML = (
    r'<!DOCTYPE html><html><head></head><body>'
    r'<script>self.__next_f.push([1,"7:[\"$\",\"$L8\",null,{\"realties\":{\"cards\":['
    + json.dumps(SAMPLE_COMMERCIAL_CARD_DICT, ensure_ascii=False).replace('"', r'\"')
    + r']}]);"])</script></body></html>'
)


class TestStage4Lun(unittest.TestCase):
    """Test suite for LUN.ua scraper, normalizer, and database integration."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()
        database.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def test_districts_configuration_and_slice_urls(self):
        """Verifies 10 official Kyiv districts and slice URL generator."""
        self.assertEqual(len(LUN_KYIV_DISTRICTS), 10)
        expected_names = {
            "Голосіївський", "Дарницький", "Деснянський", "Дніпровський",
            "Оболонський", "Печерський", "Подільський", "Святошинський",
            "Солом'янський", "Шевченківський",
        }
        actual_names = {name for _, name in LUN_KYIV_DISTRICTS}
        self.assertEqual(expected_names, actual_names)

        # Test residential slice URL
        s_res = LunSlice(
            category_type="residential",
            district_slug="obolonskyi",
            district_name="Оболонський",
            label="Оболонський (житлова)",
        )
        self.assertEqual(s_res.get_url(1), "https://lun.ua/rent/kyiv/flats-obolonskyi-district")
        self.assertEqual(s_res.get_url(2), "https://lun.ua/rent/kyiv/flats-obolonskyi-district?page=2")

        # Test commercial slice URL
        s_comm = LunSlice(
            category_type="commercial",
            district_slug="shevchenkivskyi",
            district_name="Шевченківський",
            label="Шевченківський (комерція)",
        )
        self.assertEqual(s_comm.get_url(1), "https://lun.ua/rent/kyiv/commercial-shevchenkivskyi-district")
        self.assertEqual(s_comm.get_url(3), "https://lun.ua/rent/kyiv/commercial-shevchenkivskyi-district?page=3")

    def test_rsc_card_extractor(self):
        """Tests bracket-counting RSC card extractor from Next.js payload."""
        cards = LunCardParser.extract_cards(SAMPLE_RSC_HTML)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["id"], 4731279531)
        self.assertEqual(cards[0]["price"], 25000)

        # Edge cases
        self.assertEqual(LunCardParser.extract_cards(""), [])
        self.assertEqual(LunCardParser.extract_cards("<html><body>No cards here</body></html>"), [])
        self.assertEqual(LunCardParser.extract_cards('{"realties": {"cards": []}}'), [])

    def test_normalize_lun_residential_standard(self):
        """Tests normalization of structured LUN residential offer into 23 schema columns."""
        norm = normalize_lun_residential_offer(SAMPLE_RESIDENTIAL_CARD_DICT)

        self.assertEqual(norm["url"], "https://lun.ua/uk/realty/4731279531")
        self.assertEqual(norm["source"], "lun")
        self.assertEqual(norm["price"], "25000")
        self.assertEqual(norm["currency"], "UAH")
        self.assertEqual(norm["seller"], "agency")
        self.assertEqual(norm["street"], "набережна Дніпровська")
        self.assertEqual(norm["house"], "14")
        self.assertEqual(norm["district"], "Дарницький")
        self.assertEqual(norm["rooms"], "2")
        self.assertEqual(norm["area_total"], "65.5")
        self.assertEqual(norm["area_kitchen"], "14")
        self.assertEqual(norm["floor"], "8")
        self.assertEqual(norm["floors_total"], "25")
        self.assertEqual(norm["building_type"], "new_build_2010s_plus")
        self.assertEqual(norm["generator_in_building"], "yes")
        self.assertEqual(norm["inverter_in_flat"], "yes")
        self.assertEqual(norm["pets_allowed"], "yes")
        self.assertEqual(norm["kids_allowed"], "yes")
        self.assertEqual(norm["commission_pct"], "0")
        self.assertEqual(norm["seen_date"], "2026-11-05")

    def test_normalize_lun_residential_jk_fallback(self):
        """Tests Stage 2 residential complex fallback rule when house number is missing."""
        norm = normalize_lun_residential_offer(SAMPLE_RESIDENTIAL_CARD_DICT)
        norm_fallback = normalize_lun_residential_offer(SAMPLE_RESIDENTIAL_JK_FALLBACK_CARD_DICT)

        self.assertEqual(norm_fallback["url"], "https://lun.ua/uk/realty/4731350629")
        self.assertEqual(norm_fallback["source"], "lun")
        self.assertEqual(norm_fallback["street"], "ЖК Саперно-Слобідський")
        self.assertEqual(norm_fallback["house"], "")
        self.assertEqual(norm_fallback["district"], "Голосіївський")
        self.assertEqual(norm_fallback["price"], "1200")
        self.assertEqual(norm_fallback["currency"], "USD")
        self.assertEqual(norm_fallback["seller"], "owner")
        self.assertEqual(norm_fallback["pets_allowed"], "no")
        self.assertEqual(norm_fallback["kids_allowed"], "no")

    def test_normalize_lun_commercial_aim_and_conditional_clearing(self):
        """Tests commercial normalization, aim mapping, URL protection, and conditional field clearing."""
        norm = normalize_lun_commercial_offer(SAMPLE_COMMERCIAL_CARD_DICT)

        # Primary key protection: third party rieltor.ua URL replaced by canonical LUN URL
        self.assertEqual(norm["url"], "https://lun.ua/uk/realty/4731324937")
        self.assertEqual(norm["source"], "lun")
        self.assertEqual(norm["property_type"], "office")
        self.assertEqual(norm["price"], "50000")
        self.assertEqual(norm["currency"], "UAH")
        self.assertEqual(norm["street"], "вулиця Велика Васильківська")
        self.assertEqual(norm["house"], "25")
        self.assertEqual(norm["district"], "Шевченківський")
        self.assertEqual(norm["area_total"], "120")
        self.assertEqual(norm["generator"], "yes")
        self.assertEqual(norm["shelter_in_building"], "yes")
        self.assertEqual(norm["commission_pct"], "0")

        # Strict conditional clearing: for 'office', ceiling_height_m, power_kw, separate_entrance, first_line must be cleared
        self.assertEqual(norm["ceiling_height_m"], "")
        self.assertEqual(norm["power_kw"], "")
        self.assertEqual(norm["separate_entrance"], "")
        self.assertEqual(norm["first_line"], "")

        # Warehouse test where ceiling_height_m, power_kw, loading_dock, heated ARE preserved
        wh_card = {
            "id": 4731399999,
            "urlRaw": "https://lun.ua/uk/realty/4731399999",
            "price": 80000,
            "currency": "uah",
            "aim": [{"name": "Склад", "id": 2}],
            "areaTotal": 450.0,
            "ceilingHeight": 6.5,
            "header": "Оренда теплого складу з рампою",
            "text": "Сучасний теплий склад, стеля 6.5 м, потужність 50 кВт, наявна рампа для розвантаження.",
            "geoEntities": [
                {"type": "district", "name": "Деснянський"},
                {"type": "street", "name": "вулиця Пухівська"},
                {"type": "house", "name": "4"},
            ],
            "heatingSystemName": "autonomous",
        }
        wh_norm = normalize_lun_commercial_offer(wh_card)
        self.assertEqual(wh_norm["property_type"], "warehouse")
        self.assertEqual(wh_norm["ceiling_height_m"], "6.5")
        self.assertEqual(wh_norm["power_kw"], "50")
        self.assertEqual(wh_norm["loading_dock"], "yes")
        self.assertEqual(wh_norm["heated"], "yes")

    def test_lun_scraper_generate_slices(self):
        """Verifies slice generation for different category combinations."""
        scraper = LunScraper(db_path=self.db_path)

        res_slices = scraper.generate_slices("residential")
        self.assertEqual(len(res_slices), 10)
        self.assertTrue(all(s.category_type == "residential" for s in res_slices))

        comm_slices = scraper.generate_slices("commercial")
        self.assertEqual(len(comm_slices), 10)
        self.assertTrue(all(s.category_type == "commercial" for s in comm_slices))

        all_slices = scraper.generate_slices("all")
        self.assertEqual(len(all_slices), 20)

    def test_lun_scraper_mocked_run_and_db_persistence(self):
        """Tests scraper run with mocked HTTP client, verifying database batch upserts and session records."""
        scraper = LunScraper(db_path=self.db_path)

        # Mock request_html to return residential RSC HTML on flats slices and commercial on commercial slices
        async def mock_request_html(url, stop_event=None):
            if "flats" in url:
                return SAMPLE_RSC_HTML
            elif "commercial" in url:
                return SAMPLE_RSC_COMMERCIAL_HTML
            return None

        scraper.client.request_html = AsyncMock(side_effect=mock_request_html)
        scraper.client.close = AsyncMock()

        progress_updates = []
        def on_progress(status_dict):
            progress_updates.append(status_dict["processed_count"])

        # Run 1 residential slice and 1 commercial slice
        res_slice = LunSlice("residential", "obolonskyi", "Оболонський", "Оболонський (житлова)")
        comm_slice = LunSlice("commercial", "shevchenkivskyi", "Шевченківський", "Шевченківський (комерція)")
        scraper.generate_slices = MagicMock(return_value=[res_slice, comm_slice])

        result = asyncio.run(
            scraper.run(
                category="all",
                max_offers=2,
                max_slices=2,
                on_progress=on_progress,
            )
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["processed_count"], 2)
        self.assertEqual(result["processed_residential"], 1)
        self.assertEqual(result["processed_commercial"], 1)
        self.assertGreaterEqual(len(progress_updates), 1)

        # Verify residential record in SQLite
        conn = database.get_db_connection(self.db_path)
        res_row = conn.execute(
            "SELECT url, source, district, price, building_type, first_session_id FROM residential_offers WHERE url = ?",
            ("https://lun.ua/uk/realty/4731279531",)
        ).fetchone()
        self.assertIsNotNone(res_row)
        self.assertEqual(res_row["source"], "lun")
        self.assertEqual(res_row["district"], "Дарницький")
        self.assertEqual(res_row["price"], 25000.0)
        self.assertEqual(res_row["building_type"], "new_build_2010s_plus")
        self.assertIsNotNone(res_row["first_session_id"])

        # Verify commercial record in SQLite
        comm_row = conn.execute(
            "SELECT url, source, district, property_type, generator, shelter_in_building FROM commercial_offers WHERE url = ?",
            ("https://lun.ua/uk/realty/4731324937",)
        ).fetchone()
        self.assertIsNotNone(comm_row)
        self.assertEqual(comm_row["source"], "lun")
        self.assertEqual(comm_row["district"], "Шевченківський")
        self.assertEqual(comm_row["property_type"], "office")
        self.assertEqual(comm_row["generator"], "yes")
        self.assertEqual(comm_row["shelter_in_building"], "yes")

        # Verify session record in SQLite
        session_row = conn.execute(
            "SELECT * FROM scrape_sessions WHERE id = ?",
            (result["session_id"],)
        ).fetchone()
        self.assertIsNotNone(session_row)
        self.assertEqual(session_row["status"], "completed")
        self.assertEqual(session_row["processed_count"], 2)
        conn.close()

    def test_lun_scraper_stop_and_error_handling(self):
        """Tests scraper stop() and error handling during scrape."""
        scraper = LunScraper(db_path=self.db_path)

        async def slow_request_html(url, stop_event=None):
            scraper.stop()
            return SAMPLE_RSC_HTML

        scraper.client.request_html = AsyncMock(side_effect=slow_request_html)
        scraper.client.close = AsyncMock()

        result = asyncio.run(scraper.run(category="residential", max_slices=5))
        self.assertEqual(result["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
