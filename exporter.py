"""
CSV Exporter Module for OLX Parser.
Implements Step 4 of the Development Plan:
- Exports residential offers to 'olx_residential_kyiv.csv' (strictly 23 columns).
- Exports commercial offers to 'olx_commercial_kyiv.csv' (strictly 35 columns).
- Strictly follows utf-8-sig encoding (UTF-8 with BOM for Excel compatibility).
- Formats numbers with dot decimal separators, integers without decimals.
- Provides metadata inspection and template verification helpers for FastAPI dashboard.
"""

import argparse
import csv
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from database import (
    COMMERCIAL_COLUMNS,
    DEFAULT_DB_PATH,
    RESIDENTIAL_COLUMNS,
    get_all_commercial_offers,
    get_all_residential_offers,
)

DEFAULT_RESIDENTIAL_FILENAME = "olx_residential_kyiv.csv"
DEFAULT_COMMERCIAL_FILENAME = "olx_commercial_kyiv.csv"
DEFAULT_EXPORTS_DIR = "exports"

# Set of columns where integer values are strictly expected when whole
INTEGER_LIKE_COLUMNS = {
    "price",
    "rooms",
    "floor",
    "floors_total",
    "parking_spaces",
    "min_lease_months",
    "commission_pct",
}

# Set of columns where floating point values may appear
FLOAT_LIKE_COLUMNS = {
    "area_total",
    "area_kitchen",
    "opex_uah_m2",
    "ceiling_height_m",
    "power_kw",
    "floor_load_t_m2",
}


def format_csv_value(val: Any, col_name: str = "") -> str:
    """
    Formats a database value for clean CSV output:
    - None or empty string -> empty string
    - Floats/integers -> dot as decimal separator, integers without .0
    - Booleans -> 'yes' or 'no'
    - Strings -> trimmed, without extra whitespace
    """
    if val is None:
        return ""

    if isinstance(val, bool):
        return "yes" if val else "no"

    if isinstance(val, int):
        return str(val)

    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return ""
        if val.is_integer():
            if col_name == "ceiling_height_m":
                return f"{val:.1f}"
            return str(int(val))
        return f"{val:.2f}".rstrip("0").rstrip(".")

    if isinstance(val, str):
        s = val.strip()
        if not s:
            return ""
        # Check if the string is numeric with an unnecessary trailing .0
        if col_name in INTEGER_LIKE_COLUMNS:
            try:
                f = float(s.replace(",", "."))
                if f.is_integer():
                    return str(int(f))
            except ValueError:
                pass
        return s

    return str(val).strip()


def format_file_size(size_bytes: int) -> str:
    """Formats file size in bytes to a human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.2f} MB"


def get_file_info(file_path: Union[str, Path]) -> Optional[Dict[str, Any]]:
    """
    Inspects an exported CSV file and returns its metadata:
    filename, path, size_bytes, size_formatted, rows_count, created_at, modified_at.
    """
    path = Path(file_path)
    if not path.is_file():
        return None

    stat = path.stat()
    rows_count = 0
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header is not None:
                for _ in reader:
                    rows_count += 1
    except Exception:
        rows_count = 0

    return {
        "filename": path.name,
        "path": str(path.resolve()),
        "size_bytes": stat.st_size,
        "size_formatted": format_file_size(stat.st_size),
        "rows_count": rows_count,
        "created_at": datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M:%S"),
        "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }


def list_exported_files(directories: Optional[List[Union[str, Path]]] = None) -> List[Dict[str, Any]]:
    """
    Finds all exported CSV files in designated search directories
    (by default, project root and 'exports/' folder) and returns their metadata.
    """
    search_dirs = directories or [".", DEFAULT_EXPORTS_DIR]
    known_paths = set()
    files_info = []

    for d in search_dirs:
        dir_path = Path(d)
        if not dir_path.exists() or not dir_path.is_dir():
            continue
        for p in dir_path.glob("*.csv"):
            abs_p = str(p.resolve())
            # Skip templates themselves from the export list
            if p.name in ("olx-manual-template.csv", "olx-commercial-template.csv"):
                continue
            if abs_p not in known_paths:
                known_paths.add(abs_p)
                info = get_file_info(p)
                if info:
                    files_info.append(info)

    # Sort files by last modified timestamp descending
    files_info.sort(key=lambda x: x.get("modified_at", ""), reverse=True)
    return files_info


def export_residential(
    db_path: Optional[str] = None,
    output_path: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """
    Exports all residential offers to a CSV file with strictly 23 columns
    matching olx-manual-template.csv with utf-8-sig encoding.
    """
    target = Path(output_path or DEFAULT_RESIDENTIAL_FILENAME)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)

    offers = get_all_residential_offers(db_path=db_path)

    with open(target, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=",", quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        writer.writerow(RESIDENTIAL_COLUMNS)
        for offer in offers:
            row = [format_csv_value(offer.get(col), col) for col in RESIDENTIAL_COLUMNS]
            writer.writerow(row)

    info = get_file_info(target)
    if not info:
        raise IOError(f"Failed to retrieve info for exported file: {target}")
    return info


def export_commercial(
    db_path: Optional[str] = None,
    output_path: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """
    Exports all commercial offers to a CSV file with strictly 35 columns
    matching olx-commercial-template.csv with utf-8-sig encoding.
    """
    target = Path(output_path or DEFAULT_COMMERCIAL_FILENAME)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)

    offers = get_all_commercial_offers(db_path=db_path)

    with open(target, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=",", quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        writer.writerow(COMMERCIAL_COLUMNS)
        for offer in offers:
            row = [format_csv_value(offer.get(col), col) for col in COMMERCIAL_COLUMNS]
            writer.writerow(row)

    info = get_file_info(target)
    if not info:
        raise IOError(f"Failed to retrieve info for exported file: {target}")
    return info


def export_all(
    db_path: Optional[str] = None,
    output_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Exports both residential and commercial datasets.
    If output_dir is given, files are placed in that directory.
    Otherwise, written to the current working directory.
    """
    if output_dir:
        out = Path(output_dir)
        res_target = out / DEFAULT_RESIDENTIAL_FILENAME
        comm_target = out / DEFAULT_COMMERCIAL_FILENAME
    else:
        res_target = Path(DEFAULT_RESIDENTIAL_FILENAME)
        comm_target = Path(DEFAULT_COMMERCIAL_FILENAME)

    res_info = export_residential(db_path=db_path, output_path=res_target)
    comm_info = export_commercial(db_path=db_path, output_path=comm_target)

    return {
        "residential": res_info,
        "commercial": comm_info,
    }


