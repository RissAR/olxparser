#!/usr/bin/env python3
"""
Step 7: Verification and Test Run Script.

This script executes Step 7 of the development plan:
1. Performs a test scrape of real offers for residential and commercial categories (optional or configurable).
2. Exports scraped records to CSV with UTF-8 BOM (utf-8-sig).
3. Verifies generated CSV files against reference templates:
   - olx-manual-template.csv (23 columns)
   - olx-commercial-template.csv (35 columns)
4. Checks data types, enumerations, numeric formats, and boolean values.
5. Produces a detailed verification report.
"""

import argparse
import asyncio
import csv
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from database import (
    COMMERCIAL_COLUMNS,
    RESIDENTIAL_COLUMNS,
    get_all_commercial_offers,
    get_all_residential_offers,
    get_stats,
    init_db,
)
from exporter import (
    DEFAULT_COMMERCIAL_FILENAME,
    DEFAULT_RESIDENTIAL_FILENAME,
    export_commercial,
    export_residential,
    verify_csv_against_template,
)
from scraper import OLXScraper

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("verifier")

KYIV_DISTRICTS_SET = {
    "Голосіївський",
    "Дарницький",
    "Деснянський",
    "Дніпровський",
    "Оболонський",
    "Печерський",
    "Подільський",
    "Святошинський",
    "Солом'янський",
    "Шевченківський",
}

VALID_CURRENCIES = {"UAH", "USD", "EUR"}
VALID_RESIDENTIAL_SELLERS = {"owner", "agency", "developer"}
VALID_COMMERCIAL_SELLERS = {"owner", "agency", "developer"}
VALID_COMMERCIAL_PROPERTY_TYPES = {
    "office",
    "retail",
    "food",
    "warehouse",
    "production",
    "free_purpose",
    "medical_beauty",
    "education",
    "hotel",
}
VALID_PRICE_BASIS = {"month_total", "month_per_m2"}
VALID_CONDITIONS = {"ready", "shell_and_core", "white_box", "needs_repair"}
VALID_FURNITURE = {"none", "partial", "full"}
VALID_BUILDING_CLASSES = {"A", "B", "C"}
VALID_BOOLEANS = {"yes", "no", ""}


def validate_residential_rows(csv_path: Path) -> List[str]:
    """Validates data types and constraints in residential CSV rows."""
    issues: List[str] = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader, start=2):
            url = row.get("url", "")
            if not url or not url.startswith("http"):
                issues.append(f"Row {idx}: Invalid url '{url}'")

            seen_date = row.get("seen_date", "")
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", seen_date):
                issues.append(f"Row {idx}: Invalid seen_date '{seen_date}' (expected YYYY-MM-DD)")

            price = row.get("price", "")
            if price:
                try:
                    p_val = float(price)
                    if p_val <= 0:
                        issues.append(f"Row {idx}: Non-positive price '{price}'")
                except ValueError:
                    issues.append(f"Row {idx}: Invalid numeric price '{price}'")

            curr = row.get("currency", "")
            if curr and curr not in VALID_CURRENCIES:
                issues.append(f"Row {idx}: Unexpected currency '{curr}'")

            seller = row.get("seller", "")
            if seller and seller not in VALID_RESIDENTIAL_SELLERS:
                issues.append(f"Row {idx}: Unexpected seller '{seller}'")

            district = row.get("district", "")
            if district and district not in KYIV_DISTRICTS_SET:
                issues.append(f"Row {idx}: Unknown Kyiv district '{district}'")

            # Check booleans
            for bool_col in ("generator_in_building", "inverter_in_flat", "pets_allowed", "kids_allowed", "utilities_included"):
                val = row.get(bool_col, "")
                if val not in VALID_BOOLEANS:
                    issues.append(f"Row {idx}: Invalid boolean for {bool_col}: '{val}' (expected yes/no/empty)")

    return issues


