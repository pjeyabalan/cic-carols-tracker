from __future__ import annotations

import base64
import csv
import io
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, make_response, redirect, render_template, request, url_for

from tracker.data_sources import (
    CarolsStop,
    CsvDataSource,
    PostgresDataSource,
    resolve_pg_dsn,
)
from tracker.gps import Coordinate, RoutePlan, simulate_live_updates, simulate_route

app = Flask(
    __name__,
    template_folder=Path(__file__).resolve().parent / "templates",
)

FAVICON_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0"
    "lEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="
)


def _resolve_csv_path() -> Path:
    base = Path(__file__).resolve().parents[1]
    return base / "data" / "carols_schedule.csv"


pg_dsn = resolve_pg_dsn()
if pg_dsn:
    data_source = PostgresDataSource(pg_dsn)
else:
    data_source = CsvDataSource(_resolve_csv_path())


@app.route("/registrations/delete/<int:item_id>", methods=["POST"])
def delete_registration(item_id: int):
    using_postgres = isinstance(data_source, PostgresDataSource)
    if not using_postgres:
        return jsonify({"success": False, "error": "Deleting requires DATABASE_URL or PG_DSN."}), 400
    try:
        data_source.delete_stop_by_id(item_id)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def _parse_float(value: Optional[str], default: float) -> float:
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _parse_time_window(start: Optional[str], end: Optional[str]) -> Optional[tuple[str, str]]:
    if start and end:
        return start, end
    return None


def _split_address_components(full_address: Optional[str]) -> tuple[str, Optional[str], Optional[str]]:
    """Split full address into street, city, state using commas.
    If last token is a 2-letter state (e.g., IL), treat it as state; next token as city.
    """
    if not full_address:
        return "", None, None
    parts = [p.strip() for p in str(full_address).split(",") if p is not None]
    if not parts:
        return "", None, None
    street = ""
    city: Optional[str] = None
    state: Optional[str] = None
    if len(parts) >= 3:
        maybe_state = parts[-1]
        if len(maybe_state) == 2 and maybe_state.isalpha():
            state = maybe_state.upper()
            city = parts[-2] or None
            street = ", ".join(parts[:-2]).strip()
        else:
            city = parts[-2] or None
            street = ", ".join(parts[:-1]).strip()
    elif len(parts) == 2:
        city = parts[1] or None
        street = parts[0]
    else:
        street = parts[0]
    return street, city, state


def _compose_address(street: str, city: Optional[str], state: Optional[str]) -> str:
    parts = [street.strip()] if street else []
    if city:
        parts.append(city.strip())
    if state:
        parts.append(state.strip().upper())
    return ", ".join([p for p in parts if p])


def _now_local() -> datetime:
    """Return current time in Central Standard Time (UTC-6) by default.

    If you prefer DST-aware behavior, set APP_TIMEZONE to an IANA zone like
    "America/Chicago". If APP_TIMEZONE is set to "CST" or "UTC-6", a fixed
    offset of -06:00 will be used (no DST).
    """
    tz_name = os.getenv("APP_TIMEZONE", "America/Chicago")
    if tz_name.upper() in {"CST", "UTC-6", "UTC-06", "UTC-06:00"}:
        tz = timezone(timedelta(hours=-6))
        return datetime.now(tz)
    # Try IANA zone if provided
    try:
        tz = ZoneInfo(tz_name)
        return datetime.now(tz)
    except Exception:
        # Fallback to fixed CST (no DST)
        tz = timezone(timedelta(hours=-6))
        return datetime.now(tz)


@app.route("/healthz")
def healthz():
    """Health check endpoint for Render."""
    return jsonify(
        {
            "ok": True,
            "database": "postgres" if isinstance(data_source, PostgresDataSource) else "csv",
        }
    )


@app.route("/favicon.ico")
def favicon():
    response = make_response(FAVICON_BYTES)
    response.headers["Content-Type"] = "image/png"
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@dataclass(frozen=True)
class StopDisplay:
    stop: CarolsStop
    display_time: str
    status: str
    badge: str
    css_class: str
    is_current: bool


def _parse_hhmm(value: Optional[str]) -> Optional[time]:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError:  # pragma: no cover
        return None


def _annotate_stops(stops: Iterable[CarolsStop], reference: datetime) -> list[StopDisplay]:
    annotated: list[StopDisplay] = []
    sorted_stops = sorted(stops, key=_sort_key)
    # Determine the first unvisited index using address_visited when available; fallback to time
    first_pending_index = None
    for idx, stop in enumerate(sorted_stops):
        if not _is_done(reference, stop):
            first_pending_index = idx
            break
    for idx, stop in enumerate(sorted_stops):
        scheduled = _parse_hhmm(stop.scheduled_time)
        display_time = scheduled.strftime("%I:%M %p") if scheduled else "TBD"
        # Date-aware status
        done = _is_done(reference, stop)
        status = "done" if done else "pending"
        badge = "Completed" if done else "Upcoming"
        css_class = "status-done" if done else "status-pending"
        is_current = first_pending_index is not None and idx == first_pending_index
        annotated.append(
            StopDisplay(
                stop=stop,
                display_time=display_time,
                status=status,
                badge=badge,
                css_class=css_class,
                is_current=is_current,
            )
        )
    return annotated


