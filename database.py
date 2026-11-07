"""
SQLite Storage Layer for OLX Parser.
Manages database initialization, schema definitions, and CRUD operations
for residential offers, commercial offers, and scrape sessions.
"""

import os
from pathlib import Path
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
    db_file = Path(path)
    if db_file.parent and not db_file.parent.exists():
        db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    """Initializes the database, runs safe column migrations, and creates indexes."""
    conn = get_db_connection(db_path)
    with conn:
        # Residential table (23 normalized fields + raw_data + source + sessions + timestamps)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS residential_offers (
                url TEXT PRIMARY KEY NOT NULL,
                source TEXT NOT NULL DEFAULT 'olx',
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
                first_session_id INTEGER,
                last_session_id INTEGER,
                raw_data TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
        """)

        # Commercial table (35 normalized fields + raw_data + source + sessions + timestamps)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS commercial_offers (
                url TEXT PRIMARY KEY NOT NULL,
                source TEXT NOT NULL DEFAULT 'olx',
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
                first_session_id INTEGER,
                last_session_id INTEGER,
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

        # Schema migration check for existing databases
        res_cols = {row[1] for row in conn.execute("PRAGMA table_info(residential_offers)").fetchall()}
        if res_cols:
            if "source" not in res_cols:
                conn.execute("ALTER TABLE residential_offers ADD COLUMN source TEXT NOT NULL DEFAULT 'olx';")
            if "first_session_id" not in res_cols:
                conn.execute("ALTER TABLE residential_offers ADD COLUMN first_session_id INTEGER;")
            if "last_session_id" not in res_cols:
                conn.execute("ALTER TABLE residential_offers ADD COLUMN last_session_id INTEGER;")

        comm_cols = {row[1] for row in conn.execute("PRAGMA table_info(commercial_offers)").fetchall()}
        if comm_cols:
            if "source" not in comm_cols:
                conn.execute("ALTER TABLE commercial_offers ADD COLUMN source TEXT NOT NULL DEFAULT 'olx';")
            if "first_session_id" not in comm_cols:
                conn.execute("ALTER TABLE commercial_offers ADD COLUMN first_session_id INTEGER;")
            if "last_session_id" not in comm_cols:
                conn.execute("ALTER TABLE commercial_offers ADD COLUMN last_session_id INTEGER;")

        # Indexes for fast querying and filtering
        conn.execute("CREATE INDEX IF NOT EXISTS idx_res_district ON residential_offers(district);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_res_seen_date ON residential_offers(seen_date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_res_source ON residential_offers(source);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_res_first_session ON residential_offers(first_session_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comm_district ON commercial_offers(district);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comm_property_type ON commercial_offers(property_type);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comm_seen_date ON commercial_offers(seen_date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comm_source ON commercial_offers(source);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comm_first_session ON commercial_offers(first_session_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_status ON scrape_sessions(status);")
    conn.close()


def upsert_residential_offer(
    data: Dict[str, Any],
    raw_data: Optional[str] = None,
    db_path: Optional[str] = None,
    session_id: Optional[int] = None,
) -> None:
    """Inserts or updates a residential offer with source and session tracking."""
    fields = [col for col in RESIDENTIAL_COLUMNS if col in data]
    cols = list(fields)
    params = {col: data[col] for col in cols}

    cols.append("source")
    params["source"] = data.get("source") or "olx"

    if session_id is not None:
        cols.extend(["first_session_id", "last_session_id"])
        params["first_session_id"] = session_id
        params["last_session_id"] = session_id

    if raw_data is not None:
        cols.append("raw_data")
        params["raw_data"] = raw_data

    cols.append("updated_at")
    placeholders = [":" + col if col != "updated_at" else "datetime('now', 'localtime')" for col in cols]

    update_cols = [
        f"{col} = excluded.{col}"
        for col in cols
        if col not in ("url", "first_session_id")
    ]

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


def upsert_commercial_offer(
    data: Dict[str, Any],
    raw_data: Optional[str] = None,
    db_path: Optional[str] = None,
    session_id: Optional[int] = None,
) -> None:
    """Inserts or updates a commercial offer with source and session tracking."""
    fields = [col for col in COMMERCIAL_COLUMNS if col in data]
    cols = list(fields)
    params = {col: data[col] for col in cols}

    cols.append("source")
    params["source"] = data.get("source") or "olx"

    if session_id is not None:
        cols.extend(["first_session_id", "last_session_id"])
        params["first_session_id"] = session_id
        params["last_session_id"] = session_id

    if raw_data is not None:
        cols.append("raw_data")
        params["raw_data"] = raw_data

    cols.append("updated_at")
    placeholders = [":" + col if col != "updated_at" else "datetime('now', 'localtime')" for col in cols]

    update_cols = [
        f"{col} = excluded.{col}"
        for col in cols
        if col not in ("url", "first_session_id")
    ]

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


def upsert_residential_offers_batch(
    offers: List[Any],
    db_path: Optional[str] = None,
    session_id: Optional[int] = None,
) -> int:
    """Batch inserts or updates residential offers in a single transaction with session tracking."""
    if not offers:
        return 0
    conn = get_db_connection(db_path)
    with conn:
        for item in offers:
            if isinstance(item, tuple):
                data, raw_data = item
            else:
                data, raw_data = item, None

            fields = [col for col in RESIDENTIAL_COLUMNS if col in data]
            cols = list(fields)
            params = {col: data[col] for col in cols}

            cols.append("source")
            params["source"] = data.get("source") or "olx"

            if session_id is not None:
                cols.extend(["first_session_id", "last_session_id"])
                params["first_session_id"] = session_id
                params["last_session_id"] = session_id

            if raw_data is not None:
                cols.append("raw_data")
                params["raw_data"] = raw_data

            cols.append("updated_at")
            placeholders = [":" + col if col != "updated_at" else "datetime('now', 'localtime')" for col in cols]

            update_cols = [
                f"{col} = excluded.{col}"
                for col in cols
                if col not in ("url", "first_session_id")
            ]

            sql = f"""
                INSERT INTO residential_offers ({', '.join(cols)})
                VALUES ({', '.join(placeholders)})
                ON CONFLICT(url) DO UPDATE SET
                {', '.join(update_cols)};
            """
            conn.execute(sql, params)
    conn.close()
    return len(offers)


def upsert_commercial_offers_batch(
    offers: List[Any],
    db_path: Optional[str] = None,
    session_id: Optional[int] = None,
) -> int:
    """Batch inserts or updates commercial offers in a single transaction with session tracking."""
    if not offers:
        return 0
    conn = get_db_connection(db_path)
    with conn:
        for item in offers:
            if isinstance(item, tuple):
                data, raw_data = item
            else:
                data, raw_data = item, None

            fields = [col for col in COMMERCIAL_COLUMNS if col in data]
            cols = list(fields)
            params = {col: data[col] for col in cols}

            cols.append("source")
            params["source"] = data.get("source") or "olx"

            if session_id is not None:
                cols.extend(["first_session_id", "last_session_id"])
                params["first_session_id"] = session_id
                params["last_session_id"] = session_id

            if raw_data is not None:
                cols.append("raw_data")
                params["raw_data"] = raw_data

            cols.append("updated_at")
            placeholders = [":" + col if col != "updated_at" else "datetime('now', 'localtime')" for col in cols]

            update_cols = [
                f"{col} = excluded.{col}"
                for col in cols
                if col not in ("url", "first_session_id")
            ]

            sql = f"""
                INSERT INTO commercial_offers ({', '.join(cols)})
                VALUES ({', '.join(placeholders)})
                ON CONFLICT(url) DO UPDATE SET
                {', '.join(update_cols)};
            """
            conn.execute(sql, params)
    conn.close()
    return len(offers)


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
    """Retrieves database summary metrics without executing DDL schema statements on each invocation."""
    conn = get_db_connection(db_path)
    try:
        res_count = conn.execute("SELECT COUNT(*) FROM residential_offers").fetchone()[0]
        comm_count = conn.execute("SELECT COUNT(*) FROM commercial_offers").fetchone()[0]
        res_by_source = dict(conn.execute("SELECT source, COUNT(*) FROM residential_offers GROUP BY source").fetchall())
        comm_by_source = dict(conn.execute("SELECT source, COUNT(*) FROM commercial_offers GROUP BY source").fetchall())
        latest_session = conn.execute("SELECT * FROM scrape_sessions ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        conn.close()
        init_db(db_path)
        conn = get_db_connection(db_path)
        res_count = conn.execute("SELECT COUNT(*) FROM residential_offers").fetchone()[0]
        comm_count = conn.execute("SELECT COUNT(*) FROM commercial_offers").fetchone()[0]
        res_by_source = dict(conn.execute("SELECT source, COUNT(*) FROM residential_offers GROUP BY source").fetchall())
        comm_by_source = dict(conn.execute("SELECT source, COUNT(*) FROM commercial_offers GROUP BY source").fetchall())
        latest_session = conn.execute("SELECT * FROM scrape_sessions ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()

    return {
        "residential_count": res_count,
        "commercial_count": comm_count,
        "residential_by_source": res_by_source,
        "commercial_by_source": comm_by_source,
        "latest_session": dict(latest_session) if latest_session else None,
    }


def get_all_residential_offers(
    db_path: Optional[str] = None,
    only_latest_date: bool = False,
    since_days: Optional[int] = None,
    source: Optional[str] = None,
    only_new: bool = False,
    session_id: Optional[int] = None,
    include_source: bool = False,
) -> List[Dict[str, Any]]:
    """Retrieves residential offers ordered by seen_date DESC, created_at DESC, with optional recency, source, and deduplication filtering."""
    init_db(db_path)
    conn = get_db_connection(db_path)
    select_cols = (["source"] + RESIDENTIAL_COLUMNS) if include_source else RESIDENTIAL_COLUMNS
    cols_sql = ", ".join(select_cols)
    where_clauses: List[str] = []
    params: List[Any] = []

    if source and source.strip().lower() not in ("all", "*"):
        where_clauses.append("source = ?")
        params.append(source.strip().lower())

    if only_new:
        if session_id is not None:
            where_clauses.append("first_session_id = ?")
            params.append(session_id)
        else:
            where_clauses.append("first_session_id = (SELECT MAX(id) FROM scrape_sessions)")
    elif only_latest_date:
        where_clauses.append("seen_date = (SELECT MAX(seen_date) FROM residential_offers WHERE seen_date IS NOT NULL)")
    elif since_days is not None:
        where_clauses.append(f"seen_date >= date((SELECT MAX(seen_date) FROM residential_offers WHERE seen_date IS NOT NULL), '-{int(since_days)} days')")

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    rows = conn.execute(f"SELECT {cols_sql} FROM residential_offers {where_sql} ORDER BY seen_date DESC, created_at DESC", params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_commercial_offers(
    db_path: Optional[str] = None,
    only_latest_date: bool = False,
    since_days: Optional[int] = None,
    source: Optional[str] = None,
    only_new: bool = False,
    session_id: Optional[int] = None,
    include_source: bool = False,
) -> List[Dict[str, Any]]:
    """Retrieves commercial offers ordered by seen_date DESC, created_at DESC, with optional recency, source, and deduplication filtering."""
    init_db(db_path)
    conn = get_db_connection(db_path)
    select_cols = (["source"] + COMMERCIAL_COLUMNS) if include_source else COMMERCIAL_COLUMNS
    cols_sql = ", ".join(select_cols)
    where_clauses: List[str] = []
    params: List[Any] = []

    if source and source.strip().lower() not in ("all", "*"):
        where_clauses.append("source = ?")
        params.append(source.strip().lower())

    if only_new:
        if session_id is not None:
            where_clauses.append("first_session_id = ?")
            params.append(session_id)
        else:
            where_clauses.append("first_session_id = (SELECT MAX(id) FROM scrape_sessions)")
    elif only_latest_date:
        where_clauses.append("seen_date = (SELECT MAX(seen_date) FROM commercial_offers WHERE seen_date IS NOT NULL)")
    elif since_days is not None:
        where_clauses.append(f"seen_date >= date((SELECT MAX(seen_date) FROM commercial_offers WHERE seen_date IS NOT NULL), '-{int(since_days)} days')")

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    rows = conn.execute(f"SELECT {cols_sql} FROM commercial_offers {where_sql} ORDER BY seen_date DESC, created_at DESC", params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully at:", DEFAULT_DB_PATH)
    print("Current stats:", get_stats())
