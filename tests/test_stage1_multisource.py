"""
Unit tests for Stage 1 Multi-Source Architecture & Exporter:
- Schema migration and 'source' column support in SQLite
- Session tracking (first_session_id, last_session_id) and preservation on conflict
- Deduplication filtering (only_new=True)
- Source filtering (source='lun', 'rieltor', 'olx')
- Unified multi-source export with 'source' column (UNIFIED_RESIDENTIAL_COLUMNS, UNIFIED_COMMERCIAL_COLUMNS)
- Unified single master file export (UNIFIED_MASTER_COLUMNS)
"""

import csv
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import (
    COMMERCIAL_COLUMNS,
    RESIDENTIAL_COLUMNS,
    create_scrape_session,
    get_all_commercial_offers,
    get_all_residential_offers,
    get_db_connection,
    get_stats,
    init_db,
    upsert_commercial_offer,
    upsert_commercial_offers_batch,
    upsert_residential_offer,
    upsert_residential_offers_batch,
)
from exporter import (
    DEFAULT_UNIFIED_ALL_FILENAME,
    DEFAULT_UNIFIED_COMMERCIAL_FILENAME,
    DEFAULT_UNIFIED_RESIDENTIAL_FILENAME,
    UNIFIED_COMMERCIAL_COLUMNS,
    UNIFIED_MASTER_COLUMNS,
    UNIFIED_RESIDENTIAL_COLUMNS,
    export_commercial,
    export_residential,
    export_unified,
    export_unified_master,
)