def _sort_key(stop: CarolsStop) -> tuple[int, int]:
    # Prefer explicit visit order if provided; fall back to time
    if stop.visit_order is not None:
        return (stop.visit_order, 0)
    parsed = _parse_hhmm(stop.scheduled_time)
    if parsed:
        return (parsed.hour * 60 + parsed.minute, 0)
    return (9999, 0)


def _scheduled_time(reference: datetime, stop: CarolsStop) -> datetime:
    scheduled = _parse_hhmm(stop.scheduled_time)
    if not scheduled:
        return reference
    return reference.replace(hour=scheduled.hour, minute=scheduled.minute, second=0, microsecond=0)


def _is_done(reference: datetime, stop: CarolsStop) -> bool:
    """Strictly use address_visited to determine done/pending; ignore time/date."""
    return bool(stop.address_visited) if stop.address_visited is not None else False


@app.route("/tracker")
def tracker() -> str:  # pragma: no cover - UI entrypoint for tracker
    today_dt = _now_local()
    today_name = today_dt.strftime("%A")
    iso_today = today_dt.strftime("%Y-%m-%d")
    # If today (or an explicit date param) is the special day, render the special two-team view
    special_date = "2025-12-12"
    date_param = (request.args.get("date") or "").strip()
    if date_param:
        try:
            datetime.strptime(date_param, "%Y-%m-%d")
        except ValueError:
            date_param = ""
    force_special_window = iso_today in {"2025-12-11", "2025-12-12"}
    effective_date = special_date if force_special_window and not date_param else (date_param or iso_today)
    if effective_date == special_date:
        try:
            stops = data_source.get_stops(date=effective_date)
        except TypeError:
            try:
                d = datetime.strptime(effective_date, "%Y-%m-%d").date()
                day_name = d.strftime("%A")
            except Exception:
                day_name = None
            stops = data_source.get_stops(day=day_name) if day_name else []

        team1: list[CarolsStop] = []
        team2: list[CarolsStop] = []
        others: list[CarolsStop] = []
        for s in stops:
            ref = (s.referral or "").strip().upper()
            if ref == "TEAM1":
                team1.append(s)
            elif ref == "TEAM2":
                team2.append(s)
            else:
                others.append(s)

        team1_sorted = sorted(team1, key=_sort_key)
        team2_sorted = sorted(team2, key=_sort_key)
        others_sorted = sorted(others, key=_sort_key)
        others_target = "team1" if len(team1_sorted) >= len(team2_sorted) else "team2"

        return render_template(
            "tracker_special.html",
            date_iso=effective_date,
            date_display=datetime.strptime(effective_date, "%Y-%m-%d").strftime("%A, %b %d"),
            team1=team1_sorted,
            team2=team2_sorted,
            others=others_sorted,
            others_target=others_target,
        )

    day = request.args.get("day")
    selected_day = day or today_name
    start_time = request.args.get("start_time")
    end_time = request.args.get("end_time")
    start_lat = _parse_float(request.args.get("start_lat"), 40.0)
    start_lon = _parse_float(request.args.get("start_lon"), -74.0)
    speed = _parse_float(request.args.get("speed"), 25.0)
    update_interval = int(_parse_float(request.args.get("update_interval"), 20))

    time_window = _parse_time_window(start_time, end_time)
    target_date = date_param or iso_today
    using_postgres = isinstance(data_source, PostgresDataSource)
    if using_postgres:
        stops = data_source.get_stops(date=target_date, time_window=time_window)
    else:
        stops = data_source.get_stops(day=today_name, time_window=time_window)
    if not stops and day:
        # Only honor explicit day filter when provided in query
        stops = data_source.get_stops(day=selected_day, time_window=time_window)
    reference_time = today_dt
    display_stops = _annotate_stops(stops, reference_time)

    plan: Optional[RoutePlan] = None
    updates: list[dict] = []
    if stops:
        plan = simulate_route(
            stops,
            start_coordinate=Coordinate(latitude=start_lat, longitude=start_lon),
            speed_kmh=speed,
        )
        updates = list(simulate_live_updates(plan, update_interval_seconds=update_interval))[:12]

    # Compute global visited count across all scheduled/registered houses with addresses
    all_stops = data_source.get_stops()

    def is_countable_house(s: CarolsStop) -> bool:
        ref = (getattr(s, "referral", "") or "").strip()
        if ref.endswith("-Newhope") or ref == "Newhope":
            return False
        return bool((getattr(s, "address", "") or "").strip())

    filtered_stops = [s for s in all_stops if is_countable_house(s)]
    visited_total = sum(1 for s in filtered_stops if bool(getattr(s, "address_visited", False)))
    total_houses = len(filtered_stops)
    visited_today = sum(1 for s in stops if bool(getattr(s, "address_visited", False))) if stops else 0
    total_today = len(stops) if stops else 0

    return render_template(
        "index.html",
        stops=display_stops,
        plan=plan,
        updates=updates,
        day_filter=selected_day,
        today_name=today_name,
        iso_today=target_date,
        start_time=start_time,
        end_time=end_time,
        start_lat=start_lat,
        start_lon=start_lon,
        speed=speed,
        update_interval=update_interval,
        visited_total=visited_total,
        total_houses=total_houses,
        visited_today=visited_today,
        total_today=total_today,
    )


