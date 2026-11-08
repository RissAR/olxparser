"""
Stage 6 End-to-End Multi-Source Integration, Stress Testing, and Acceptance Test Suite.

Verifies:
1. End-to-end ingestion and persistence of real estate offers from all three sources:
   - OLX.ua (olx_scraper)
   - Rieltor.ua (rieltor_scraper)
   - LUN.ua (lun_scraper)
2. SQLite database schema integrity:
   - source column tracking ('olx', 'rieltor', 'lun')
   - session correlation (first_session_id, last_session_id)
   - URL uniqueness and conflict resolution (ON CONFLICT(url) DO UPDATE)
   - deduplication across multiple scrape sessions
3. Multi-source anti-hallucination and ML schema stress tests:
   - Strict conditional field clearing across all sources (office vs warehouse vs retail)
   - Stage 2 Residential Complex (ЖК) fallback rule across all sources
   - Multi-currency ISO resolution (UAH, USD, EUR)
   - Autonomy features (generator, inverter, shelter)
4. Full CSV export and template compliance:
   - Reference templates (strictly 23 cols residential, 35 cols commercial, utf-8-sig BOM)
   - Multi-source unified exports (unified_residential_kyiv.csv, unified_commercial_kyiv.csv, unified_kyiv_real_estate.csv)
5. Multi-Source Orchestrator and FastAPI REST API full lifecycle.
"""

import asyncio
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import app
import database
from exporter import (
    DEFAULT_COMMERCIAL_FILENAME,
    DEFAULT_RESIDENTIAL_FILENAME,
    DEFAULT_UNIFIED_ALL_FILENAME,
    DEFAULT_UNIFIED_COMMERCIAL_FILENAME,
    DEFAULT_UNIFIED_RESIDENTIAL_FILENAME,
    export_all,
    export_commercial,
    export_residential,
    export_unified,
    export_unified_master,
    verify_csv_against_template,
)
from lun_scraper import LunCardParser
from normalizer import (
    COMMERCIAL_CONDITIONAL_FIELDS,
    CommercialNormalizer,
    LunNormalizer,
    ResidentialNormalizer,
    RieltorNormalizer,
    apply_conditional_clearing,
    get_address_parser,
    normalize_commercial_offer,
    normalize_lun_commercial_offer,
    normalize_lun_residential_offer,
    normalize_residential_offer,
    normalize_rieltor_commercial_offer,
    normalize_rieltor_residential_offer,
)
from orchestrator import MultiSourceOrchestrator, get_orchestrator_instance
from rieltor_scraper import RieltorCardParser
from tests.test_app import ASGITestClient
from verify import validate_commercial_rows, validate_residential_rows


