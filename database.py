"""
SQLite Storage Layer for OLX Parser.
Manages database initialization, schema definitions, and CRUD operations
for residential offers, commercial offers, and scrape sessions.
"""

import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

DEFAULT_DB_PATH = os.getenv("OLX_DB_PATH", "olx_storage.db")

RESIDENTIAL_COLUMNS = [
    "url",
    "seen_date",
    "price",
    "currency",
    "seller",
    "street",
    "house",
    "district",
    "rooms",
    "area_total",
    "area_kitchen",
    "floor",
    "floors_total",
    "building_type",
    "renovation",
    "furniture",
    "appliances",
    "generator_in_building",
    "inverter_in_flat",
    "pets_allowed",
    "kids_allowed",
    "commission_pct",
    "utilities_included",
]

COMMERCIAL_COLUMNS = [
    "url",
    "seen_date",
    "property_type",
    "price",
    "currency",
    "price_basis",
    "vat_included",
    "opex_included",
    "opex_uah_m2",
    "seller",
    "street",
    "house",
    "district",
    "business_center_name",
    "building_class",
    "area_total",
    "floor",
    "floors_total",
    "first_line",
    "separate_entrance",
    "shop_windows",
    "condition",
    "furniture",
    "ceiling_height_m",
    "power_kw",
    "parking_spaces",
    "generator",
    "shelter_in_building",
    "ventilation_hood",
    "water_in_rooms",
    "loading_dock",
    "floor_load_t_m2",
    "heated",
    "min_lease_months",
    "commission_pct",
]


def get_db_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Creates a database connection with row factory enabled."""
    path = db_path or DEFAULT_DB_PATH
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    """Initializes the database and creates tables and indexes if they do not exist."""
    conn = get_db_connection(db_path)
    with conn:
        # Residential table (23 normalized fields + raw_data + timestamps)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS residential_offers (
                url TEXT PRIMARY KEY NOT NULL,
                seen_date TEXT,
                price REAL,
                currency TEXT,
                seller TEXT,
                street TEXT,
                house TEXT,
                district TEXT,
                rooms INTEGER,
                area_total REAL,
                area_kitchen REAL,
                floor INTEGER,
                floors_total INTEGER,
                building_type TEXT,
                renovation TEXT,
                furniture TEXT,
                appliances TEXT,
                generator_in_building TEXT,
                inverter_in_flat TEXT,
                pets_allowed TEXT,
                kids_allowed TEXT,
                commission_pct REAL,
                utilities_included TEXT,
                raw_data TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
        """)

        # Commercial table (35 normalized fields + raw_data + timestamps)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS commercial_offers (
                url TEXT PRIMARY KEY NOT NULL,
                seen_date TEXT,
                property_type TEXT,
                price REAL,
                currency TEXT,
                price_basis TEXT,
                vat_included TEXT,
                opex_included TEXT,
                opex_uah_m2 REAL,
                seller TEXT,
                street TEXT,
                house TEXT,
                district TEXT,
                business_center_name TEXT,
                building_class TEXT,
                area_total REAL,
                floor INTEGER,
                floors_total INTEGER,
                first_line TEXT,
                separate_entrance TEXT,
                shop_windows TEXT,
                condition TEXT,
                furniture TEXT,
                ceiling_height_m REAL,
                power_kw REAL,
                parking_spaces INTEGER,
                generator TEXT,
                shelter_in_building TEXT,
                ventilation_hood TEXT,
                water_in_rooms TEXT,
                loading_dock TEXT,
                floor_load_t_m2 REAL,
                heated TEXT,
                min_lease_months INTEGER,
                commission_pct REAL,
                raw_data TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
        """)

        # Scrape sessions table for progress tracking
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scrape_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                total_found INTEGER DEFAULT 0,
                processed_count INTEGER DEFAULT 0,
                errors_count INTEGER DEFAULT 0,
                started_at TEXT DEFAULT (datetime('now', 'localtime')),
                finished_at TEXT,
                error_message TEXT
            );
        """)

        # Indexes for fast querying and filtering
        conn.execute("CREATE INDEX IF NOT EXISTS idx_res_district ON residential_offers(district);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_res_seen_date ON residential_offers(seen_date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comm_district ON commercial_offers(district);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comm_property_type ON commercial_offers(property_type);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comm_seen_date ON commercial_offers(seen_date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_status ON scrape_sessions(status);")
    conn.close()


