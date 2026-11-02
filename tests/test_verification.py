"""
Step 7 Verification and Acceptance Tests.

Verifies:
1. Exact structural compliance with olx-manual-template.csv (23 columns).
2. Exact structural compliance with olx-commercial-template.csv (35 columns).
3. UTF-8 BOM encoding (\xef\xbb\xbf) presence on exported CSV files.
4. Row-level data types, formatting, enumerations, and boolean values.
5. End-to-end test execution of the verification pipeline (verify.py).
"""

import csv
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    verify_csv_against_template,
)
from verify import (
    run_full_verification,
    validate_commercial_rows,
    validate_residential_rows,
)


class TestStep7Verification(unittest.TestCase):
    """Test suite for Step 7 verification and acceptance."""

    def setUp(self):
        self.root_dir = Path(__file__).resolve().parent.parent
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db = os.path.join(self.temp_dir.name, "test_verify.db")
        init_db(self.test_db)

        self.res_template = self.root_dir / "olx-manual-template.csv"
        self.comm_template = self.root_dir / "olx-commercial-template.csv"

        # Valid residential sample
        self.sample_res = {
            "url": "https://www.olx.ua/d/uk/obyavlenie/test-verify-res-1.html",
            "seen_date": "2026-11-03",
            "price": 22000.0,
            "currency": "UAH",
            "seller": "owner",
            "street": "вулиця Хрещатик",
            "house": "25",
            "district": "Шевченківський",
            "rooms": 2,
            "area_total": 60.5,
            "area_kitchen": 12.0,
            "floor": 3,
            "floors_total": 9,
            "building_type": "tsar",
            "renovation": "author",
            "furniture": "full",
            "appliances": "ac,washer,boiler,dishwasher",
            "generator_in_building": "yes",
            "inverter_in_flat": "no",
            "pets_allowed": "yes",
            "kids_allowed": "yes",
            "commission_pct": 0.0,
            "utilities_included": "no",
        }

        # Valid commercial sample
        self.sample_comm = {
            "url": "https://www.olx.ua/d/uk/obyavlenie/test-verify-comm-1.html",
            "seen_date": "2026-11-03",
            "property_type": "office",
            "price": 1500.0,
            "currency": "USD",
            "price_basis": "month_total",
            "vat_included": "no",
            "opex_included": "yes",
            "opex_uah_m2": 50.0,
            "seller": "agency",
            "street": "вулиця Велика Васильківська",
            "house": "100",
            "district": "Голосіївський",
            "business_center_name": "Toronto-Kyiv",
            "building_class": "A",
            "area_total": 120.0,
            "floor": 5,
            "floors_total": 18,
            "first_line": "yes",
            "separate_entrance": "yes",
            "shop_windows": "no",
            "condition": "ready",
            "furniture": "partial",
            "ceiling_height_m": 3.2,
            "power_kw": 25.0,
            "parking_spaces": 3,
            "generator": "yes",
            "shelter_in_building": "yes",
            "ventilation_hood": "no",
            "water_in_rooms": "yes",
            "loading_dock": "no",
            "floor_load_t_m2": None,
            "heated": "yes",
            "min_lease_months": 12,
            "commission_pct": 50.0,
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_reference_templates_exist_and_match_specs(self):
        """Verifies reference template files in repository root."""
        self.assertTrue(self.res_template.is_file(), "olx-manual-template.csv must exist")
        self.assertTrue(self.comm_template.is_file(), "olx-commercial-template.csv must exist")

        # Residential template
        with open(self.res_template, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader)
            self.assertEqual(len(header), 23, "Residential template must have 23 columns")
            self.assertEqual(header, RESIDENTIAL_COLUMNS, "Header must strictly match RESIDENTIAL_COLUMNS")

        # Commercial template
        with open(self.comm_template, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader)
            self.assertEqual(len(header), 35, "Commercial template must have 35 columns")
            self.assertEqual(header, COMMERCIAL_COLUMNS, "Header must strictly match COMMERCIAL_COLUMNS")

    def test_export_and_verify_residential_template_conformance(self):
        """Verifies generated residential CSV strictly matches template and BOM."""
        upsert_residential_offer(self.sample_res, db_path=self.test_db)
        out_csv = Path(self.temp_dir.name) / "test_res.csv"

        info = export_residential(db_path=self.test_db, output_path=out_csv)
        self.assertEqual(info["rows_count"], 1)

        # Check BOM
        with open(out_csv, "rb") as bf:
            bom = bf.read(3)
            self.assertEqual(bom, b"\xef\xbb\xbf", "Exported CSV must begin with UTF-8 BOM")

        # Template verification function
        v_res = verify_csv_against_template(out_csv, self.res_template)
        self.assertTrue(v_res["valid"], msg=f"Errors: {v_res.get('errors')}")
        self.assertEqual(v_res["columns_count"], 23)
        self.assertEqual(v_res["rows_count"], 1)

        # Row-level validation
        issues = validate_residential_rows(out_csv)
        self.assertEqual(len(issues), 0, msg=f"Validation issues: {issues}")

    def test_export_and_verify_commercial_template_conformance(self):
        """Verifies generated commercial CSV strictly matches template and BOM."""
        upsert_commercial_offer(self.sample_comm, db_path=self.test_db)
        out_csv = Path(self.temp_dir.name) / "test_comm.csv"

        info = export_commercial(db_path=self.test_db, output_path=out_csv)
        self.assertEqual(info["rows_count"], 1)

        # Check BOM
        with open(out_csv, "rb") as bf:
            bom = bf.read(3)
            self.assertEqual(bom, b"\xef\xbb\xbf", "Exported CSV must begin with UTF-8 BOM")

        # Template verification function
        v_comm = verify_csv_against_template(out_csv, self.comm_template)
        self.assertTrue(v_comm["valid"], msg=f"Errors: {v_comm.get('errors')}")
        self.assertEqual(v_comm["columns_count"], 35)
        self.assertEqual(v_comm["rows_count"], 1)

        # Row-level validation
        issues = validate_commercial_rows(out_csv)
        self.assertEqual(len(issues), 0, msg=f"Validation issues: {issues}")

    def test_validate_rows_detects_invalid_values(self):
        """Verifies that validate_residential_rows and validate_commercial_rows catch invalid data."""
        bad_csv = Path(self.temp_dir.name) / "bad_rows.csv"

        # Invalid residential rows
        with open(bad_csv, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=RESIDENTIAL_COLUMNS)
            writer.writeheader()
            writer.writerow({
                "url": "not-a-url",
                "seen_date": "03.11.2026",  # wrong format
                "price": "-500",            # negative
                "currency": "BITCOIN",       # invalid currency
                "seller": "unknown_actor",  # invalid seller
                "district": "UnknownDistrict", # invalid district
                "pets_allowed": "maybe",    # invalid bool
            })

        issues = validate_residential_rows(bad_csv)
        self.assertTrue(any("Invalid url" in err for err in issues))
        self.assertTrue(any("Invalid seen_date" in err for err in issues))
        self.assertTrue(any("Non-positive price" in err for err in issues))
        self.assertTrue(any("Unexpected currency" in err for err in issues))
        self.assertTrue(any("Unexpected seller" in err for err in issues))
        self.assertTrue(any("Unknown Kyiv district" in err for err in issues))
        self.assertTrue(any("Invalid boolean" in err for err in issues))

    def test_run_full_verification_pipeline(self):
        """Tests the full run_full_verification runner function."""
        upsert_residential_offer(self.sample_res, db_path=self.test_db)
        upsert_commercial_offer(self.sample_comm, db_path=self.test_db)

        result = run_full_verification(
            db_path=self.test_db,
            output_dir=self.temp_dir.name,
            res_template=str(self.res_template),
            comm_template=str(self.comm_template),
        )
        self.assertTrue(result, "run_full_verification must return True when data is compliant")


if __name__ == "__main__":
    unittest.main()