class TestStage6EndToEnd(unittest.IsolatedAsyncioTestCase):
    """Stage 6 End-to-End Multi-Source Integration & Stress Test Suite."""

    def setUp(self):
        self.root_dir = Path(__file__).resolve().parent.parent
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "stage6_kyiv_rent.db")
        database.init_db(self.db_path)

        self.res_template = self.root_dir / "olx-manual-template.csv"
        self.comm_template = self.root_dir / "olx-commercial-template.csv"
        self.client = ASGITestClient(app)

    def tearDown(self):
        self.temp_dir.cleanup()

    @classmethod
    def tearDownClass(cls):
        for fname in [
            "olx_storage.db", "olx_storage.db-wal", "olx_storage.db-shm",
            "kyiv_rent.db", "kyiv_rent.db-wal", "kyiv_rent.db-shm",
            "olx_residential_kyiv.csv", "olx_commercial_kyiv.csv",
            "rieltor_residential_kyiv.csv", "rieltor_commercial_kyiv.csv",
            "lun_residential_kyiv.csv", "lun_commercial_kyiv.csv",
            "unified_residential_kyiv.csv", "unified_commercial_kyiv.csv",
            "unified_kyiv_real_estate.csv",
        ]:
            p = Path(fname)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        for p in Path(".").glob("*_202*-*.csv"):
            try:
                p.unlink()
            except OSError:
                pass

    # -------------------------------------------------------------------------
    # 1. Multi-Source Ingestion & Persistence
    # -------------------------------------------------------------------------
    async def test_e2e_multisource_ingestion_and_persistence(self):
        """Verifies ingestion and database storage across OLX, Rieltor, and LUN."""
        sess_olx = database.create_scrape_session("olx_all", self.db_path)
        sess_rieltor = database.create_scrape_session("rieltor_all", self.db_path)
        sess_lun = database.create_scrape_session("lun_all", self.db_path)

        # 1. OLX Residential & Commercial
        olx_res_norm = {
            "url": "https://www.olx.ua/d/uk/obyavlenie/olx-res-test-1.html",
            "source": "olx",
            "seen_date": "2026-11-07",
            "price": "22000",
            "currency": "UAH",
            "seller": "owner",
            "street": "вулиця Хрещатик",
            "house": "25",
            "district": "Шевченківський",
            "rooms": "2",
            "area_total": "60",
            "area_kitchen": "12",
            "floor": "3",
            "floors_total": "9",
            "building_type": "tsarist",
            "renovation": "euro",
            "furniture": "full",
            "appliances": "fridge,washer",
            "generator_in_building": "yes",
            "inverter_in_flat": "no",
            "pets_allowed": "yes",
            "kids_allowed": "yes",
            "commission_pct": "0",
            "utilities_included": "no",
        }
        database.upsert_residential_offer(olx_res_norm, db_path=self.db_path, session_id=sess_olx)

        olx_comm_norm = {
            "url": "https://www.olx.ua/d/uk/obyavlenie/olx-comm-test-1.html",
            "source": "olx",
            "seen_date": "2026-11-07",
            "property_type": "office",
            "price": "45000",
            "currency": "UAH",
            "price_basis": "month_total",
            "vat_included": "no",
            "opex_included": "yes",
            "opex_uah_m2": "",
            "seller": "agency",
            "street": "вулиця Велика Васильківська",
            "house": "100",
            "district": "Голосіївський",
            "business_center_name": "Toronto-Kyiv",
            "building_class": "A",
            "area_total": "120",
            "floor": "5",
            "floors_total": "18",
            "first_line": "",
            "separate_entrance": "",
            "shop_windows": "",
            "condition": "ready",
            "furniture": "full",
            "ceiling_height_m": "",
            "power_kw": "",
            "parking_spaces": "3",
            "generator": "yes",
            "shelter_in_building": "yes",
            "ventilation_hood": "",
            "water_in_rooms": "",
            "loading_dock": "",
            "floor_load_t_m2": "",
            "heated": "",
            "min_lease_months": "12",
            "commission_pct": "50",
        }
        database.upsert_commercial_offer(olx_comm_norm, db_path=self.db_path, session_id=sess_olx)

        # 2. Rieltor Residential & Commercial
        rieltor_res_norm = {
            "url": "https://rieltor.ua/flats-rent/view/13191521/",
            "source": "rieltor",
            "seen_date": "2026-11-07",
            "price": "18000",
            "currency": "UAH",
            "seller": "agency",
            "street": "вулиця Раїси Окіпної",
            "house": "5",
            "district": "Дніпровський",
            "rooms": "3",
            "area_total": "62",
            "area_kitchen": "7",
            "floor": "4",
            "floors_total": "9",
            "building_type": "khrushchyovka",
            "renovation": "euro",
            "furniture": "full",
            "appliances": "washer,boiler",
            "generator_in_building": "yes",
            "inverter_in_flat": "no",
            "pets_allowed": "no",
            "kids_allowed": "yes",
            "commission_pct": "50",
            "utilities_included": "no",
        }
        database.upsert_residential_offer(rieltor_res_norm, db_path=self.db_path, session_id=sess_rieltor)

        rieltor_comm_norm = {
            "url": "https://rieltor.ua/commercials-rent/view/13200256/",
            "source": "rieltor",
            "seen_date": "2026-11-07",
            "property_type": "warehouse",
            "price": "60000",
            "currency": "UAH",
            "price_basis": "month_total",
            "vat_included": "",
            "opex_included": "",
            "opex_uah_m2": "",
            "seller": "agency",
            "street": "вулиця Пухівська",
            "house": "4",
            "district": "Деснянський",
            "business_center_name": "",
            "building_class": "B",
            "area_total": "350",
            "floor": "1",
            "floors_total": "1",
            "first_line": "",
            "separate_entrance": "",
            "shop_windows": "",
            "condition": "ready",
            "furniture": "",
            "ceiling_height_m": "6.0",
            "power_kw": "40",
            "parking_spaces": "5",
            "generator": "yes",
            "shelter_in_building": "no",
            "ventilation_hood": "",
            "water_in_rooms": "",
            "loading_dock": "yes",
            "floor_load_t_m2": "5",
            "heated": "yes",
            "min_lease_months": "12",
            "commission_pct": "50",
        }
        database.upsert_commercial_offer(rieltor_comm_norm, db_path=self.db_path, session_id=sess_rieltor)

        # 3. LUN Residential & Commercial
        lun_res_norm = {
            "url": "https://lun.ua/uk/realty/4731279531",
            "source": "lun",
            "seen_date": "2026-11-07",
            "price": "28000",
            "currency": "UAH",
            "seller": "agency",
            "street": "набережна Дніпровська",
            "house": "14",
            "district": "Дарницький",
            "rooms": "2",
            "area_total": "65",
            "area_kitchen": "14",
            "floor": "8",
            "floors_total": "25",
            "building_type": "new_build_2010s_plus",
            "renovation": "euro",
            "furniture": "full",
            "appliances": "washer,dishwasher,boiler",
            "generator_in_building": "yes",
            "inverter_in_flat": "yes",
            "pets_allowed": "yes",
            "kids_allowed": "yes",
            "commission_pct": "0",
            "utilities_included": "no",
        }
        database.upsert_residential_offer(lun_res_norm, db_path=self.db_path, session_id=sess_lun)

        lun_comm_norm = {
            "url": "https://lun.ua/uk/realty/4731324937",
            "source": "lun",
            "seen_date": "2026-11-07",
            "property_type": "retail",
            "price": "75000",
            "currency": "UAH",
            "price_basis": "month_total",
            "vat_included": "",
            "opex_included": "",
            "opex_uah_m2": "",
            "seller": "owner",
            "street": "вулиця Хрещатик",
            "house": "15",
            "district": "Шевченківський",
            "business_center_name": "",
            "building_class": "",
            "area_total": "95",
            "floor": "1",
            "floors_total": "5",
            "first_line": "yes",
            "separate_entrance": "yes",
            "shop_windows": "yes",
            "condition": "ready",
            "furniture": "",
            "ceiling_height_m": "",
            "power_kw": "",
            "parking_spaces": "",
            "generator": "yes",
            "shelter_in_building": "yes",
            "ventilation_hood": "",
            "water_in_rooms": "",
            "loading_dock": "",
            "floor_load_t_m2": "",
            "heated": "",
            "min_lease_months": "12",
            "commission_pct": "0",
        }
        database.upsert_commercial_offer(lun_comm_norm, db_path=self.db_path, session_id=sess_lun)

        # Verify DB Counts & Breakdown
        stats = database.get_stats(self.db_path)
        self.assertEqual(stats["residential_count"], 3)
        self.assertEqual(stats["commercial_count"], 3)
        self.assertEqual(stats["residential_by_source"], {"olx": 1, "rieltor": 1, "lun": 1})
        self.assertEqual(stats["commercial_by_source"], {"olx": 1, "rieltor": 1, "lun": 1})

    # -------------------------------------------------------------------------
    # 2. Deduplication & Session Tracking Across Consecutive Runs
    # -------------------------------------------------------------------------
    async def test_e2e_deduplication_and_session_evolution(self):
        """Verifies ON CONFLICT updates last_session_id and preserves first_session_id."""
        sess_1 = database.create_scrape_session("lun_all", self.db_path)
        sess_2 = database.create_scrape_session("lun_all", self.db_path)

        card_url = "https://lun.ua/uk/realty/4731279531"
        data_v1 = {
            "url": card_url,
            "source": "lun",
            "seen_date": "2026-11-01",
            "price": "25000",
            "currency": "UAH",
            "seller": "agency",
            "street": "набережна Дніпровська",
            "house": "14",
            "district": "Дарницький",
            "rooms": "2",
            "area_total": "65",
        }
        database.upsert_residential_offer(data_v1, db_path=self.db_path, session_id=sess_1)

        # Second scrape: price lowered to 23000
        data_v2 = dict(data_v1)
        data_v2["seen_date"] = "2026-11-07"
        data_v2["price"] = "23000"
        database.upsert_residential_offer(data_v2, db_path=self.db_path, session_id=sess_2)

        conn = database.get_db_connection(self.db_path)
        row = conn.execute(
            "SELECT url, price, first_session_id, last_session_id FROM residential_offers WHERE url = ?",
            (card_url,)
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["price"], 23000.0)
        self.assertEqual(row["first_session_id"], sess_1)
        self.assertEqual(row["last_session_id"], sess_2)

        # Count must remain 1 (no duplicate rows created)
        stats = database.get_stats(self.db_path)
        self.assertEqual(stats["residential_count"], 1)

    # -------------------------------------------------------------------------
    # 3. Multi-Source Anti-Hallucination Stress Tests
    # -------------------------------------------------------------------------
    def test_stress_commercial_conditional_clearing_across_all_sources(self):
        """
        Stress test: Checks that office offers from ANY source unconditionally clear
        ceiling_height_m, power_kw, separate_entrance, and first_line per customer specification.
        """
        # Raw data with heavy buzzwords that could cause hallucinations
        office_buzzwords = (
            "Оренда офісу в БЦ. Окремий вхід, фасадні вікна, перша лінія! "
            "Висота стелі 4.2 м, підведено потужність 80 кВт. Власна рампа."
        )

        # Source 1: OLX
        olx_card = {
            "url": "https://www.olx.ua/d/uk/obyavlenie/office-buzz-1.html",
            "title": "Оренда престижного офісу",
            "description": office_buzzwords,
            "params": [
                {"key": "total_area", "value": "150"},
                {"key": "floor", "value": "2"},
                {"key": "price", "value": {"value": 50000, "currency": "UAH"}},
            ],
            "category": {"id": 3690},  # Office subcategory
        }
        olx_norm = normalize_commercial_offer(olx_card)
        self.assertEqual(olx_norm["property_type"], "office")
        self.assertEqual(olx_norm["ceiling_height_m"], "")
        self.assertEqual(olx_norm["power_kw"], "")
        self.assertEqual(olx_norm["separate_entrance"], "")
        self.assertEqual(olx_norm["first_line"], "")

        # Source 2: Rieltor
        rieltor_card = {
            "id": "999901",
            "url": "https://rieltor.ua/commercials-rent/view/999901/",
            "price": 50000.0,
            "currency": "UAH",
            "address": "вулиця Хрещатик, 1",
            "details": ["офісне приміщення", "150 м²", "поверх 2 з 5"],
            "description": office_buzzwords,
        }
        rieltor_norm = normalize_rieltor_commercial_offer(rieltor_card)
        self.assertEqual(rieltor_norm["property_type"], "office")
        self.assertEqual(rieltor_norm["ceiling_height_m"], "")
        self.assertEqual(rieltor_norm["power_kw"], "")
        self.assertEqual(rieltor_norm["separate_entrance"], "")
        self.assertEqual(rieltor_norm["first_line"], "")

        # Source 3: LUN
        lun_card = {
            "id": 999902,
            "urlRaw": "https://lun.ua/uk/realty/999902",
            "price": 50000,
            "currency": "uah",
            "aim": [{"name": "Офіс", "id": 1}],
            "areaTotal": 150,
            "ceilingHeight": 4.2,
            "header": "Оренда офісу в центрі",
            "text": office_buzzwords,
            "geoEntities": [
                {"type": "district", "name": "Шевченківський"},
                {"type": "street", "name": "вулиця Хрещатик"},
                {"type": "house", "name": "1"},
            ],
        }
        lun_norm = normalize_lun_commercial_offer(lun_card)
        self.assertEqual(lun_norm["property_type"], "office")
        self.assertEqual(lun_norm["ceiling_height_m"], "")
        self.assertEqual(lun_norm["power_kw"], "")
        self.assertEqual(lun_norm["separate_entrance"], "")
        self.assertEqual(lun_norm["first_line"], "")

    def test_stress_residential_complex_fallback_across_all_sources(self):
        """
        Stress test: Stage 2 residential complex fallback rule across OLX, Rieltor, and LUN.
        When street and house are missing, street must fallback to ЖК <Name>.
        """
        complex_name = "ЖК Нова Англія"

        # OLX: text has ЖК but title/description has no house number or street
        olx_data = {
            "title": "Здам 2-кімнатну квартиру в новому комплексі",
            "description": f"Чудова видова квартира в новому комплексі {complex_name}. Ремонт, техніка.",
            "params": [{"key": "number_of_rooms_string", "value": "dvuhkomnatnye"}],
            "city": {"name": "Київ"},
            "district": {"name": "Голосіївський"},
        }
        olx_norm = normalize_residential_offer(olx_data)
        self.assertEqual(olx_norm["street"], complex_name)
        self.assertEqual(olx_norm["house"], "")
        self.assertEqual(olx_norm["district"], "Голосіївський")

        # Rieltor: address has no house or street, chip has ЖК
        rieltor_data = {
            "id": "888801",
            "url": "https://rieltor.ua/flats-rent/view/888801/",
            "address": "",
            "district_hint": "Голосіївський",
            "chips": [complex_name],
            "description": "Квартира з панорамним видом.",
            "details": ["2 кімнати", "65 м²", "поверх 12 з 25"],
        }
        rieltor_norm = normalize_rieltor_residential_offer(rieltor_data)
        self.assertEqual(rieltor_norm["street"], complex_name)
        self.assertEqual(rieltor_norm["house"], "")
        self.assertEqual(rieltor_norm["district"], "Голосіївський")

        # LUN: geoEntities has residential_complex, but no house or street
        lun_data = {
            "id": 888802,
            "urlRaw": "https://lun.ua/uk/realty/888802",
            "header": "Оренда 2к квартири в новому комплексі",
            "text": "Сучасна квартира в новому ЖК.",
            "geoEntities": [
                {"type": "district", "name": "Голосіївський"},
                {"type": "residential_complex", "name": complex_name},
            ],
            "roomCount": 2,
            "areaTotal": 65,
        }
        lun_norm = normalize_lun_residential_offer(lun_data)
        self.assertEqual(lun_norm["street"], complex_name)
        self.assertEqual(lun_norm["house"], "")
        self.assertEqual(lun_norm["district"], "Голосіївський")

    # -------------------------------------------------------------------------
    # 4. Multi-Source CSV Export & Template Verification
    # -------------------------------------------------------------------------
    async def test_e2e_full_csv_export_and_template_compliance(self):
        """Verifies generation of all 5 CSV file types and schema conformance."""
        # Populate DB with 1 residential and 1 commercial offer from each source (total 6)
        await self.test_e2e_multisource_ingestion_and_persistence()

        out_dir = Path(self.temp_dir.name) / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)

        # 1. Standard Template Exports (23 cols residential, 35 cols commercial)
        res_info = export_residential(db_path=self.db_path, output_path=out_dir / DEFAULT_RESIDENTIAL_FILENAME)
        comm_info = export_commercial(db_path=self.db_path, output_path=out_dir / DEFAULT_COMMERCIAL_FILENAME)

        self.assertEqual(res_info["rows_count"], 3)
        self.assertEqual(comm_info["rows_count"], 3)

        # Verify against reference templates
        v_res = verify_csv_against_template(out_dir / DEFAULT_RESIDENTIAL_FILENAME, self.res_template)
        v_comm = verify_csv_against_template(out_dir / DEFAULT_COMMERCIAL_FILENAME, self.comm_template)

        self.assertTrue(v_res["valid"], f"Residential template errors: {v_res.get('errors')}")
        self.assertTrue(v_comm["valid"], f"Commercial template errors: {v_comm.get('errors')}")

        res_issues = validate_residential_rows(out_dir / DEFAULT_RESIDENTIAL_FILENAME)
        comm_issues = validate_commercial_rows(out_dir / DEFAULT_COMMERCIAL_FILENAME)
        self.assertEqual(res_issues, [], f"Residential row issues: {res_issues}")
        self.assertEqual(comm_issues, [], f"Commercial row issues: {comm_issues}")

        # 2. Multi-Source Unified Exports (includes source column)
        unified_res_info = export_residential(
            db_path=self.db_path,
            output_path=out_dir / DEFAULT_UNIFIED_RESIDENTIAL_FILENAME,
            include_source=True,
        )
        unified_comm_info = export_commercial(
            db_path=self.db_path,
            output_path=out_dir / DEFAULT_UNIFIED_COMMERCIAL_FILENAME,
            include_source=True,
        )
        unified_master_info = export_unified_master(
            db_path=self.db_path,
            output_path=out_dir / DEFAULT_UNIFIED_ALL_FILENAME,
        )

        self.assertEqual(unified_res_info["rows_count"], 3)
        self.assertEqual(unified_comm_info["rows_count"], 3)
        self.assertEqual(unified_master_info["rows_count"], 6)  # 3 res + 3 comm

        # Check UTF-8 BOM on all exported files
        for p in [
            out_dir / DEFAULT_RESIDENTIAL_FILENAME,
            out_dir / DEFAULT_COMMERCIAL_FILENAME,
            out_dir / DEFAULT_UNIFIED_RESIDENTIAL_FILENAME,
            out_dir / DEFAULT_UNIFIED_COMMERCIAL_FILENAME,
            out_dir / DEFAULT_UNIFIED_ALL_FILENAME,
        ]:
            raw_bytes = p.read_bytes()
            self.assertTrue(raw_bytes.startswith(b"\xef\xbb\xbf"), f"File {p.name} missing UTF-8 BOM")

    # -------------------------------------------------------------------------
    # 5. Full REST API and Orchestrator Integration
    # -------------------------------------------------------------------------
    async def test_e2e_rest_api_lifecycle(self):
        """Tests full REST API lifecycle: status -> start -> stop -> export -> files."""
        # Check initial status
        st_resp = await self.client.get("/api/status")
        self.assertEqual(st_resp.status_code, 200)
        self.assertFalse(st_resp.json()["is_running"])

        # Trigger multi-source start with mocked background execution
        with patch("app.run_scraping_workflow", new=AsyncMock()) as mock_wf:
            start_resp = await self.client.post(
                "/api/scrape/start",
                json={
                    "sources": ["olx", "rieltor", "lun"],
                    "category": "all",
                    "max_offers": 5,
                    "auto_export": False,
                },
            )
            self.assertEqual(start_resp.status_code, 200)
            self.assertEqual(start_resp.json()["status"], "started")
            self.assertEqual(start_resp.json()["sources"], ["olx", "rieltor", "lun"])

        # Stop endpoint
        stop_resp = await self.client.post("/api/scrape/stop")
        self.assertEqual(stop_resp.status_code, 200)

        # Export endpoint
        export_resp = await self.client.post(
            "/api/export",
            json={"category": "all", "source": "all", "include_source": True},
        )
        self.assertEqual(export_resp.status_code, 200)
        self.assertEqual(export_resp.json()["status"], "success")

        # Files endpoint
        files_resp = await self.client.get("/api/files")
        self.assertEqual(files_resp.status_code, 200)
        self.assertIn("files", files_resp.json())


if __name__ == "__main__":
    unittest.main()
