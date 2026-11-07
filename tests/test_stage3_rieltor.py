"""
Unit tests for Stage 3: Rieltor.ua scraper, card parser, and normalizer.
"""

import asyncio
from datetime import datetime, timedelta
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import database
from normalizer import (
    RieltorNormalizer,
    get_address_parser,
    normalize_rieltor_commercial_offer,
    normalize_rieltor_residential_offer,
)
from rieltor_scraper import (
    RIELTOR_KYIV_DISTRICTS,
    RieltorCardParser,
    RieltorHttpClient,
    RieltorScraper,
    RieltorSlice,
    clean_html_text,
    parse_rieltor_price,
    parse_rieltor_update_date,
)


SAMPLE_RESIDENTIAL_CARD_HTML = """
<div class="catalog-card " data-label="15 900 грн" data-analytics-event="card-click-all" data-jss="catalog-item" data-catalog-item-id="13191521" data-longitude="30.595668792725" data-latitude="50.449161529541">
    <a href="https://rieltor.ua/flats-rent/view/13191521/" class="catalog-card-media" data-analytics-event="card-gallery-click"></a>
    <div class="catalog-card-body">
        <strong class="catalog-card-price-title lun-identity-font">15 900 грн/міс</strong>
        <div class="catalog-card-address">Раїси Окіпної вул., 5</div>
        <div class="catalog-card-region">Київ , Дніпровський р-н</div>
        <div class="catalog-card-chips">
            <a class="catalog-card-chip -subway -subway-red" href="#">Лівобережна</a>
            <a class="catalog-card-chip -orient" href="#">Лівобережний масив</a>
        </div>
        <div class="catalog-card-details">
            <div class="catalog-card-details-row">3 кімнати</div>
            <div class="catalog-card-details-row">62 / 47 / 7 м²</div>
            <div class="catalog-card-details-row">поверх 4 з 9</div>
            <div class="catalog-card-details-row">1969 рік побудови</div>
        </div>
        <div class="catalog-card-description">
            <span class="-free">Здам 3-кімнатну квартиру біля метро. Є вся побутова техніка: бойлер, пральна машина, холодильник. В будинку встановлено генератор на ліфт. Без тварин.</span>
        </div>
        <div class="catalog-card-update">сьогодні</div>
        <div class="catalog-card-author">Олексій</div>
        <div class="catalog-card-agency">Агентство Недвижки</div>
    </div>
</div>
"""

SAMPLE_RESIDENTIAL_CARD_JK_FALLBACK_HTML = """
<div class="catalog-card " data-label="1 200 $" data-analytics-event="card-click-all" data-jss="catalog-item" data-catalog-item-id="13060863">
    <a href="https://rieltor.ua/flats-rent/view/13060863/" class="catalog-card-media"></a>
    <div class="catalog-card-body">
        <strong class="catalog-card-price-title">1 200 $/міс</strong>
        <div class="catalog-card-address">Саксаганського вул.</div>
        <div class="catalog-card-region">Київ , Голосіївський р-н</div>
        <div class="catalog-card-chips">
            <a class="catalog-card-chip" href="#">ПРЕМІУМ ЖК Royal Tower</a>
        </div>
        <div class="catalog-card-details">
            <div class="catalog-card-details-row">2 кімнати</div>
            <div class="catalog-card-details-row">67 / 45 / 12 м²</div>
            <div class="catalog-card-details-row">поверх 10 з 30</div>
            <div class="catalog-card-details-row">2016 рік побудови</div>
        </div>
        <div class="catalog-card-description">
            <span>Пропонується в оренду 2-кімнатна квартира в новому комплексі. Без комісії!</span>
        </div>
        <div class="catalog-card-update">вчора</div>
        <div class="catalog-card-author">Власник</div>
    </div>
</div>
"""