class TestStage1MultiSource(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_multisource.db")
        init_db(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_schema_has_source_and_session_columns(self):
        """Checks that tables include source, first_session_id, and last_session_id."""
        conn = get_db_connection(self.db_path)
        res_cols = {row[1] for row in conn.execute("PRAGMA table_info(residential_offers)").fetchall()}
        comm_cols = {row[1] for row in conn.execute("PRAGMA table_info(commercial_offers)").fetchall()}
        conn.close()

        self.assertIn("source", res_cols)
        self.assertIn("first_session_id", res_cols)
        self.assertIn("last_session_id", res_cols)

        self.assertIn("source", comm_cols)
        self.assertIn("first_session_id", comm_cols)
        self.assertIn("last_session_id", comm_cols)

    def test_schema_migration_on_legacy_db(self):
        """Verifies safe migration of an existing database without source columns."""
        legacy_db_path = os.path.join(self.temp_dir.name, "legacy.db")
        conn = sqlite3.connect(legacy_db_path)
        # Create realistic v1 table with standard columns but without source or sessions
        conn.execute("""
            CREATE TABLE residential_offers (
                url TEXT PRIMARY KEY,
                seen_date TEXT,
                price REAL,
                currency TEXT,
                seller TEXT,
                street TEXT,
                house TEXT,
                district TEXT
            );
        """)
        conn.execute("""
            CREATE TABLE commercial_offers (
                url TEXT PRIMARY KEY,
                seen_date TEXT,
                property_type TEXT,
                price REAL,
                currency TEXT,
                seller TEXT,
                street TEXT,
                house TEXT,
                district TEXT
            );
        """)
        conn.commit()
        conn.close()

        # Running init_db must safely add missing columns
        init_db(legacy_db_path)

        conn = sqlite3.connect(legacy_db_path)
        res_cols = {row[1] for row in conn.execute("PRAGMA table_info(residential_offers)").fetchall()}
        comm_cols = {row[1] for row in conn.execute("PRAGMA table_info(commercial_offers)").fetchall()}
        self.assertIn("source", res_cols)
        self.assertIn("first_session_id", res_cols)
        self.assertIn("last_session_id", res_cols)
        self.assertIn("source", comm_cols)
        self.assertIn("first_session_id", comm_cols)
        self.assertIn("last_session_id", comm_cols)
        conn.close()

    def test_source_tagging_and_stats(self):
        """Tests inserting offers from OLX, LUN, and Rieltor, and inspecting stats."""
        # 1 OLX offer
        upsert_residential_offer(
            {"url": "https://www.olx.ua/d/1", "price": 15000, "currency": "UAH", "source": "olx"},
            db_path=self.db_path,
        )
        # 2 LUN offers
        upsert_residential_offer(
            {"url": "https://lun.ua/realty/1", "price": 18000, "currency": "UAH", "source": "lun"},
            db_path=self.db_path,
        )
        upsert_residential_offer(
            {"url": "https://lun.ua/realty/2", "price": 20000, "currency": "UAH", "source": "lun"},
            db_path=self.db_path,
        )
        # 1 Rieltor offer
        upsert_residential_offer(
            {"url": "https://rieltor.ua/flats-rent/view/1", "price": 25000, "currency": "UAH", "source": "rieltor"},
            db_path=self.db_path,
        )

        stats = get_stats(self.db_path)
        self.assertEqual(stats["residential_count"], 4)
        self.assertEqual(stats["residential_by_source"].get("olx"), 1)
        self.assertEqual(stats["residential_by_source"].get("lun"), 2)
        self.assertEqual(stats["residential_by_source"].get("rieltor"), 1)

    def test_deduplication_and_session_tracking(self):
        """Verifies session tracking and only_new deduplication logic."""
        session_1 = create_scrape_session("residential", db_path=self.db_path)
        session_2 = create_scrape_session("residential", db_path=self.db_path)

        # Run 1: Offer A and Offer B are discovered
        upsert_residential_offer(
            {"url": "https://lun.ua/realty/A", "price": 15000, "source": "lun"},
            db_path=self.db_path,
            session_id=session_1,
        )
        upsert_residential_offer(
            {"url": "https://rieltor.ua/flats-rent/view/B", "price": 20000, "source": "rieltor"},
            db_path=self.db_path,
            session_id=session_1,
        )

        # Run 2: Offer A is seen again (duplicate), but Offer C is brand new
        upsert_residential_offer(
            {"url": "https://lun.ua/realty/A", "price": 15500, "source": "lun"},
            db_path=self.db_path,
            session_id=session_2,
        )
        upsert_residential_offer(
            {"url": "https://www.olx.ua/d/C", "price": 30000, "source": "olx"},
            db_path=self.db_path,
            session_id=session_2,
        )

        # All offers count = 3
        all_offers = get_all_residential_offers(db_path=self.db_path)
        self.assertEqual(len(all_offers), 3)

        # In Session 2, only Offer C is new (Offer A has first_session_id=session_1)
        new_offers = get_all_residential_offers(db_path=self.db_path, only_new=True, session_id=session_2)
        self.assertEqual(len(new_offers), 1)
        self.assertEqual(new_offers[0]["url"], "https://www.olx.ua/d/C")

    def test_export_with_include_source(self):
        """Tests that exporting with include_source=True includes the 'source' column."""
        upsert_residential_offer(
            {
                "url": "https://lun.ua/realty/test-1",
                "price": 25000,
                "currency": "UAH",
                "source": "lun",
                "street": "вулиця Бережанська",
                "house": "12-А",
                "rooms": 2,
                "area_total": 63.0,
            },
            db_path=self.db_path,
        )

        out_path = os.path.join(self.temp_dir.name, "test_res_with_source.csv")
        info = export_residential(db_path=self.db_path, output_path=out_path, include_source=True)
        self.assertEqual(info["rows_count"], 1)

        with open(out_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader)
            row = next(reader)

        self.assertEqual(header, UNIFIED_RESIDENTIAL_COLUMNS)
        self.assertEqual(header[0], "source")
        self.assertEqual(row[0], "lun")
        self.assertEqual(row[header.index("price")], "25000")

    def test_export_unified_master_combines_sources_and_categories(self):
        """Tests export_unified_master combines residential and commercial with 'source' and 'category'."""
        upsert_residential_offer(
            {
                "url": "https://lun.ua/realty/res-1",
                "source": "lun",
                "price": 18000,
                "currency": "UAH",
                "street": "вулиця Хрещатик",
                "house": "10",
                "rooms": 1,
                "area_total": 45.0,
            },
            db_path=self.db_path,
        )
        upsert_commercial_offer(
            {
                "url": "https://rieltor.ua/commerce/comm-1",
                "source": "rieltor",
                "property_type": "office",
                "price": 1000,
                "currency": "USD",
                "street": "вулиця Велика Васильківська",
                "house": "25",
                "area_total": 80.0,
            },
            db_path=self.db_path,
        )

        out_path = os.path.join(self.temp_dir.name, "master.csv")
        info = export_unified_master(db_path=self.db_path, output_path=out_path)
        self.assertEqual(info["rows_count"], 2)

        with open(out_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        self.assertEqual(len(rows), 2)
        res_row = next(r for r in rows if r["category"] == "residential")
        comm_row = next(r for r in rows if r["category"] == "commercial")

        self.assertEqual(res_row["source"], "lun")
        self.assertEqual(res_row["rooms"], "1")
        self.assertEqual(res_row["property_type"], "")

        self.assertEqual(comm_row["source"], "rieltor")
        self.assertEqual(comm_row["property_type"], "office")
        self.assertEqual(comm_row["rooms"], "")


if __name__ == "__main__":
    unittest.main()
