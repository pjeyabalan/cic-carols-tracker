from datetime import datetime

from tracker.data_sources import CarolsStop
from tracker.gps import (
    Coordinate,
    cluster_two_teams,
    forecast_dynamic_etas,
    optimize_route_2opt,
    simulate_live_updates,
    simulate_route,
)


def _make_stop(
    name: str,
    zipcode: str = "60089",
    *,
    stop_id: int | None = None,
    lat: float | None = None,
    lon: float | None = None,
    visited: bool = False,
) -> CarolsStop:
    return CarolsStop(
        id=stop_id,
        name=name,
        family_members=["Test"],
        contact_number="",
        email_id=None,
        prayer_request="",
        address=f"123 {name} St",
        zipcode=zipcode,
        address_visited=visited,
        latitude=lat,
        longitude=lon,
    )


def test_route_plan_has_points() -> None:
    stops = [_make_stop("Home A", "01101"), _make_stop("Home B", "01102")]
    plan = simulate_route(stops, start_coordinate=Coordinate(40.0, -74.0), speed_kmh=30)
    assert len(plan.points) == len(stops)


def test_live_updates_cover_stops() -> None:
    stops = [_make_stop("Home A", "01101")]
    plan = simulate_route(stops, start_coordinate=Coordinate(40.0, -74.0), speed_kmh=30)
    updates = list(simulate_live_updates(plan, update_interval_seconds=5))
    assert updates
    assert updates[-1]["next_stop"] == stops[-1].name


def test_optimize_route_2opt_uncrosses_zigzag_path() -> None:
    # Deliberately scrambled north-south-north-south zig-zag order
    stops = [
        _make_stop("North-1", stop_id=1, lat=42.20, lon=-87.96),
        _make_stop("South-1", stop_id=2, lat=42.06, lon=-87.96),
        _make_stop("North-2", stop_id=3, lat=42.19, lon=-87.95),
        _make_stop("South-2", stop_id=4, lat=42.07, lon=-87.95),
        _make_stop("Mid", stop_id=5, lat=42.13, lon=-87.955),
    ]
    optimized, baseline_miles, optimized_miles = optimize_route_2opt(stops)
    assert len(optimized) == 5
    assert optimized_miles < baseline_miles
    assert (baseline_miles - optimized_miles) > 10.0


def test_optimize_route_2opt_locks_pinned_final_home() -> None:
    stops = [
        _make_stop("House A", stop_id=10, lat=42.18, lon=-87.96),
        _make_stop("Dinner Host", stop_id=20, lat=42.14, lon=-87.95),
        _make_stop("House B", stop_id=30, lat=42.06, lon=-87.96),
        _make_stop("House C", stop_id=40, lat=42.17, lon=-87.96),
    ]
    optimized, _, _ = optimize_route_2opt(stops, lock_last_id=20)
    assert optimized[-1].id == 20
    assert optimized[-1].name == "Dinner Host"


def test_cluster_two_teams_balanced_and_geographically_separated() -> None:
    # 4 homes in Hawthorn Woods (west) + 4 homes in Vernon Hills (east)
    stops = [
        _make_stop("HW-1", stop_id=1, lat=42.220, lon=-88.070),
        _make_stop("VH-1", stop_id=2, lat=42.225, lon=-87.950),
        _make_stop("HW-2", stop_id=3, lat=42.228, lon=-88.065),
        _make_stop("VH-2", stop_id=4, lat=42.232, lon=-87.945),
        _make_stop("HW-3", stop_id=5, lat=42.215, lon=-88.075),
        _make_stop("VH-3", stop_id=6, lat=42.218, lon=-87.955),
        _make_stop("HW-4", stop_id=7, lat=42.222, lon=-88.068),
        _make_stop("VH-4", stop_id=8, lat=42.229, lon=-87.948),
    ]
    team1, team2 = cluster_two_teams(stops)
    assert len(team1) == 4
    assert len(team2) == 4
    t1_prefixes = {s.name.split("-")[0] for s in team1}
    t2_prefixes = {s.name.split("-")[0] for s in team2}
    assert len(t1_prefixes) == 1
    assert len(t2_prefixes) == 1
    assert t1_prefixes != t2_prefixes


def test_forecast_dynamic_etas_generates_live_arrival_estimates() -> None:
    stops = [
        _make_stop("Visited Home", stop_id=1, lat=42.16, lon=-87.96, visited=True),
        _make_stop("Current Home", stop_id=2, lat=42.15, lon=-87.96, visited=False),
        _make_stop("Next Home", stop_id=3, lat=42.12, lon=-87.96, visited=False),
    ]
    ref = datetime(2026, 12, 1, 19, 0, 0)
    etas = forecast_dynamic_etas(stops, ref)
    assert 0 not in etas
    assert 1 in etas and etas[1].startswith("~")
    assert 2 in etas and etas[2].startswith("~")