SAMPLE_COMMERCIAL_CARD_HTML = """
<div class="catalog-card " data-label="45 000 грн" data-jss="catalog-item" data-catalog-item-id="13200256">
    <a href="https://rieltor.ua/commercials-rent/view/13200256/" class="catalog-card-media"></a>
    <div class="catalog-card-body">
        <strong class="catalog-card-price-title">45 000 грн/міс</strong>
        <div class="catalog-card-address">Центральна вул., 21-А</div>
        <div class="catalog-card-region">Київ , Дарницький р-н</div>
        <div class="catalog-card-chips">
            <a class="catalog-card-chip" href="#">Славутич</a>
        </div>
        <div class="catalog-card-details">
            <div class="catalog-card-details-row">офісне приміщення</div>
            <div class="catalog-card-details-row">111 м²</div>
            <div class="catalog-card-details-row">поверх 2 з 10</div>
            <div class="catalog-card-details-row">2017 рік побудови</div>
        </div>
        <div class="catalog-card-description">
            <span>ОРЕНДА ОФІСУ В БЦ CRYSTAL | 111 м² | МЕТРО СЛАВУТИЧ. Окремий вхід, фасад, висота стелі — 3.2 м, потужність 25 кВт. Паркінг: 3 авто. Встановлено генератор.</span>
        </div>
        <div class="catalog-card-update">сьогодні</div>
        <div class="catalog-card-agency">Park Lane</div>
    </div>
</div>
"""