@app.route("/tracker/special")
def tracker_special() -> str:
    """Special two-team tracker view for a specific date."""
    date_iso = (request.args.get("date") or _now_local().strftime("%Y-%m-%d")).strip()
    try:
        datetime.strptime(date_iso, "%Y-%m-%d")
    except ValueError:
        date_iso = _now_local().strftime("%Y-%m-%d")

    try:
        stops = data_source.get_stops(date=date_iso)
    except TypeError:
        try:
            d = datetime.strptime(date_iso, "%Y-%m-%d").date()
            day_name = d.strftime("%A")
        except Exception:
            day_name = None
        stops = data_source.get_stops(day=day_name) if day_name else []

    team1: list[CarolsStop] = []
    team2: list[CarolsStop] = []
    others: list[CarolsStop] = []
    for s in stops:
        ref = (s.referral or "").strip().upper()
        if ref == "TEAM1":
            team1.append(s)
        elif ref == "TEAM2":
            team2.append(s)
        else:
            others.append(s)

    team1_sorted = sorted(team1, key=_sort_key)
    team2_sorted = sorted(team2, key=_sort_key)
    others_sorted = sorted(others, key=_sort_key)
    others_target = "team1" if len(team1_sorted) >= len(team2_sorted) else "team2"

    return render_template(
        "tracker_special.html",
        date_iso=date_iso,
        date_display=datetime.strptime(date_iso, "%Y-%m-%d").strftime("%A, %b %d"),
        team1=team1_sorted,
        team2=team2_sorted,
        others=others_sorted,
        others_target=others_target,
    )


@app.route("/api/tracker_status")
def api_tracker_status():
    """Lightweight status for the tracker UI to decide when to move Santa.

    Returns stops in visit order with visited flags and the current (first unvisited) index.
    """
    day = request.args.get("day") or _now_local().strftime("%A")
    date_iso = request.args.get("date") or None
    if date_iso:
        try:
            datetime.strptime(date_iso, "%Y-%m-%d")
        except ValueError:
            date_iso = None
    try:
        stops = data_source.get_stops(date=date_iso) if date_iso else data_source.get_stops(day=day)
    except TypeError:
        stops = data_source.get_stops(day=day)
    ordered = sorted(stops, key=_sort_key)
    visited_flags = [bool(s.address_visited) if s.address_visited is not None else False for s in ordered]
    try:
        current_index = next(i for i, v in enumerate(visited_flags) if not v)
    except StopIteration:
        current_index = None
    return jsonify(
        {
            "day": day,
            "order_ids": [getattr(s, "id", None) for s in ordered],
            "visit_order": [s.visit_order for s in ordered],
            "visited": visited_flags,
            "current_index": current_index,
            "count": len(ordered),
        }
    )


@app.route("/")
def root():
    return redirect(url_for("home"))


@app.route("/home")
def home() -> str:  # pragma: no cover - landing page
    tiles = [
        {
            "title": "Carols Tracker",
            "description": "Follow the live route, Santa animation, and open each address in Google Maps as you move through the list.",
            "url": url_for("tracker"),
            "cta": "View tracker",
        },
        {
            "title": "Registration",
            "description": "Sign up your home for a Christmas Carols visit! Enter your street address, contact number, and prayer requests.",
            "url": url_for("registration"),
            "cta": "Register your home",
        },
        {
            "title": "Weekly Schedule",
            "description": "See every house grouped by date; search by family name to jump directly to the right night.",
            "url": url_for("schedule"),
            "cta": "Open schedule",
        },
        {
            "title": "Songs",
            "description": "Open the Christmas songbook with click-to-expand lyrics for caroling nights.",
            "url": url_for("songs"),
            "cta": "Open songs",
        },
        {
            "title": "Registrations List",
            "description": "Admin view: review submitted Carols registrations, schedule visit dates, and export full CSV backups.",
            "url": url_for("registrations_list"),
            "cta": "View registrations",
        },
        {
            "title": "Add Address",
            "description": "Directly add a family address onto a specific Carols date with prayer request and visit order.",
            "url": url_for("add"),
            "cta": "Add address",
        },
    ]
    return render_template("home.html", tiles=tiles)