def validate_commercial_rows(csv_path: Path) -> List[str]:
    """Validates data types and constraints in commercial CSV rows."""
    issues: List[str] = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader, start=2):
            url = row.get("url", "")
            if not url or not url.startswith("http"):
                issues.append(f"Row {idx}: Invalid url '{url}'")

            seen_date = row.get("seen_date", "")
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", seen_date):
                issues.append(f"Row {idx}: Invalid seen_date '{seen_date}' (expected YYYY-MM-DD)")

            prop_type = row.get("property_type", "")
            if prop_type and prop_type not in VALID_COMMERCIAL_PROPERTY_TYPES:
                issues.append(f"Row {idx}: Unexpected property_type '{prop_type}'")

            price_basis = row.get("price_basis", "")
            if price_basis and price_basis not in VALID_PRICE_BASIS:
                issues.append(f"Row {idx}: Unexpected price_basis '{price_basis}'")

            condition = row.get("condition", "")
            if condition and condition not in VALID_CONDITIONS:
                issues.append(f"Row {idx}: Unexpected condition '{condition}'")

            furniture = row.get("furniture", "")
            if furniture and furniture not in VALID_FURNITURE:
                issues.append(f"Row {idx}: Unexpected furniture '{furniture}'")

            b_class = row.get("building_class", "")
            if b_class and b_class not in VALID_BUILDING_CLASSES:
                issues.append(f"Row {idx}: Unexpected building_class '{b_class}'")

            district = row.get("district", "")
            if district and district not in KYIV_DISTRICTS_SET:
                issues.append(f"Row {idx}: Unknown Kyiv district '{district}'")

            # Check booleans
            commercial_bools = (
                "vat_included", "opex_included", "first_line", "separate_entrance",
                "shop_windows", "generator", "shelter_in_building", "ventilation_hood",
                "water_in_rooms", "loading_dock", "heated"
            )
            for bool_col in commercial_bools:
                val = row.get(bool_col, "")
                if val not in VALID_BOOLEANS:
                    issues.append(f"Row {idx}: Invalid boolean for {bool_col}: '{val}'")

    return issues


async def run_live_sample_scrape(max_offers: int = 5, db_path: str = "olx_storage.db") -> None:
    """Performs a controlled live sample scrape for both categories."""
    logger.info("--- Starting Test Scrape on Real OLX Listings ---")
    scraper = OLXScraper(db_path=db_path)

    # 1. Residential sample
    logger.info("Scraping sample residential offers (max_offers=%d)...", max_offers)
    res_status = await scraper.run(
        category="residential",
        max_offers=max_offers,
        max_slices=1,
    )
    logger.info(
        "Residential sample finished: status=%s, processed=%d, errors=%d",
        res_status.get("status"),
        res_status.get("processed_count", 0),
        res_status.get("errors_count", 0),
    )

    # 2. Commercial sample
    logger.info("Scraping sample commercial offers (max_offers=%d)...", max_offers)
    comm_status = await scraper.run(
        category="commercial",
        max_offers=max_offers,
        max_slices=1,
    )
    logger.info(
        "Commercial sample finished: status=%s, processed=%d, errors=%d",
        comm_status.get("status"),
        comm_status.get("processed_count", 0),
        comm_status.get("errors_count", 0),
    )


