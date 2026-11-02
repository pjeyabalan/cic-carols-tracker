from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Iterable, Optional

from tracker.data_sources import CsvDataSource, PostgresDataSource, resolve_pg_dsn
from tracker.gps import Coordinate, RoutePlan, simulate_live_updates, simulate_route


def _print_stops_summary(stops: Iterable) -> None:
    for stop in stops:
        print("-" * 40)
        print(f"Name: {stop.name}")
        if stop.family_members:
            print(f"Family: {', '.join(stop.family_members)}")
        if stop.prayer_request:
            print(f"Prayer: {stop.prayer_request}")
        print(f"Address: {stop.address}")
        if stop.email_id:
            print(f"Email: {stop.email_id}")
        print(f"Scheduled time: {stop.scheduled_time or 'TBD'}")
        print(f"Zipcode: {stop.zipcode}")


def _print_route_plan(plan: RoutePlan) -> None:
    print("\n--- Simulated GPS Route ---")
    prev = plan.start_coordinate
    for point in plan.points:
        dist_km = point.distance_from_previous_km
        eta = point.eta.strftime("%H:%M")
        print(
            f"{point.stop.name} ({point.stop.address}) - {dist_km:.2f} km from last stop, ETA {eta}"
        )
        prev = point.coordinate


def _load_data_source(args: argparse.Namespace):
    if args.source == "postgres":
        if not args.pg_dsn:
            raise ValueError("Postgres source requires --pg-dsn")
        return PostgresDataSource(args.pg_dsn)
    return CsvDataSource(Path(args.csv_file))


def _parse_time_window(start: Optional[str], end: Optional[str]) -> Optional[tuple[str, str]]:
    if start and end:
        return start, end
    if start or end:
        raise ValueError("Both --start-time and --end-time must be provided together")
    return None


def main(argv: Optional[Iterable[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Carols tracker runner")
    default_source = "postgres" if resolve_pg_dsn() else "csv"
    parser.add_argument("--source", choices=["csv", "postgres"], default=default_source)
    parser.add_argument("--csv-file", default=Path("data/carols_schedule.csv"))
    parser.add_argument("--pg-dsn", help="Postgres DSN for carols_schedule table")
    parser.add_argument("--day", help="Optional day field to filter")
    parser.add_argument("--start-time", help="Filter visits starting at or after this HH:MM")
    parser.add_argument("--end-time", help="Filter visits ending at or before this HH:MM")
    parser.add_argument("--simulate", action="store_true", help="Simulate GPS route for today")
    parser.add_argument("--start-lat", type=float, default=40.0)
    parser.add_argument("--start-lon", type=float, default=-74.0)
    parser.add_argument("--speed", type=float, default=25.0, help="Estimated speed in km/h")
    parser.add_argument("--update-interval", type=int, default=20, help="Seconds between simulated GPS updates")
    parser.add_argument("--max-updates", type=int, default=4, help="Max live GPS updates to print")
    args = parser.parse_args(argv)

    time_window = _parse_time_window(args.start_time, args.end_time)
    if args.source == "postgres":
        args.pg_dsn = resolve_pg_dsn(args.pg_dsn)
    source = _load_data_source(args)
    stops = source.get_stops(day=args.day, time_window=time_window)
    if not stops:
        print("No stops found for the current filters.")
        return
    print("Upcoming carols stops")
    _print_stops_summary(stops)

    if args.simulate:
        plan = simulate_route(
            stops,
            start_coordinate=Coordinate(latitude=args.start_lat, longitude=args.start_lon),
            speed_kmh=args.speed,
        )
        _print_route_plan(plan)
        updates = list(simulate_live_updates(plan, update_interval_seconds=args.update_interval))
        print("\n--- Live GPS samples ---")
        for update in updates[: args.max_updates]:
            eta_minutes = int(update["eta_to_destination"].total_seconds() // 60)
            print(
                f"{update['timestamp'].strftime('%H:%M:%S')} -> {update['next_stop']} @ "
                f"{update['latitude']:.5f},{update['longitude']:.5f} (ETA in {eta_minutes} min)"
            )


if __name__ == "__main__":
    main()