def _is_registration_entry(s: CarolsStop) -> bool:
    """Return True if a stop originated from /registration or is not yet assigned a schedule date."""
    ref = (s.referral or "").strip()
    ref_lower = ref.lower()
    if ref.endswith("-Newhope") or ref == "Newhope":
        return True
    if "cic" in ref_lower or "carols" in ref_lower or "registration" in ref_lower:
        return True
    # Any stop without an assigned date (and with an id in Postgres) is an unscheduled registration
    if s.id is not None and not s.date:
        return True
    return False


@app.route("/registrations")
def registrations_list() -> str:
    """List submitted registrations and show whether each address has been scheduled."""
    stops = data_source.get_stops()

    def is_newhope_referral(s: CarolsStop) -> bool:
        ref = (s.referral or "").strip()
        return ref.endswith("-Newhope") or ref == "Newhope"

    def is_cic_referral(s: CarolsStop) -> bool:
        return _is_registration_entry(s) and not is_newhope_referral(s)

    regs = [s for s in stops if _is_registration_entry(s)]
    newhope_regs = [s for s in regs if is_newhope_referral(s)]
    cic_regs = [s for s in regs if is_cic_referral(s)]
    unscheduled_count = sum(1 for s in regs if not s.date)
    regs = sorted(regs, key=lambda s: (0 if not s.date else 1, (s.name or "").lower()))
    using_postgres = isinstance(data_source, PostgresDataSource)
    return render_template(
        "registrations_list.html",
        registrations=regs,
        total_count=len(regs),
        newhope_count=len(newhope_regs),
        cic_count=len(cic_regs),
        unscheduled_count=unscheduled_count,
        using_postgres=using_postgres,
    )


@app.route("/registrations/export")
def registrations_export():
    """Export registrations to CSV."""
    stops = data_source.get_stops()
    regs = [s for s in stops if _is_registration_entry(s)]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Id",
            "Name",
            "Family Members",
            "Email",
            "Address",
            "Zipcode",
            "Phone",
            "Prayer",
            "Scheduled Date",
            "Visited",
            "Invited by",
        ]
    )
    for r in regs:
        writer.writerow(
            [
                r.id or "",
                r.name or "",
                ", ".join(r.family_members) if r.family_members else "",
                getattr(r, "email_id", None) or "",
                r.address or "",
                r.zipcode or "",
                r.contact_number or "",
                r.prayer_request or "",
                r.date or "",
                "Yes" if r.address_visited else "No",
                r.referral or "",
            ]
        )
    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=registrations.csv"
    return resp


