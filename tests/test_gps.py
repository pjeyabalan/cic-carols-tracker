from tracker.data_sources import CarolsStop
from tracker.gps import Coordinate, simulate_live_updates, simulate_route


def _make_stop(name: str, zipcode: str) -> CarolsStop:
    return CarolsStop(
        name=name,
        family_members=["Test"],
        contact_number="",
        email_id=None,
        prayer_request="",
        address="123 Test St",
        zipcode=zipcode,
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