class TestStage3Rieltor(unittest.TestCase):
    """Test suite for Rieltor scraper, normalizer, and database integration."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()
        database.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_rieltor_districts_configuration(self):
        """Verifies 10 Kyiv districts with correct IDs and slugs."""
        self.assertEqual(len(RIELTOR_KYIV_DISTRICTS), 10)
        dist_names = [d[1] for d in RIELTOR_KYIV_DISTRICTS]
        expected_districts = [
            "Голосіївський", "Дарницький", "Деснянський", "Дніпровський",
            "Оболонський", "Печерський", "Подільський", "Святошинський",
            "Солом'янський", "Шевченківський"
        ]
        self.assertEqual(sorted(dist_names), sorted(expected_districts))

        for d_id, name, slug in RIELTOR_KYIV_DISTRICTS:
            self.assertGreater(d_id, 0)
            self.assertTrue(slug.endswith(f"-d{d_id}"))

    def test_rieltor_slice_generation(self):
        """Verifies slice creation for categories residential, commercial, all."""
        scraper = RieltorScraper(db_path=self.db_path)

        res_slices = scraper.generate_slices("residential")
        self.assertEqual(len(res_slices), 10)
        self.assertTrue(all(s.category_type == "residential" for s in res_slices))

        comm_slices = scraper.generate_slices("commercial")
        self.assertEqual(len(comm_slices), 10)
        self.assertTrue(all(s.category_type == "commercial" for s in comm_slices))

        all_slices = scraper.generate_slices("all")
        self.assertEqual(len(all_slices), 20)
        self.assertEqual(len([s for s in all_slices if s.category_type == "residential"]), 10)
        self.assertEqual(len([s for s in all_slices if s.category_type == "commercial"]), 10)

        # Verify Slice URL generation
        s = res_slices[0]
        self.assertTrue(s.get_url(1).startswith("https://rieltor.ua/flats-rent/"))
        self.assertIn("?page=2", s.get_url(2))

    def test_price_and_date_parsing_helpers(self):
        """Verifies price and date extraction helpers."""
        self.assertEqual(parse_rieltor_price("15 900 грн/міс"), (15900.0, "UAH"))
        self.assertEqual(parse_rieltor_price("1 200 $/міс"), (1200.0, "USD"))
        self.assertEqual(parse_rieltor_price("850 €/міс"), (850.0, "EUR"))
        self.assertEqual(parse_rieltor_price(None), (None, "UAH"))
        self.assertEqual(parse_rieltor_price("договірна"), (None, "UAH"))

        today_str = datetime.now().strftime("%Y-%m-%d")
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        self.assertEqual(parse_rieltor_update_date("сьогодні"), today_str)
        self.assertEqual(parse_rieltor_update_date("вчора"), yesterday_str)
        self.assertEqual(parse_rieltor_update_date(""), today_str)
        self.assertEqual(parse_rieltor_update_date(None), today_str)

    def test_residential_card_parsing(self):
        """Tests parsing a standard residential card."""
        parser = RieltorCardParser()
        cards = parser.extract_cards(SAMPLE_RESIDENTIAL_CARD_HTML)
        self.assertEqual(len(cards), 1)

        result = parser.parse_card(cards[0], category_type="residential", district_hint="Дніпровський")
        self.assertIsNotNone(result)
        norm, raw = result

        # Check raw
        self.assertEqual(raw["id"], "13191521")
        self.assertEqual(raw["url"], "https://rieltor.ua/flats-rent/view/13191521/")
        self.assertEqual(raw["rooms"], 3)
        self.assertEqual(raw["area_total"], 62.0)
        self.assertEqual(raw["area_kitchen"], 7.0)
        self.assertEqual(raw["floor"], 4)
        self.assertEqual(raw["floors_total"], 9)
        self.assertEqual(raw["built_year"], 1969)

        # Check normalized 23 fields
        self.assertEqual(norm["source"], "rieltor")
        self.assertEqual(norm["url"], "https://rieltor.ua/flats-rent/view/13191521/")
        self.assertEqual(norm["price"], "15900.0")
        self.assertEqual(norm["currency"], "UAH")
        self.assertEqual(norm["seller"], "agency")
        self.assertEqual(norm["street"], "вулиця Окіпної Раїси")
        self.assertEqual(norm["house"], "5")
        self.assertEqual(norm["district"], "Дніпровський")
        self.assertEqual(norm["rooms"], "3")
        self.assertEqual(norm["area_total"], "62")
        self.assertEqual(norm["area_kitchen"], "7")
        self.assertEqual(norm["floor"], "4")
        self.assertEqual(norm["floors_total"], "9")
        self.assertEqual(norm["building_type"], "khrushchyovka")  # 1969 -> khrushchyovka
        self.assertEqual(norm["generator_in_building"], "yes")
        self.assertEqual(norm["pets_allowed"], "no")
        self.assertIn("boiler", norm["appliances"])
        self.assertIn("washer", norm["appliances"])
        self.assertIn("fridge", norm["appliances"])

    def test_residential_card_jk_fallback(self):
        """Tests Stage 2 rule on Rieltor: fallback to 'ЖК <Name>' if house is missing."""
        parser = RieltorCardParser()
        cards = parser.extract_cards(SAMPLE_RESIDENTIAL_CARD_JK_FALLBACK_HTML)
        self.assertEqual(len(cards), 1)

        result = parser.parse_card(cards[0], category_type="residential", district_hint="Голосіївський")
        self.assertIsNotNone(result)
        norm, raw = result

        self.assertEqual(norm["source"], "rieltor")
        self.assertEqual(norm["house"], "")
        self.assertEqual(norm["street"], "ЖК Royal Tower")
        self.assertEqual(norm["district"], "Голосіївський")
        self.assertEqual(norm["price"], "1200.0")
        self.assertEqual(norm["currency"], "USD")
        self.assertEqual(norm["seller"], "owner")
        self.assertEqual(norm["commission_pct"], "0")
        self.assertEqual(norm["building_type"], "new_build_2010s_plus")  # 2016 -> new_build

    def test_commercial_card_parsing(self):
        """Tests commercial card parsing, business center extraction, and conditional clearing."""
        parser = RieltorCardParser()
        cards = parser.extract_cards(SAMPLE_COMMERCIAL_CARD_HTML)
        self.assertEqual(len(cards), 1)

        result = parser.parse_card(cards[0], category_type="commercial", district_hint="Дарницький")
        self.assertIsNotNone(result)
        norm, raw = result

        self.assertEqual(norm["source"], "rieltor")
        self.assertEqual(norm["url"], "https://rieltor.ua/commercials-rent/view/13200256/")
        self.assertEqual(norm["property_type"], "office")
        self.assertEqual(norm["price"], "45000.0")
        self.assertEqual(norm["currency"], "UAH")
        self.assertEqual(norm["price_basis"], "month_total")
        self.assertEqual(norm["street"], "вулиця Центральна")
        self.assertEqual(norm["house"], "21-А")
        self.assertEqual(norm["business_center_name"], "CRYSTAL")
        self.assertEqual(norm["area_total"], "111")
        self.assertEqual(norm["floor"], "2")
        self.assertEqual(norm["floors_total"], "10")
        # Verify conditional clearing: office allows BC name, parking, generator; clears first_line, separate_entrance, power_kw, etc.
        self.assertEqual(norm["business_center_name"], "CRYSTAL")
        self.assertEqual(norm["parking_spaces"], "3")
        self.assertEqual(norm["generator"], "yes")
        self.assertEqual(norm["separate_entrance"], "")
        self.assertEqual(norm["first_line"], "")
        self.assertEqual(norm["ceiling_height_m"], "")
        self.assertEqual(norm["power_kw"], "")
        self.assertEqual(norm["loading_dock"], "")
        self.assertEqual(norm["floor_load_t_m2"], "")
        self.assertEqual(norm["heated"], "")

    def test_rieltor_scraper_mocked_run_and_persistence(self):
        """Tests full RieltorScraper run workflow with mocked HTTP client."""
        async def run_test():
            scraper = RieltorScraper(db_path=self.db_path, min_delay=0.01, max_delay=0.02)

            # Mock client request_html
            async def fake_request(url, stop_event=None):
                if "?page=1" in url or not "?page=" in url:
                    if "commercials-rent" in url:
                        return SAMPLE_COMMERCIAL_CARD_HTML
                    return SAMPLE_RESIDENTIAL_CARD_HTML
                return ""  # page 2 empty -> end of slice

            scraper.client.request_html = AsyncMock(side_effect=fake_request)

            progress_calls = []
            def on_progress(stats):
                progress_calls.append(stats.copy())

            # Run 1 residential slice
            status_res = await scraper.run(
                category="residential",
                max_slices=1,
                on_progress=on_progress,
            )
            self.assertEqual(status_res["status"], "completed")
            self.assertEqual(status_res["source"], "rieltor")
            self.assertGreater(status_res["processed_count"], 0)
            self.assertGreater(len(progress_calls), 0)

            # Run 1 commercial slice
            scraper2 = RieltorScraper(db_path=self.db_path, min_delay=0.01, max_delay=0.02)
            scraper2.client.request_html = AsyncMock(side_effect=fake_request)
            status_comm = await scraper2.run(
                category="commercial",
                max_slices=1,
            )
            self.assertEqual(status_comm["status"], "completed")
            self.assertEqual(status_comm["source"], "rieltor")

            # Check database records
            res_offers = database.get_all_residential_offers(db_path=self.db_path, include_source=True)
            self.assertEqual(len(res_offers), 1)
            self.assertEqual(res_offers[0]["source"], "rieltor")
            self.assertEqual(res_offers[0]["url"], "https://rieltor.ua/flats-rent/view/13191521/")
            self.assertEqual(res_offers[0]["house"], "5")

            comm_offers = database.get_all_commercial_offers(db_path=self.db_path, include_source=True)
            self.assertEqual(len(comm_offers), 1)
            self.assertEqual(comm_offers[0]["source"], "rieltor")
            self.assertEqual(comm_offers[0]["url"], "https://rieltor.ua/commercials-rent/view/13200256/")
            self.assertEqual(comm_offers[0]["property_type"], "office")

            # Check session tracking
            stats = database.get_stats(self.db_path)
            self.assertEqual(stats["residential_by_source"].get("rieltor"), 1)
            self.assertEqual(stats["commercial_by_source"].get("rieltor"), 1)

        asyncio.run(run_test())

    def test_rieltor_scraper_graceful_stop(self):
        """Tests that scraper stops promptly when stop() is called."""
        async def run_stop_test():
            scraper = RieltorScraper(db_path=self.db_path, min_delay=0.01, max_delay=0.02)

            async def slow_request(url, stop_event=None):
                scraper.stop()
                return SAMPLE_RESIDENTIAL_CARD_HTML

            scraper.client.request_html = AsyncMock(side_effect=slow_request)
            status = await scraper.run(category="residential", max_slices=5)
            self.assertEqual(status["status"], "stopped")

        asyncio.run(run_stop_test())


if __name__ == "__main__":
    unittest.main()