@app.route("/api/backup/export_csv")
def backup_export_csv():
    """Export ALL rows and columns from carols_schedule so zero data is ever lost."""
    stops = data_source.get_stops()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Id",
            "Name",
            "Family Members",
            "Contact Number",
            "Email Id",
            "Prayer Request",
            "Address",
            "Zipcode",
            "Day",
            "Date",
            "Scheduled Time",
            "Visit Order",
            "Address Visited",
            "Referral",
        ]
    )
    for s in stops:
        writer.writerow(
            [
                s.id if s.id is not None else "",
                s.name or "",
                ", ".join(s.family_members) if s.family_members else "",
                s.contact_number or "",
                s.email_id or "",
                s.prayer_request or "",
                s.address or "",
                s.zipcode or "",
                s.day or "",
                s.date or "",
                s.scheduled_time or "",
                s.visit_order if s.visit_order is not None else "",
                "true" if s.address_visited else "false",
                s.referral or "church",
            ]
        )
    stamp = _now_local().strftime("%Y%m%d")
    resp = make_response(output.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = f"attachment; filename=carols_full_backup_{stamp}.csv"
    return resp


@app.route("/api/backup/export_json")
def backup_export_json():
    """Export ALL rows and columns from carols_schedule as JSON."""
    stops = data_source.get_stops()
    payload = [
        {
            "id": s.id,
            "name": s.name,
            "family_members": s.family_members,
            "contact_number": s.contact_number,
            "email": s.email_id,
            "prayer_request": s.prayer_request,
            "address": s.address,
            "zipcode": s.zipcode,
            "day": s.day,
            "date": s.date,
            "scheduled_time": s.scheduled_time,
            "visit_order": s.visit_order,
            "address_visited": bool(s.address_visited),
            "referral": s.referral,
        }
        for s in stops
    ]
    return jsonify({"ok": True, "count": len(payload), "stops": payload})


def _season_year() -> int:
    raw = (os.getenv("CAROLS_YEAR") or "").strip()
    if raw.isdigit():
        return int(raw)
    return _now_local().year


def _build_planned_dates(extra_iso_dates: Optional[Iterable[str]] = None) -> list[date]:
    """Build Carols schedule dates for the current season year plus any dates stored in DB."""
    year = _season_year()
    allowed = {1, 2, 4, 5}  # Tuesday(1), Wednesday(2), Friday(4), Saturday(5)
    result_set: set[date] = set()

    if year == 2025:
        start = date(2025, 12, 3)
        end = date(2025, 12, 22)
        current = start
        while current <= end:
            if current.weekday() in allowed:
                result_set.add(current)
            current += timedelta(days=1)
        result_set.add(date(2025, 12, 18))
        result_set.discard(date(2025, 12, 19))
        result_set.discard(date(2025, 12, 20))
        result_set.add(date(2025, 12, 21))
        result_set.add(date(2025, 12, 22))
    else:
        start = date(year, 12, 1)
        end = date(year, 12, 23)
        current = start
        while current <= end:
            if current.weekday() in allowed:
                result_set.add(current)
            current += timedelta(days=1)

    if extra_iso_dates:
        for iso_val in extra_iso_dates:
            if not iso_val:
                continue
            try:
                parsed = datetime.strptime(iso_val.strip(), "%Y-%m-%d").date()
                result_set.add(parsed)
            except ValueError:
                continue

    return sorted(result_set)


@app.route("/schedule")
def schedule() -> str:  # pragma: no cover - UI entrypoint
    all_stops = data_source.get_stops()
    db_dates = [s.date for s in all_stops if s.date]
    dates = _build_planned_dates(extra_iso_dates=db_dates)
    reference_time = _now_local()
    season_year = _season_year()

    # Historical 2025 city & Santa volunteer badges (preserved if viewing 2025 dates)
    city_labels = {
        "2025-12-03": "Buffalo Grove",
        "2025-12-05": "Mount Prospect",
        "2025-12-06": "Arlington Heights",
        "2025-12-09": "Elgin",
        "2025-12-10": "Schaumburg",
        "2025-12-12": "Hawthorn Woods",
        "2025-12-13": "Buffalo Grove",
        "2025-12-16": "Palatine",
        "2025-12-17": "Gurnee",
        "2025-12-18": "Hoffman Estates",
        "2025-12-21": "Little Sisters of Hope",
        "2025-12-22": "CIC",
    }
    santa_map = {
        "2025-12-03": "DPS",
        "2025-12-05": "Siva",
        "2025-12-06": "Sindu",
        "2025-12-09": "Siva",
        "2025-12-10": "Pandi",
        "2025-12-12": "Siva, Sree",
        "2025-12-13": "Pandi, Benny",
        "2025-12-16": "Siva",
        "2025-12-17": "Siva, Sree",
        "2025-12-18": "Pandi, DPS",
    }
    schedule_blocks = []
    for planned_date in dates:
        day_name = planned_date.strftime("%A")
        iso_date = planned_date.strftime("%Y-%m-%d")
        try:
            stops = data_source.get_stops(date=iso_date)
        except TypeError:
            stops = data_source.get_stops(day=day_name)
        annotated = _annotate_stops(stops, reference_time)
        schedule_blocks.append(
            {
                "id": iso_date,
                "display": planned_date.strftime("%a, %b %d"),
                "day": day_name,
                "date": iso_date,
                "city": city_labels.get(iso_date),
                "santa": santa_map.get(iso_date),
                "count": len(annotated),
                "stops": annotated,
            }
        )
    unscheduled_count = sum(1 for s in all_stops if not s.date and (s.address or "").strip())
    using_postgres = isinstance(data_source, PostgresDataSource)
    return render_template(
        "schedule.html",
        schedule_blocks=schedule_blocks,
        using_postgres=using_postgres,
        season_year=season_year,
        unscheduled_count=unscheduled_count,
    )


@app.route("/songs")
def songs() -> str:
    """Songbook page with click-to-expand UI."""
    return render_template("songs.html")


@app.route("/api/stops/<int:item_id>/visited", methods=["POST"])  # pragma: no cover - AJAX toggle endpoint
def api_toggle_visited(item_id: int):
    """Toggle or set the visited flag for a stop by numeric id."""
    using_postgres = isinstance(data_source, PostgresDataSource)
    if not using_postgres:
        return jsonify({"ok": False, "error": "Editing requires DATABASE_URL or PG_DSN."}), 400
    try:
        payload = request.get_json(force=True) or {}
        raw = str(payload.get("visited")).strip().lower()
        visited = True if raw in ("true", "t", "1", "yes", "on") else False
        data_source.update_stop_by_id(item_id, {"address_visited": visited})
        current = data_source.get_stop_by_id(item_id)
        return jsonify(
            {
                "ok": True,
                "id": item_id,
                "visited": bool(current.address_visited)
                if current and current.address_visited is not None
                else visited,
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Failed to update: {exc}"}), 500


@app.route("/api/stops/<int:item_id>/delete", methods=["POST"])  # pragma: no cover - AJAX delete endpoint
def api_delete_stop(item_id: int):
    """Delete a stop by its id."""
    using_postgres = isinstance(data_source, PostgresDataSource)
    if not using_postgres:
        return jsonify({"ok": False, "error": "Deleting requires DATABASE_URL or PG_DSN."}), 400
    try:
        data_source.delete_stop_by_id(item_id)
        return jsonify({"ok": True, "id": item_id})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Failed to delete: {exc}"}), 500


@app.route("/api/stops/<int:item_id>/email", methods=["POST"])  # pragma: no cover - AJAX update email
def api_update_email(item_id: int):
    """Update the email field for a stop by numeric id."""
    using_postgres = isinstance(data_source, PostgresDataSource)
    if not using_postgres:
        return jsonify({"ok": False, "error": "Editing requires DATABASE_URL or PG_DSN."}), 400
    try:
        payload = request.get_json(force=True) or {}
        email_val = (payload.get("email") or "").strip() or None
        data_source.update_stop_by_id(item_id, {"email": email_val})
        current = data_source.get_stop_by_id(item_id)
        return jsonify(
            {
                "ok": True,
                "id": item_id,
                "email": getattr(current, "email_id", None),
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Failed to update email: {exc}"}), 500


@app.route("/api/schedule/reorder", methods=["POST"])  # pragma: no cover - AJAX reorder endpoint
def api_reorder_schedule():
    """Rewrite visit_order for a given date/day based on client-provided id sequence."""
    using_postgres = isinstance(data_source, PostgresDataSource)
    if not using_postgres:
        return jsonify({"ok": False, "error": "Editing requires DATABASE_URL or PG_DSN."}), 400
    try:
        payload = request.get_json(force=True) or {}
        ids = payload.get("ids") or []
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            return jsonify({"ok": False, "error": "ids must be a list of integers"}), 400
        date_val = payload.get("date") or None
        day_val = payload.get("day") or None
        data_source.reorder_stops(ids=ids, date=date_val, day=day_val)
        return jsonify({"ok": True, "count": len(ids)})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Failed to reorder: {exc}"}), 500


@app.route("/api/schedule/quick_add", methods=["POST"])  # pragma: no cover - AJAX quick add endpoint
def api_quick_add():
    """Quickly add a new stop with just Name and Address for a given date."""
    using_postgres = isinstance(data_source, PostgresDataSource)
    if not using_postgres:
        return jsonify({"ok": False, "error": "Adding requires DATABASE_URL or PG_DSN."}), 400
    try:
        payload = request.get_json(force=True) or {}
        date_val = (payload.get("date") or "").strip()
        name = (payload.get("name") or "").strip()
        address = (payload.get("address") or "").strip()
        referral = (payload.get("referral") or "church").strip() or "church"
        if not date_val or not name or not address:
            return jsonify({"ok": False, "error": "date, name and address are required"}), 400
        try:
            d = datetime.strptime(date_val, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"ok": False, "error": "Invalid date format (use YYYY-MM-DD)"}), 400
        day_name = d.strftime("%A")
        new_id = data_source.add_stop(
            name=name,
            address=address,
            day=day_name,
            date=date_val,
            scheduled_time=None,
            visit_order=None,
            address_visited=False,
            referral=referral,
        )
        return jsonify(
            {
                "ok": True,
                "id": new_id,
                "name": name,
                "address": address,
                "date": date_val,
                "day": day_name,
                "referral": referral,
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Failed to add: {exc}"}), 500


@app.route("/registration", methods=["GET", "POST"])
def registration() -> str:
    """Public Carols home visit registration form.

    Collects family name, full home address (street, city, state, zipcode), phone number,
    optional email, family members, prayer request, and invited-by referral.
    Stores record in Postgres with date=None until scheduled by an organizer.
    """
    using_postgres = isinstance(data_source, PostgresDataSource)
    message: Optional[str] = None
    error: Optional[str] = None
    if request.method == "POST":
        if not using_postgres:
            error = "Registration requires DATABASE_URL or PG_DSN to be configured."
        else:
            first = (request.form.get("first_name") or "").strip()
            last = (request.form.get("last_name") or "").strip()
            street = (request.form.get("address") or "").strip()
            city = (request.form.get("city") or "").strip() or None
            state = (request.form.get("state") or "IL").strip() or "IL"
            zipcode = (request.form.get("zipcode") or "").strip() or None
            phone = (request.form.get("phone") or "").strip() or None
            email = (request.form.get("email") or "").strip() or None
            family_raw = (request.form.get("family_members") or "").strip()
            prayer_request = (request.form.get("prayer_request") or "").strip() or None
            raw_ref = (request.form.get("referral") or "").strip()

            if not first:
                error = "First name is required."
            elif not street or not city:
                error = "Street address and City are required so our Carols team can visit your home."
            elif not phone:
                error = "Phone number is required so we can notify you when the Carols team is on the way."
            else:
                try:
                    name = f"{first} {last}".strip()
                    full_address = _compose_address(street, city, state)
                    family_members = [p.strip() for p in family_raw.split(",") if p.strip()] or None
                    if raw_ref:
                        referral_val = raw_ref if "cic" in raw_ref.lower() else f"{raw_ref}-CIC"
                    else:
                        referral_val = "Carols-CIC"
                    data_source.add_stop(
                        name=name,
                        address=full_address,
                        family_members=family_members,
                        contact_number=phone,
                        email=email,
                        prayer_request=prayer_request,
                        zipcode=zipcode,
                        day=None,
                        date=None,
                        scheduled_time=None,
                        visit_order=None,
                        address_visited=False,
                        referral=referral_val,
                    )
                    message = (
                        "Thank you! Your home address has been registered for a Christmas Carols visit. "
                        "Our team will reach out once your date and time window are scheduled!"
                    )
                except Exception as exc:
                    error = f"Failed to submit registration: {exc}"
    return render_template(
        "registration.html",
        using_postgres=using_postgres,
        message=message,
        error=error,
        default_state="IL",
    )


@app.route("/add", methods=["GET", "POST"])
def add() -> str:
    """Add a new address to the schedule (DB insert)."""
    using_postgres = isinstance(data_source, PostgresDataSource)
    message: Optional[str] = None
    error: Optional[str] = None
    if request.method == "POST":
        if not using_postgres:
            error = "Database inserts are only available when DATABASE_URL or PG_DSN is configured."
        else:
            name = (request.form.get("name") or "").strip()
            street_address = (request.form.get("address") or "").strip()
            city = (request.form.get("city") or "").strip() or None
            state = (request.form.get("state") or "IL").strip() or "IL"
            family_members_raw = (request.form.get("family_members") or "").strip()
            contact_number = (request.form.get("contact_number") or "").strip() or None
            email = (request.form.get("email") or "").strip() or None
            prayer_request = (request.form.get("prayer_request") or "").strip() or None
            zipcode = (request.form.get("zipcode") or "").strip() or None
            date_str = (request.form.get("date") or "").strip()
            _st_raw = (request.form.get("scheduled_time") or "").strip()
            scheduled_time = _st_raw if _parse_hhmm(_st_raw) else None
            visit_order_raw = (request.form.get("visit_order") or "").strip()
            address_visited_raw = (request.form.get("address_visited") or "").strip().lower()
            address_visited = True if address_visited_raw in ("true", "t", "1", "yes", "on") else False

            if not name or not street_address:
                error = "Full Name and Address are required."
            elif not date_str:
                error = "Please choose a date."
            else:
                family_members = [p.strip() for p in family_members_raw.split(",") if p.strip()] or None
                try:
                    selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    day = selected_date.strftime("%A")
                    address = _compose_address(street_address, city, state or "IL")
                    referral = (request.form.get("referral") or "church").strip() or "church"
                    visit_order = int(visit_order_raw) if visit_order_raw.isdigit() else None
                    data_source.add_stop(
                        name=name,
                        address=address,
                        family_members=family_members,
                        contact_number=contact_number,
                        email=email,
                        prayer_request=prayer_request,
                        zipcode=zipcode,
                        day=day,
                        date=date_str,
                        scheduled_time=scheduled_time,
                        visit_order=visit_order,
                        address_visited=address_visited,
                        referral=referral,
                    )
                    message = "Address added successfully."
                except Exception as exc:  # pragma: no cover
                    error = f"Failed to add address: {exc}"
    return render_template("add.html", using_postgres=using_postgres, message=message, error=error, default_state="IL")


@app.route("/edit", methods=["GET", "POST"])
def edit() -> str:
    """Edit an existing address entry."""
    using_postgres = isinstance(data_source, PostgresDataSource)
    if not using_postgres:
        return render_template("edit.html", using_postgres=False, error="Editing requires DATABASE_URL or PG_DSN.")

    name = (request.args.get("name") or request.form.get("name_key") or "").strip()
    address = (request.args.get("address") or request.form.get("address_key") or "").strip()
    day = (request.args.get("day") or request.form.get("day_key") or "").strip() or None
    scheduled_time = (request.args.get("scheduled_time") or request.form.get("time_key") or "").strip() or None

    current = data_source.get_stop(name=name, address=address, day=day, scheduled_time=scheduled_time)
    if request.method == "POST":
        updates = {
            "name": (request.form.get("name") or "").strip() or (current.name if current else None),
            "address": (request.form.get("address") or "").strip() or (current.address if current else None),
            "family_members": [p.strip() for p in (request.form.get("family_members") or "").split(",") if p.strip()],
            "contact_number": (request.form.get("contact_number") or "").strip() or None,
            "email": (request.form.get("email") or "").strip() or None,
            "prayer_request": (request.form.get("prayer_request") or "").strip() or None,
            "zipcode": (request.form.get("zipcode") or "").strip() or None,
            "day": (request.form.get("day") or "").strip() or None,
            "scheduled_time": (request.form.get("scheduled_time") or "").strip() or None,
            "visit_order": (
                int(request.form.get("visit_order"))
                if (request.form.get("visit_order") or "").isdigit()
                else None
            ),
        }
        if updates["scheduled_time"] and not _parse_hhmm(str(updates["scheduled_time"])):
            updates["scheduled_time"] = None
        try:
            data_source.update_stop(
                name=name,
                address=address,
                day=day,
                scheduled_time=scheduled_time,
                updates=updates,
            )
            current = data_source.get_stop(
                name=updates["name"] or name,
                address=updates["address"] or address,
                day=updates["day"] or day,
                scheduled_time=updates["scheduled_time"] or scheduled_time,
            )
            message = "Updated successfully."
            return render_template("edit.html", using_postgres=True, entry=current, message=message)
        except Exception as exc:  # pragma: no cover
            return render_template("edit.html", using_postgres=True, entry=current, error=f"Failed to update: {exc}")

    return render_template("edit.html", using_postgres=True, entry=current)


@app.route("/edit/<int:item_id>", methods=["GET", "POST"])
def edit_by_id(item_id: int) -> str:
    using_postgres = isinstance(data_source, PostgresDataSource)
    if not using_postgres:
        return render_template("edit.html", using_postgres=False, error="Editing requires DATABASE_URL or PG_DSN.")

    current = data_source.get_stop_by_id(item_id)
    street_addr = ""
    city_prefill: Optional[str] = None
    state_prefill: Optional[str] = None
    if current:
        street_addr, city_prefill, state_prefill = _split_address_components(current.address)
    if request.method == "POST":
        updates = {
            "name": (request.form.get("name") or "").strip() or (current.name if current else None),
            "family_members": [p.strip() for p in (request.form.get("family_members") or "").split(",") if p.strip()],
            "contact_number": (request.form.get("contact_number") or "").strip() or None,
            "email": (request.form.get("email") or "").strip() or None,
            "prayer_request": (request.form.get("prayer_request") or "").strip() or None,
            "zipcode": (request.form.get("zipcode") or "").strip() or None,
            "date": (request.form.get("date") or "").strip() or None,
            "scheduled_time": (request.form.get("scheduled_time") or "").strip() or None,
            "visit_order": (
                int(request.form.get("visit_order"))
                if (request.form.get("visit_order") or "").isdigit()
                else None
            ),
            "referral": (request.form.get("referral") or "church").strip() or "church",
        }
        if updates.get("date"):
            try:
                d = datetime.strptime(str(updates["date"]), "%Y-%m-%d").date()
                updates["day"] = d.strftime("%A")
            except ValueError:
                pass
        if updates["scheduled_time"] and not _parse_hhmm(str(updates["scheduled_time"])):
            updates["scheduled_time"] = None
        visited_raw = (request.form.get("address_visited") or "").strip().lower()
        updates["address_visited"] = True if visited_raw in ("true", "t", "1", "yes", "on") else False
        street_in = (request.form.get("address") or "").strip()
        city_in = (request.form.get("city") or "").strip() or None
        state_in = (request.form.get("state") or (state_prefill or "IL")).strip() or "IL"
        if street_in or city_in or state_in:
            updates["address"] = _compose_address(
                street_in or street_addr,
                city_in or city_prefill,
                state_in or state_prefill or "IL",
            )
        try:
            data_source.update_stop_by_id(item_id, updates)
            current = data_source.get_stop_by_id(item_id)
            if current:
                street_addr, city_prefill, state_prefill = _split_address_components(current.address)
            message = "Updated successfully."
            return render_template(
                "edit.html",
                using_postgres=True,
                entry=current,
                message=message,
                street_address=street_addr,
                city=city_prefill or "",
                state=state_prefill or "IL",
            )
        except Exception as exc:  # pragma: no cover
            return render_template(
                "edit.html",
                using_postgres=True,
                entry=current,
                error=f"Failed to update: {exc}",
                street_address=street_addr,
                city=city_prefill or "",
                state=state_prefill or "IL",
            )
    return render_template(
        "edit.html",
        using_postgres=True,
        entry=current,
        street_address=street_addr,
        city=city_prefill or "",
        state=state_prefill or "IL",
    )