def run_full_verification(
    db_path: str = "olx_storage.db",
    output_dir: str = ".",
    res_template: str = "olx-manual-template.csv",
    comm_template: str = "olx-commercial-template.csv",
) -> bool:
    """
    Exports CSV files and runs complete Step 7 verification against templates.
    Returns True if all checks pass.
    """
    print("\n" + "=" * 70)
    print("        OLX KYIV REAL ESTATE PARSER - STEP 7 VERIFICATION")
    print("=" * 70)

    # 1. Database stats
    init_db(db_path)
    stats = get_stats(db_path)
    print(f"\n[1] Database State ({db_path}):")
    print(f"    - Residential offers stored: {stats['residential_count']}")
    print(f"    - Commercial offers stored:  {stats['commercial_count']}")
    latest_id = stats['latest_session']['id'] if stats.get('latest_session') else 'None'
    print(f"    - Latest scrape session ID:  #{latest_id}")

    # 2. Export to CSV
    print(f"\n[2] Exporting CSV files (utf-8-sig BOM)...")
    res_csv = Path(output_dir) / DEFAULT_RESIDENTIAL_FILENAME
    comm_csv = Path(output_dir) / DEFAULT_COMMERCIAL_FILENAME

    res_info = export_residential(db_path=db_path, output_path=res_csv)
    comm_info = export_commercial(db_path=db_path, output_path=comm_csv)

    print(f"    - Residential CSV: {res_info['path']} ({res_info['rows_count']} rows, {res_info['size_formatted']})")
    print(f"    - Commercial CSV:  {comm_info['path']} ({comm_info['rows_count']} rows, {comm_info['size_formatted']})")

    # 3. Verify against templates
    print(f"\n[3] Template Conformance Verification:")

    # Residential verification
    v_res = verify_csv_against_template(res_csv, res_template)
    res_issues = validate_residential_rows(res_csv)
    v_res_valid = v_res["valid"] and len(res_issues) == 0

    print(f"    • Residential ({res_csv.name} vs {res_template}):")
    print(f"        - Columns count:  {v_res.get('columns_count')} (Expected 23) [{'✓' if v_res.get('columns_count') == 23 else '✗'}]")
    print(f"        - Header matches: {'✓' if not any('Header mismatch' in e for e in v_res.get('errors', [])) else '✗'}")
    print(f"        - UTF-8 BOM:      {'✓' if not any('BOM' in e for e in v_res.get('errors', [])) else '✗'}")
    print(f"        - Data rows:      {v_res.get('rows_count')}")
    print(f"        - Row validation: {'✓ 0 issues' if not res_issues else f'✗ {len(res_issues)} issues'}")
    if res_issues:
        for err in res_issues[:5]:
            print(f"            ! {err}")

    # Commercial verification
    v_comm = verify_csv_against_template(comm_csv, comm_template)
    comm_issues = validate_commercial_rows(comm_csv)
    v_comm_valid = v_comm["valid"] and len(comm_issues) == 0

    print(f"\n    • Commercial ({comm_csv.name} vs {comm_template}):")
    print(f"        - Columns count:  {v_comm.get('columns_count')} (Expected 35) [{'✓' if v_comm.get('columns_count') == 35 else '✗'}]")
    print(f"        - Header matches: {'✓' if not any('Header mismatch' in e for e in v_comm.get('errors', [])) else '✗'}")
    print(f"        - UTF-8 BOM:      {'✓' if not any('BOM' in e for e in v_comm.get('errors', [])) else '✗'}")
    print(f"        - Data rows:      {v_comm.get('rows_count')}")
    print(f"        - Row validation: {'✓ 0 issues' if not comm_issues else f'✗ {len(comm_issues)} issues'}")
    if comm_issues:
        for err in comm_issues[:5]:
            print(f"            ! {err}")

    # 4. Final Verdict
    all_passed = v_res_valid and v_comm_valid
    print("\n" + "=" * 70)
    if all_passed:
        print(" [✓] STEP 7 VERIFICATION SUCCESSFUL: ALL CHECKS PASSED!")
    else:
        print(" [✗] STEP 7 VERIFICATION FAILED: ISSUES FOUND")
    print("=" * 70 + "\n")

    return all_passed


def main():
    parser = argparse.ArgumentParser(description="OLX Parser Step 7 Verification")
    parser.add_argument("--db", default="olx_storage.db", help="Path to SQLite database")
    parser.add_argument("--outdir", default=".", help="Directory for exported CSVs")
    parser.add_argument("--sample-scrape", action="store_true", help="Perform a live sample scrape on OLX before verification")
    parser.add_argument("--sample-size", type=int, default=5, help="Number of offers to scrape per category for sample run")
    parser.add_argument("--res-template", default="olx-manual-template.csv", help="Residential template CSV path")
    parser.add_argument("--comm-template", default="olx-commercial-template.csv", help="Commercial template CSV path")

    args = parser.parse_args()

    if args.sample_scrape:
        asyncio.run(run_live_sample_scrape(max_offers=args.sample_size, db_path=args.db))

    success = run_full_verification(
        db_path=args.db,
        output_dir=args.outdir,
        res_template=args.res_template,
        comm_template=args.comm_template,
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