def verify_csv_against_template(
    csv_path: Union[str, Path],
    template_path: Union[str, Path],
) -> Dict[str, Any]:
    """
    Verifies that a generated CSV strictly conforms to the template:
    1. Checks utf-8-sig BOM header (bytes: \\xef\\xbb\\xbf).
    2. Compares header column names and their exact sequence.
    3. Checks that every row has the exact expected number of columns.
    """
    errors: List[str] = []
    c_path = Path(csv_path)
    t_path = Path(template_path)

    if not c_path.is_file():
        return {"valid": False, "errors": [f"CSV file not found: {csv_path}"]}
    if not t_path.is_file():
        return {"valid": False, "errors": [f"Template file not found: {template_path}"]}

    # 1. BOM check
    with open(c_path, "rb") as bf:
        prefix = bf.read(3)
        if prefix != b"\xef\xbb\xbf":
            errors.append("File does not start with UTF-8 BOM (\\xef\\xbb\\xbf). Expected utf-8-sig.")

    # 2. Header and row validation
    with open(t_path, "r", encoding="utf-8-sig") as tf:
        template_header = next(csv.reader(tf), [])

    expected_cols = len(template_header)
    row_count = 0

    with open(c_path, "r", encoding="utf-8-sig") as cf:
        reader = csv.reader(cf)
        actual_header = next(reader, None)

        if actual_header is None:
            errors.append("CSV file is completely empty.")
        elif actual_header != template_header:
            errors.append(
                f"Header mismatch. Expected {len(template_header)} columns: {template_header}. "
                f"Got {len(actual_header)} columns: {actual_header}"
            )

        if actual_header is not None:
            for idx, row in enumerate(reader, start=1):
                row_count += 1
                if len(row) != expected_cols:
                    errors.append(
                        f"Row #{idx} has {len(row)} columns, expected {expected_cols}."
                    )

    return {
        "valid": len(errors) == 0,
        "columns_count": expected_cols,
        "rows_count": row_count,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description="OLX Parser CSV Exporter (Step 4)")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH, help="Path to SQLite database")
    parser.add_argument("--outdir", type=str, default="", help="Directory for exported CSV files")
    parser.add_argument(
        "--category",
        type=str,
        choices=["all", "residential", "commercial"],
        default="all",
        help="Category to export (default: all)",
    )
    args = parser.parse_args()

    out_dir = args.outdir if args.outdir else None
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting CSV export from DB: {args.db}")

    if args.category in ("all", "residential"):
        res_target = Path(out_dir) / DEFAULT_RESIDENTIAL_FILENAME if out_dir else None
        res_info = export_residential(db_path=args.db, output_path=res_target)
        print(f" -> Residential exported: {res_info['path']} ({res_info['rows_count']} rows, {res_info['size_formatted']})")

        # Verify against template if present in current dir
        if Path("olx-manual-template.csv").exists():
            v = verify_csv_against_template(res_info["path"], "olx-manual-template.csv")
            status = "PASSED" if v["valid"] else f"FAILED: {v['errors']}"
            print(f"    Verification vs olx-manual-template.csv: {status}")

    if args.category in ("all", "commercial"):
        comm_target = Path(out_dir) / DEFAULT_COMMERCIAL_FILENAME if out_dir else None
        comm_info = export_commercial(db_path=args.db, output_path=comm_target)
        print(f" -> Commercial exported: {comm_info['path']} ({comm_info['rows_count']} rows, {comm_info['size_formatted']})")

        # Verify against template if present in current dir
        if Path("olx-commercial-template.csv").exists():
            v = verify_csv_against_template(comm_info["path"], "olx-commercial-template.csv")
            status = "PASSED" if v["valid"] else f"FAILED: {v['errors']}"
            print(f"    Verification vs olx-commercial-template.csv: {status}")

    print("CSV export completed successfully.")


if __name__ == "__main__":
    main()