def upsert_residential_offer(data: Dict[str, Any], raw_data: Optional[str] = None, db_path: Optional[str] = None) -> None:
    """Inserts or updates a residential offer."""
    fields = [col for col in RESIDENTIAL_COLUMNS if col in data]
    cols = list(fields)
    placeholders = [":" + col for col in cols]
    params = {col: data[col] for col in cols}

    if raw_data is not None:
        cols.append("raw_data")
        placeholders.append(":raw_data")
        params["raw_data"] = raw_data

    cols.append("updated_at")
    placeholders.append("datetime('now', 'localtime')")

    update_cols = [f"{col} = excluded.{col}" for col in cols if col != "url"]

    sql = f"""
        INSERT INTO residential_offers ({', '.join(cols)})
        VALUES ({', '.join(placeholders)})
        ON CONFLICT(url) DO UPDATE SET
        {', '.join(update_cols)};
    """

    conn = get_db_connection(db_path)
    with conn:
        conn.execute(sql, params)
    conn.close()


def upsert_commercial_offer(data: Dict[str, Any], raw_data: Optional[str] = None, db_path: Optional[str] = None) -> None:
    """Inserts or updates a commercial offer."""
    fields = [col for col in COMMERCIAL_COLUMNS if col in data]
    cols = list(fields)
    placeholders = [":" + col for col in cols]
    params = {col: data[col] for col in cols}

    if raw_data is not None:
        cols.append("raw_data")
        placeholders.append(":raw_data")
        params["raw_data"] = raw_data

    cols.append("updated_at")
    placeholders.append("datetime('now', 'localtime')")

    update_cols = [f"{col} = excluded.{col}" for col in cols if col != "url"]

    sql = f"""
        INSERT INTO commercial_offers ({', '.join(cols)})
        VALUES ({', '.join(placeholders)})
        ON CONFLICT(url) DO UPDATE SET
        {', '.join(update_cols)};
    """

    conn = get_db_connection(db_path)
    with conn:
        conn.execute(sql, params)
    conn.close()


def create_scrape_session(category: str, db_path: Optional[str] = None) -> int:
    """Creates a new scrape session record and returns its ID."""
    conn = get_db_connection(db_path)
    with conn:
        cursor = conn.execute(
            "INSERT INTO scrape_sessions (category, status, total_found, processed_count, errors_count) VALUES (?, 'running', 0, 0, 0)",
            (category,)
        )
        session_id = cursor.lastrowid
    conn.close()
    return session_id


def update_scrape_session(
    session_id: int,
    total_found: Optional[int] = None,
    processed_count: Optional[int] = None,
    errors_count: Optional[int] = None,
    status: Optional[str] = None,
    error_message: Optional[str] = None,
    db_path: Optional[str] = None
) -> None:
    """Updates an existing scrape session's progress or status."""
    updates = []
    params: List[Any] = []

    if total_found is not None:
        updates.append("total_found = ?")
        params.append(total_found)
    if processed_count is not None:
        updates.append("processed_count = ?")
        params.append(processed_count)
    if errors_count is not None:
        updates.append("errors_count = ?")
        params.append(errors_count)
    if status is not None:
        updates.append("status = ?")
        params.append(status)
        if status in ("completed", "failed", "stopped"):
            updates.append("finished_at = datetime('now', 'localtime')")
    if error_message is not None:
        updates.append("error_message = ?")
        params.append(error_message)

    if not updates:
        return

    params.append(session_id)
    sql = f"UPDATE scrape_sessions SET {', '.join(updates)} WHERE id = ?"

    conn = get_db_connection(db_path)
    with conn:
        conn.execute(sql, params)
    conn.close()


def get_scrape_session(session_id: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieves session details by session ID."""
    conn = get_db_connection(db_path)
    row = conn.execute("SELECT * FROM scrape_sessions WHERE id = ?", (session_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_stats(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Retrieves database summary metrics."""
    conn = get_db_connection(db_path)
    res_count = conn.execute("SELECT COUNT(*) FROM residential_offers").fetchone()[0]
    comm_count = conn.execute("SELECT COUNT(*) FROM commercial_offers").fetchone()[0]
    latest_session = conn.execute("SELECT * FROM scrape_sessions ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return {
        "residential_count": res_count,
        "commercial_count": comm_count,
        "latest_session": dict(latest_session) if latest_session else None,
    }


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully at:", DEFAULT_DB_PATH)
    print("Current stats:", get_stats())
