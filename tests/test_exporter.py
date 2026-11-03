"""
Unit tests for exporter.py module (Step 4).
Tests residential and commercial CSV export, UTF-8 BOM encoding (utf-8-sig),
strict column counts (23 for residential, 35 for commercial), numeric formatting,
metadata inspection, and template verification.
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
    init_db,
    upsert_commercial_offer,
    upsert_residential_offer,
)
from exporter import (
    DEFAULT_COMMERCIAL_FILENAME,
    DEFAULT_RESIDENTIAL_FILENAME,
    export_all,
    export_commercial,
    export_residential,
    format_csv_value,
    format_file_size,
    get_file_info,
    list_exported_files,
    verify_csv_against_template,
)


class TestValueFormatting(unittest.TestCase):
    """Tests for format_csv_value function."""

    def test_none_and_empty(self):
        self.assertEqual(format_csv_value(None), "")
        self.assertEqual(format_csv_value(""), "")
        self.assertEqual(format_csv_value("   "), "")

    def test_integers(self):
        self.assertEqual(format_csv_value(18000, "price"), "18000")
        self.assertEqual(format_csv_value(0, "commission_pct"), "0")
        self.assertEqual(format_csv_value(-1, "floor"), "-1")

    def test_floats_whole(self):
        self.assertEqual(format_csv_value(18000.0, "price"), "18000")
        self.assertEqual(format_csv_value(50.0, "commission_pct"), "50")
        self.assertEqual(format_csv_value(42.0, "area_total"), "42")

    def test_floats_fractional(self):
        self.assertEqual(format_csv_value(42.5, "area_total"), "42.5")
        self.assertEqual(format_csv_value(12.75, "area_kitchen"), "12.75")
        self.assertEqual(format_csv_value(3.5, "ceiling_height_m"), "3.5")

    def test_ceiling_height_format(self):
        # ceiling_height_m should format 3.0 as 3.0 according to template
        self.assertEqual(format_csv_value(3.0, "ceiling_height_m"), "3.0")

    def test_booleans(self):
        self.assertEqual(format_csv_value(True), "yes")
        self.assertEqual(format_csv_value(False), "no")

    def test_strings_with_whitespace(self):
        self.assertEqual(format_csv_value("  вулиця Хрещатик  "), "вулиця Хрещатик")
        self.assertEqual(format_csv_value("18000.0", "price"), "18000")


class TestCSVExport(unittest.TestCase):
    """Tests for export_residential, export_commercial, and export_all."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_storage.db")
        init_db(self.db_path)

        # Insert a sample residential offer
        self.mock_res = {
            "url": "https://www.olx.ua/d/uk/obyavlenie/test-res-1.html",
            "seen_date": "2026-11-03",
            "price": 18000.0,
            "currency": "UAH",
            "seller": "owner",
            "street": "вулиця Здолбунівська",
            "house": "7Д",
            "district": "Дарницький",
            "rooms": 1,
            "area_total": 42.0,
            "area_kitchen": 10.0,
            "floor": 9,
            "floors_total": 16,
            "building_type": "new_build_2010s_plus",
            "renovation": "euro",
            "furniture": "full",
            "appliances": "ac,washer,boiler,fridge",
            "generator_in_building": "yes",
            "inverter_in_flat": "no",
            "pets_allowed": "yes",
            "kids_allowed": "yes",
            "commission_pct": 0.0,
            "utilities_included": "no",
        }
        upsert_residential_offer(self.mock_res, db_path=self.db_path)

        # Insert a sample commercial offer
        self.mock_comm = {
            "url": "https://www.olx.ua/d/uk/obyavlenie/test-comm-1.html",
            "seen_date": "2026-11-03",
            "property_type": "office",
            "price": 1200.0,
            "currency": "USD",
            "price_basis": "month_total",
            "vat_included": "no",
            "opex_included": "no",
            "opex_uah_m2": "",
            "seller": "agency",
            "street": "вулиця Велика Васильківська",
            "house": "72",
            "district": "Голосіївський",
            "business_center_name": "",
            "building_class": "B",
            "area_total": 85.0,
            "floor": 5,
            "floors_total": 9,
            "first_line": "no",
            "separate_entrance": "no",
            "shop_windows": "no",
            "condition": "ready",
            "furniture": "partial",
            "ceiling_height_m": 3.0,
            "power_kw": 15.0,
            "parking_spaces": 2,
            "generator": "yes",
            "shelter_in_building": "yes",
            "ventilation_hood": "",
            "water_in_rooms": "",
            "loading_dock": "",
            "floor_load_t_m2": "",
            "heated": "yes",
            "min_lease_months": 12,
            "commission_pct": 50.0,
        }
        upsert_commercial_offer(self.mock_comm, db_path=self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_export_residential(self):
        out_csv = os.path.join(self.temp_dir.name, "residential_out.csv")
        info = export_residential(db_path=self.db_path, output_path=out_csv)

        self.assertEqual(info["rows_count"], 1)
        self.assertTrue(os.path.exists(out_csv))

        # Check BOM
        with open(out_csv, "rb") as f:
            self.assertTrue(f.read(3) == b"\xef\xbb\xbf")

        # Check CSV content and 23 columns
        with open(out_csv, "r", encoding="utf-8-sig") as f:
            reader = list(csv.reader(f))
            header = reader[0]
            data_row = reader[1]

            self.assertEqual(header, RESIDENTIAL_COLUMNS)
            self.assertEqual(len(header), 23)
            self.assertEqual(len(data_row), 23)

            # Check specific fields
            self.assertEqual(data_row[0], self.mock_res["url"])
            self.assertEqual(data_row[2], "18000")  # price without .0
            self.assertEqual(data_row[8], "1")      # rooms
            self.assertEqual(data_row[9], "42")     # area_total
            self.assertEqual(data_row[16], "ac,washer,boiler,fridge")
            self.assertEqual(data_row[21], "0")     # commission_pct

    def test_export_commercial(self):
        out_csv = os.path.join(self.temp_dir.name, "commercial_out.csv")
        info = export_commercial(db_path=self.db_path, output_path=out_csv)

        self.assertEqual(info["rows_count"], 1)
        self.assertTrue(os.path.exists(out_csv))

        # Check BOM
        with open(out_csv, "rb") as f:
            self.assertTrue(f.read(3) == b"\xef\xbb\xbf")

        # Check CSV content and 35 columns
        with open(out_csv, "r", encoding="utf-8-sig") as f:
            reader = list(csv.reader(f))
            header = reader[0]
            data_row = reader[1]

            self.assertEqual(header, COMMERCIAL_COLUMNS)
            self.assertEqual(len(header), 35)
            self.assertEqual(len(data_row), 35)

            # Check specific fields
            self.assertEqual(data_row[0], self.mock_comm["url"])
            self.assertEqual(data_row[2], "office")
            self.assertEqual(data_row[3], "1200")
            self.assertEqual(data_row[14], "B")
            self.assertEqual(data_row[15], "85")
            self.assertEqual(data_row[23], "3.0")   # ceiling_height_m
            self.assertEqual(data_row[24], "15")    # power_kw
            self.assertEqual(data_row[25], "2")     # parking_spaces
            self.assertEqual(data_row[33], "12")    # min_lease_months
            self.assertEqual(data_row[34], "50")    # commission_pct

    def test_export_all_to_directory(self):
        export_dir = os.path.join(self.temp_dir.name, "sub_exports")
        results = export_all(db_path=self.db_path, output_dir=export_dir)

        self.assertIn("residential", results)
        self.assertIn("commercial", results)

        res_path = Path(export_dir) / DEFAULT_RESIDENTIAL_FILENAME
        comm_path = Path(export_dir) / DEFAULT_COMMERCIAL_FILENAME

        self.assertTrue(res_path.exists())
        self.assertTrue(comm_path.exists())
        self.assertEqual(results["residential"]["rows_count"], 1)
        self.assertEqual(results["commercial"]["rows_count"], 1)

    def test_export_empty_tables(self):
        empty_db = os.path.join(self.temp_dir.name, "empty_storage.db")
        init_db(empty_db)

        out_res = os.path.join(self.temp_dir.name, "empty_res.csv")
        out_comm = os.path.join(self.temp_dir.name, "empty_comm.csv")

        info_res = export_residential(db_path=empty_db, output_path=out_res)
        info_comm = export_commercial(db_path=empty_db, output_path=out_comm)

        self.assertEqual(info_res["rows_count"], 0)
        self.assertEqual(info_comm["rows_count"], 0)

        with open(out_res, "r", encoding="utf-8-sig") as f:
            lines = list(csv.reader(f))
            self.assertEqual(len(lines), 1)
            self.assertEqual(len(lines[0]), 23)

        with open(out_comm, "r", encoding="utf-8-sig") as f:
            lines = list(csv.reader(f))
            self.assertEqual(len(lines), 1)
            self.assertEqual(len(lines[0]), 35)

    def test_export_only_latest_date(self):
        # Insert older residential and commercial offers
        older_res = dict(self.mock_res)
        older_res["url"] = "https://www.olx.ua/d/uk/obyavlenie/test-res-old.html"
        older_res["seen_date"] = "2026-10-20"
        upsert_residential_offer(older_res, db_path=self.db_path)

        older_comm = dict(self.mock_comm)
        older_comm["url"] = "https://www.olx.ua/d/uk/obyavlenie/test-comm-old.html"
        older_comm["seen_date"] = "2026-10-20"
        upsert_commercial_offer(older_comm, db_path=self.db_path)

        # Default: only_latest_date=False returns all 2 offers
        out_all = os.path.join(self.temp_dir.name, "all_res.csv")
        info_all = export_residential(db_path=self.db_path, output_path=out_all, only_latest_date=False)
        self.assertEqual(info_all["rows_count"], 2)

        # only_latest_date=True returns only 1 latest offer (2026-11-03)
        out_latest = os.path.join(self.temp_dir.name, "latest_res.csv")
        info_latest = export_residential(db_path=self.db_path, output_path=out_latest, only_latest_date=True)
        self.assertEqual(info_latest["rows_count"], 1)

        # Also verify database function directly with since_days
        from database import get_all_residential_offers, get_all_commercial_offers
        rows_since = get_all_residential_offers(db_path=self.db_path, since_days=5)
        self.assertEqual(len(rows_since), 1)

        rows_all = get_all_residential_offers(db_path=self.db_path, since_days=20)
        self.assertEqual(len(rows_all), 2)

    def test_export_all_dated_archive(self):
        export_dir = os.path.join(self.temp_dir.name, "dated_archive_exports")
        results = export_all(db_path=self.db_path, output_dir=export_dir, dated_archive=True)

        self.assertIn("residential", results)
        self.assertIn("commercial", results)
        self.assertIn("dated_residential", results)
        self.assertIn("dated_commercial", results)

        from datetime import datetime
        today_str = datetime.now().strftime("%Y-%m-%d")
        dated_res = Path(export_dir) / f"olx_residential_kyiv_{today_str}.csv"
        dated_comm = Path(export_dir) / f"olx_commercial_kyiv_{today_str}.csv"

        self.assertTrue(dated_res.exists())
        self.assertTrue(dated_comm.exists())


class TestVerificationAndFileListing(unittest.TestCase):
    """Tests for verify_csv_against_template and list_exported_files."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_verify_against_real_templates(self):
        # Verify exported files vs actual repo templates if templates exist
        if Path("olx-manual-template.csv").exists() and Path("olx_residential_kyiv.csv").exists():
            v_res = verify_csv_against_template("olx_residential_kyiv.csv", "olx-manual-template.csv")
            self.assertTrue(v_res["valid"], msg=f"Errors: {v_res.get('errors')}")
            self.assertEqual(v_res["columns_count"], 23)

        if Path("olx-commercial-template.csv").exists() and Path("olx_commercial_kyiv.csv").exists():
            v_comm = verify_csv_against_template("olx_commercial_kyiv.csv", "olx-commercial-template.csv")
            self.assertTrue(v_comm["valid"], msg=f"Errors: {v_comm.get('errors')}")
            self.assertEqual(v_comm["columns_count"], 35)

    def test_verify_detects_mismatch(self):
        # Create a CSV with wrong column count
        bad_csv = Path(self.temp_dir.name) / "bad.csv"
        with open(bad_csv, "w", encoding="utf-8") as f:  # missing BOM too
            f.write("col1,col2\nval1\n")

        mock_tmpl = Path(self.temp_dir.name) / "mock_tmpl.csv"
        with open(mock_tmpl, "w", encoding="utf-8-sig") as f:
            f.write("col1,col2,col3\n1,2,3\n")

        v = verify_csv_against_template(bad_csv, mock_tmpl)
        self.assertFalse(v["valid"])
        self.assertTrue(any("UTF-8 BOM" in err for err in v["errors"]))
        self.assertTrue(any("Header mismatch" in err for err in v["errors"]))

    def test_get_file_info_and_size_formatting(self):
        self.assertEqual(format_file_size(500), "500 B")
        self.assertEqual(format_file_size(2048), "2.0 KB")
        self.assertEqual(format_file_size(1048576 * 3), "3.00 MB")

        test_file = Path(self.temp_dir.name) / "test.csv"
        with open(test_file, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["a", "b"])
            writer.writerow(["1", "2"])
            writer.writerow(["3", "4"])

        info = get_file_info(test_file)
        self.assertIsNotNone(info)
        self.assertEqual(info["filename"], "test.csv")
        self.assertEqual(info["rows_count"], 2)

    def test_list_exported_files(self):
        f1 = Path(self.temp_dir.name) / "export1.csv"
        f2 = Path(self.temp_dir.name) / "export2.csv"
        for f in (f1, f2):
            with open(f, "w", encoding="utf-8-sig") as fp:
                fp.write("a,b\n1,2\n")

        listed = list_exported_files([self.temp_dir.name])
        filenames = [item["filename"] for item in listed]
        self.assertIn("export1.csv", filenames)
        self.assertIn("export2.csv", filenames)


if __name__ == "__main__":
    unittest.main()
