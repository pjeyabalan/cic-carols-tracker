from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore[assignment]
    RealDictCursor = None  # type: ignore[assignment]


def resolve_pg_dsn(explicit_dsn: Optional[str] = None) -> Optional[str]:
    """Resolve PostgreSQL connection string from explicit arg, DATABASE_URL (Render), or PG_DSN."""
    dsn = (explicit_dsn or os.getenv("DATABASE_URL") or os.getenv("PG_DSN") or "").strip()
    if not dsn:
        return None
    if "${PG_PASSWORD}" in dsn:
        pw = os.getenv("PG_PASSWORD")
        if pw:
            dsn = dsn.replace("${PG_PASSWORD}", pw)
        else:
            return None

    # Normalize Render postgres:// URL scheme to postgresql:// and ensure SSL on external Render hosts
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://") :]

    if dsn.startswith("postgresql://"):
        parsed = urlparse(dsn)
        host = (parsed.hostname or "").lower()
        query_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if any(d in host for d in ("render.com", "neon.tech", "supabase.co", "supabase.com")) and "sslmode" not in query_params:
            query_params["sslmode"] = "require"
            dsn = urlunparse(parsed._replace(query=urlencode(query_params)))

    return dsn


@dataclass(frozen=True)
class CarolsStop:
    """Represents a single carols visit record."""

    name: str
    family_members: List[str]
    address: str
    contact_number: Optional[str] = None
    email_id: Optional[str] = None
    prayer_request: Optional[str] = None
    zipcode: Optional[str] = None
    day: Optional[str] = None
    date: Optional[str] = None
    scheduled_time: Optional[str] = None
    visit_order: Optional[int] = None
    id: Optional[int] = None
    address_visited: Optional[bool] = None
    referral: Optional[str] = "church"


def _normalize_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip()
    return v if v else None


def _split_members(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [p.strip() for p in str(value).split(",") if p.strip()]


def _row_to_stop(row: dict) -> CarolsStop:
    visit_text = str(row.get("Visit Order") or "").strip()
    visit_parsed = int(visit_text) if visit_text.isdigit() else None
    id_text = str(row.get("Id") or "").strip()
    id_parsed = int(id_text) if id_text.isdigit() else None
    visited_val = row.get("Address Visited")
    if isinstance(visited_val, bool):
        visited_parsed: Optional[bool] = visited_val
    else:
        vtxt = str(visited_val or "").strip().lower()
        visited_parsed = (
            True
            if vtxt in ("true", "t", "1", "yes")
            else False
            if vtxt in ("false", "f", "0", "no")
            else None
        )
    return CarolsStop(
        name=_normalize_text(row.get("Name")) or "Unknown",
        family_members=_split_members(row.get("Family Members")),
        contact_number=_normalize_text(row.get("Contact Number")),
        email_id=_normalize_text(row.get("Email Id")),
        prayer_request=_normalize_text(row.get("Prayer Request")),
        address=_normalize_text(row.get("Address")) or "",
        zipcode=_normalize_text(row.get("Zipcode")),
        day=_normalize_text(row.get("Day")),
        date=_normalize_text(row.get("Date")),
        scheduled_time=_normalize_text(row.get("Scheduled Time")),
        visit_order=visit_parsed,
        id=id_parsed,
        address_visited=visited_parsed,
        referral=_normalize_text(row.get("Referral")) or "church",
    )


class CsvDataSource:
    """Loads carols stops from a CSV file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def get_stops(
        self,
        day: Optional[str] = None,
        time_window: Optional[tuple[str, str]] = None,
        date: Optional[str] = None,
    ) -> List[CarolsStop]:
        if not self.path.exists():
            return []
        rows = self._read_csv()
        stops = [_row_to_stop(row) for row in rows]
        if date:
            date_matches = [stop for stop in stops if (stop.date or "") == date]
            if date_matches or any(stop.date for stop in stops):
                stops = date_matches
            elif day:
                stops = [stop for stop in stops if (stop.day or "").lower() == day.lower()]
            else:
                stops = []
        elif day:
            stops = [stop for stop in stops if (stop.day or "").lower() == day.lower()]
        if time_window:
            start, end = time_window
            stops = [stop for stop in stops if _time_in_window(stop.scheduled_time, start, end)]
        return stops

    def _read_csv(self) -> List[dict[str, Optional[str]]]:
        with self.path.open(encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            return [row for row in reader]


def _time_in_window(value: Optional[str], start: str, end: str) -> bool:
    if not value:
        return False
    try:
        parsed = _parse_time(value)
        start_time = datetime.strptime(start, "%H:%M").time()
        end_time = datetime.strptime(end, "%H:%M").time()
        return start_time <= parsed <= end_time
    except ValueError:  # pragma: no cover
        return False


def _parse_time(value: str) -> time:
    return datetime.strptime(value.strip(), "%H:%M").time()


class PostgresDataSource:
    """Loads and manages carols stops in a Postgres database."""

    def __init__(self, dsn: str, auto_init: bool = True) -> None:
        if psycopg2 is None or RealDictCursor is None:
            raise RuntimeError("psycopg2 is required to load data from Postgres")
        self._dsn = dsn
        self._schema_ready = False
        if auto_init:
            try:
                self.ensure_schema()
            except Exception as exc:
                print(f"[WARN] Could not auto-initialize carols schema on startup: {exc}")

    def ensure_schema(self) -> None:
        """Idempotently create the isolated `carols` schema and `carols_schedule` table."""
        if self._schema_ready:
            return
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cursor:
                cursor.execute("CREATE SCHEMA IF NOT EXISTS carols;")
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS carols.carols_schedule (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL,
                        family_members TEXT,
                        contact_number TEXT,
                        email TEXT,
                        prayer_request TEXT,
                        address TEXT,
                        zipcode TEXT,
                        day TEXT,
                        date DATE,
                        scheduled_time TIME,
                        visit_order INTEGER,
                        address_visited BOOLEAN DEFAULT FALSE,
                        referral TEXT DEFAULT 'church',
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )
                # Ensure all expected columns exist for backwards compatibility
                alter_statements = [
                    "ALTER TABLE carols.carols_schedule ADD COLUMN IF NOT EXISTS family_members TEXT;",
                    "ALTER TABLE carols.carols_schedule ADD COLUMN IF NOT EXISTS contact_number TEXT;",
                    "ALTER TABLE carols.carols_schedule ADD COLUMN IF NOT EXISTS email TEXT;",
                    "ALTER TABLE carols.carols_schedule ADD COLUMN IF NOT EXISTS prayer_request TEXT;",
                    "ALTER TABLE carols.carols_schedule ADD COLUMN IF NOT EXISTS address TEXT;",
                    "ALTER TABLE carols.carols_schedule ADD COLUMN IF NOT EXISTS zipcode TEXT;",
                    "ALTER TABLE carols.carols_schedule ADD COLUMN IF NOT EXISTS day TEXT;",
                    "ALTER TABLE carols.carols_schedule ADD COLUMN IF NOT EXISTS date DATE;",
                    "ALTER TABLE carols.carols_schedule ADD COLUMN IF NOT EXISTS scheduled_time TIME;",
                    "ALTER TABLE carols.carols_schedule ADD COLUMN IF NOT EXISTS visit_order INTEGER;",
                    "ALTER TABLE carols.carols_schedule ADD COLUMN IF NOT EXISTS address_visited BOOLEAN DEFAULT FALSE;",
                    "ALTER TABLE carols.carols_schedule ADD COLUMN IF NOT EXISTS referral TEXT DEFAULT 'church';",
                    "ALTER TABLE carols.carols_schedule ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();",
                ]
                for stmt in alter_statements:
                    cursor.execute(stmt)
        self._schema_ready = True

    def get_stops(
        self,
        day: Optional[str] = None,
        time_window: Optional[tuple[str, str]] = None,
        date: Optional[str] = None,
    ) -> List[CarolsStop]:
        self.ensure_schema()
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                # Select columns; compute Day from date for display and fallback to stored day column
                try:
                    cursor.execute(
                        """
                        SELECT
                            name               AS "Name",
                            family_members     AS "Family Members",
                            contact_number     AS "Contact Number",
                            email              AS "Email Id",
                            prayer_request     AS "Prayer Request",
                            address            AS "Address",
                            zipcode            AS "Zipcode",
                            COALESCE(to_char(date, 'FMDay'), day) AS "Day",
                            to_char(date, 'YYYY-MM-DD') AS "Date",
                            to_char(scheduled_time, 'HH24:MI') AS "Scheduled Time",
                            visit_order        AS "Visit Order",
                            id                 AS "Id",
                            address_visited    AS "Address Visited",
                            referral           AS "Referral"
                        FROM carols.carols_schedule
                        ORDER BY date NULLS LAST, visit_order NULLS LAST, scheduled_time NULLS LAST, id ASC
                        """
                    )
                except Exception:
                    conn.rollback()
                    cursor.execute(
                        """
                        SELECT
                            name               AS "Name",
                            family_members     AS "Family Members",
                            contact_number     AS "Contact Number",
                            email              AS "Email Id",
                            prayer_request     AS "Prayer Request",
                            address            AS "Address",
                            zipcode            AS "Zipcode",
                            NULL               AS "Day",
                            to_char(scheduled_time, 'HH24:MI') AS "Scheduled Time",
                            visit_order        AS "Visit Order",
                            id                 AS "Id",
                            referral           AS "Referral"
                        FROM carols.carols_schedule
                        ORDER BY visit_order NULLS LAST, scheduled_time NULLS LAST
                        """
                    )
                rows = cursor.fetchall()
        stops = [_row_to_stop(row) for row in rows]
        # Prefer filtering by exact date; day is display-only now
        if date:
            stops = [stop for stop in stops if (stop.date or "") == date]
        elif day:
            stops = [stop for stop in stops if (stop.day or "").lower() == day.lower()]
        if time_window:
            start, end = time_window
            stops = [stop for stop in stops if _time_in_window(stop.scheduled_time, start, end)]
        return stops

    def add_stop(
        self,
        *,
        name: str,
        address: Optional[str] = None,
        family_members: Optional[List[str]] = None,
        contact_number: Optional[str] = None,
        email: Optional[str] = None,
        prayer_request: Optional[str] = None,
        zipcode: Optional[str] = None,
        day: Optional[str] = None,
        scheduled_time: Optional[str] = None,
        date: Optional[str] = None,
        visit_order: Optional[int] = None,
        address_visited: Optional[bool] = False,
        referral: Optional[str] = "church",
    ) -> Optional[int]:
        if psycopg2 is None:
            raise RuntimeError("psycopg2 is required to insert into Postgres")
        self.ensure_schema()
        family_members_str = ", ".join(family_members) if family_members else None
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cursor:
                def has_column(col: str) -> bool:
                    try:
                        cursor.execute(
                            "SELECT 1 FROM information_schema.columns WHERE table_schema='carols' AND table_name='carols_schedule' AND column_name=%s",
                            (col,),
                        )
                        return cursor.fetchone() is not None
                    except Exception:
                        return False

                include_date = has_column("date")
                include_day = has_column("day")

                # Auto-assign visit_order if missing when a date or day is specified
                if visit_order is None and (date is not None or day is not None):
                    if include_date and date is not None:
                        cursor.execute(
                            """
                            SELECT COALESCE(MAX(visit_order), 0) + 10
                            FROM carols.carols_schedule
                            WHERE date = %s
                            """,
                            (date,),
                        )
                    elif include_day and day is not None:
                        cursor.execute(
                            """
                            SELECT COALESCE(MAX(visit_order), 0) + 10
                            FROM carols.carols_schedule
                            WHERE day = %s
                            """,
                            (day,),
                        )
                    else:
                        cursor.execute(
                            """
                            SELECT COALESCE(MAX(visit_order), 0) + 10
                            FROM carols.carols_schedule
                            """
                        )
                    row = cursor.fetchone()
                    visit_order = int(row[0]) if row and row[0] is not None else 10

                cols = [
                    "name",
                    "family_members",
                    "contact_number",
                    "email",
                    "prayer_request",
                    "address",
                    "zipcode",
                ]
                params = [
                    name,
                    family_members_str,
                    contact_number,
                    email,
                    prayer_request,
                    address,
                    zipcode,
                ]
                if include_day:
                    cols.append("day")
                    params.append(day)
                if include_date:
                    cols.append("date")
                    params.append(date)
                cols.extend(["scheduled_time", "visit_order", "address_visited", "referral"])
                params.extend(
                    [
                        scheduled_time,
                        scheduled_time,
                        visit_order,
                        bool(address_visited) if address_visited is not None else False,
                        referral,
                    ]
                )

                placeholders = ["%s"] * (len(cols) - 4)
                placeholders.extend(
                    [
                        "CASE WHEN %s IS NOT NULL THEN to_timestamp(%s, 'HH24:MI')::time ELSE NULL END",
                        "%s",
                        "%s",
                        "COALESCE(%s, 'church')",
                    ]
                )

                cursor.execute(
                    f"""
                    INSERT INTO carols.carols_schedule ({', '.join(cols)})
                    VALUES ({', '.join(placeholders)})
                    RETURNING id
                    """,
                    params,
                )
                row = cursor.fetchone()
                return int(row[0]) if row and row[0] is not None else None

    def get_stop(
        self,
        *,
        name: str,
        address: str,
        day: Optional[str] = None,
        scheduled_time: Optional[str] = None,
    ) -> Optional[CarolsStop]:
        self.ensure_schema()
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT
                        name               AS "Name",
                        family_members     AS "Family Members",
                        contact_number     AS "Contact Number",
                        email              AS "Email Id",
                        prayer_request     AS "Prayer Request",
                        address            AS "Address",
                        zipcode            AS "Zipcode",
                        COALESCE(to_char(date, 'FMDay'), day) AS "Day",
                        to_char(date, 'YYYY-MM-DD') AS "Date",
                        to_char(scheduled_time, 'HH24:MI') AS "Scheduled Time",
                        visit_order        AS "Visit Order",
                        id                 AS "Id",
                        address_visited    AS "Address Visited",
                        referral           AS "Referral"
                    FROM carols.carols_schedule
                    WHERE name = %s AND address = %s
                        AND (
                            %s IS NULL OR (
                                scheduled_time = to_timestamp(%s, 'HH24:MI')::time
                            )
                        )
                    LIMIT 1
                    """,
                    (name, address, scheduled_time, scheduled_time),
                )
                row = cursor.fetchone()
        return _row_to_stop(row) if row else None

    def get_stop_by_id(self, item_id: int) -> Optional[CarolsStop]:
        self.ensure_schema()
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                try:
                    cursor.execute(
                        """
                        SELECT
                            name               AS "Name",
                            family_members     AS "Family Members",
                            contact_number     AS "Contact Number",
                            email              AS "Email Id",
                            prayer_request     AS "Prayer Request",
                            address            AS "Address",
                            zipcode            AS "Zipcode",
                            COALESCE(to_char(date, 'FMDay'), day) AS "Day",
                            to_char(date, 'YYYY-MM-DD') AS "Date",
                            to_char(scheduled_time, 'HH24:MI') AS "Scheduled Time",
                            visit_order        AS "Visit Order",
                            id                 AS "Id",
                            address_visited    AS "Address Visited",
                            referral           AS "Referral"
                        FROM carols.carols_schedule
                        WHERE id = %s
                        """,
                        (item_id,),
                    )
                except Exception:
                    conn.rollback()
                    cursor.execute(
                        """
                        SELECT
                            name               AS "Name",
                            family_members     AS "Family Members",
                            contact_number     AS "Contact Number",
                            email              AS "Email Id",
                            prayer_request     AS "Prayer Request",
                            address            AS "Address",
                            zipcode            AS "Zipcode",
                            NULL               AS "Day",
                            to_char(date, 'YYYY-MM-DD') AS "Date",
                            to_char(scheduled_time, 'HH24:MI') AS "Scheduled Time",
                            visit_order        AS "Visit Order",
                            id                 AS "Id",
                            referral           AS "Referral"
                        FROM carols.carols_schedule
                        WHERE id = %s
                        """,
                        (item_id,),
                    )
                row = cursor.fetchone()
        return _row_to_stop(row) if row else None

    def update_stop(
        self,
        *,
        name: str,
        address: str,
        day: Optional[str] = None,
        scheduled_time: Optional[str] = None,
        updates: dict,
    ) -> None:
        self.ensure_schema()
        allowed = {
            "name",
            "address",
            "family_members",
            "contact_number",
            "email",
            "prayer_request",
            "zipcode",
            "day",
            "date",
            "scheduled_time",
            "visit_order",
            "address_visited",
            "referral",
        }
        to_set = {k: v for k, v in updates.items() if k in allowed}
        if not to_set:
            return
        if "family_members" in to_set and isinstance(to_set["family_members"], list):
            to_set["family_members"] = (
                ", ".join([p.strip() for p in to_set["family_members"] if p.strip()]) or None
            )
        set_parts = []
        params: list = []
        for key, value in to_set.items():
            if key == "scheduled_time":
                set_parts.append(
                    "scheduled_time = CASE WHEN %s IS NOT NULL THEN to_timestamp(%s, 'HH24:MI')::time ELSE NULL END"
                )
                params.extend([value, value])
            elif key == "date":
                set_parts.append("date = %s")
                params.append(value)
            elif key == "address_visited":
                set_parts.append("address_visited = %s")
                params.append(bool(value) if value is not None else False)
            elif key == "referral":
                set_parts.append("referral = COALESCE(%s, 'church')")
                params.append(value)
            else:
                set_parts.append(f"{key} = %s")
                params.append(value)
        params.extend([name, address, scheduled_time, scheduled_time])
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE carols.carols_schedule
                    SET {', '.join(set_parts)}
                    WHERE name = %s AND address = %s
                      AND (
                        %s IS NULL OR (
                          scheduled_time = to_timestamp(%s, 'HH24:MI')::time
                        )
                      )
                    """,
                    params,
                )

    def update_stop_by_id(self, item_id: int, updates: dict) -> None:
        self.ensure_schema()
        allowed = {
            "name",
            "address",
            "family_members",
            "contact_number",
            "email",
            "prayer_request",
            "zipcode",
            "day",
            "date",
            "scheduled_time",
            "visit_order",
            "address_visited",
            "referral",
        }
        to_set = {k: v for k, v in updates.items() if k in allowed}
        if not to_set:
            return
        if "family_members" in to_set and isinstance(to_set["family_members"], list):
            to_set["family_members"] = (
                ", ".join([p.strip() for p in to_set["family_members"] if p.strip()]) or None
            )
        set_parts = []
        params: list = []
        for key, value in to_set.items():
            if key == "scheduled_time":
                set_parts.append(
                    "scheduled_time = CASE WHEN %s IS NOT NULL THEN to_timestamp(%s, 'HH24:MI')::time ELSE NULL END"
                )
                params.extend([value, value])
            elif key == "date":
                set_parts.append("date = %s")
                params.append(value)
            elif key == "address_visited":
                set_parts.append("address_visited = %s")
                params.append(bool(value) if value is not None else False)
            elif key == "referral":
                set_parts.append("referral = COALESCE(%s, 'church')")
                params.append(value)
            else:
                set_parts.append(f"{key} = %s")
                params.append(value)
        params.append(item_id)
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cursor:
                if to_set.get("date") and to_set.get("visit_order") is None:
                    cursor.execute(
                        """
                        SELECT visit_order
                        FROM carols.carols_schedule
                        WHERE id = %s
                        """,
                        (item_id,),
                    )
                    existing = cursor.fetchone()
                    if existing and existing[0] is None:
                        cursor.execute(
                            """
                            SELECT COALESCE(MAX(visit_order), 0) + 10
                            FROM carols.carols_schedule
                            WHERE date = %s
                            """,
                            (to_set["date"],),
                        )
                        max_row = cursor.fetchone()
                        next_order = int(max_row[0]) if max_row and max_row[0] is not None else 10
                        # Replace visit_order = None with next_order
                        for idx_part, part in enumerate(set_parts):
                            if part == "visit_order = %s":
                                # Count parameter index matching set_parts
                                param_offset = 0
                                for prev_part in set_parts[:idx_part]:
                                    param_offset += 2 if "CASE WHEN" in prev_part else 1
                                params[param_offset] = next_order
                                break
                cursor.execute(
                    f"""
                    UPDATE carols.carols_schedule
                    SET {', '.join(set_parts)}
                    WHERE id = %s
                    """,
                    params,
                )

    def reorder_stops(
        self, *, ids: List[int], date: Optional[str] = None, day: Optional[str] = None
    ) -> None:
        """Rewrite visit_order for the given list of ids in the provided sequence.

        If date is provided, constrain to that date; otherwise, optionally constrain to day.
        Assign visit_order values in steps of 10 to leave gaps for future inserts.
        """
        if not ids:
            return
        self.ensure_schema()
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cursor:
                where_clause = ""
                params: list = []
                if date:
                    where_clause = " AND date = %s"
                    params.append(date)
                elif day:
                    where_clause = " AND day = %s"
                    params.append(day)
                order_val = 10
                for item_id in ids:
                    cursor.execute(
                        f"""
                        UPDATE carols.carols_schedule
                        SET visit_order = %s
                        WHERE id = %s{where_clause}
                        """,
                        [order_val, item_id] + params,
                    )
                    order_val += 10

    def delete_stop_by_id(self, item_id: int) -> None:
        """Delete a stop by its numeric id."""
        self.ensure_schema()
        with psycopg2.connect(self._dsn) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM carols.carols_schedule
                    WHERE id = %s
                    """,
                    (item_id,),
                )
