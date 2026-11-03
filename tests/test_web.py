from unittest.mock import MagicMock

import tracker.web as web_mod
from tracker.data_sources import (
    CarolsStop,
    PostgresDataSource,
    normalize_address_key,
    resolve_pg_dsn,
)
from tracker.events import TrackerEventBus
from tracker.web import app


def test_homepage_loads() -> None:
    client = app.test_client()
    response = client.get("/home")
    assert response.status_code == 200
    assert b"Carols Tracker" in response.data


def test_house_cards_render() -> None:
    client = app.test_client()
    response = client.get("/tracker")
    assert b"house-card" in response.data
    assert b"google.com/maps/search" in response.data or b"Open in Maps" in response.data
    assert response.data.count(b"house-card") >= 5


def test_registration_form_has_address_fields() -> None:
    client = app.test_client()
    response = client.get("/registration")
    assert response.status_code == 200
    assert b'name="address"' in response.data
    assert b'name="city"' in response.data
    assert b'name="state"' in response.data
    assert b'name="zipcode"' in response.data
    assert b'name="phone"' in response.data
    assert b'name="prayer_request"' in response.data


def test_registration_post_persists_full_address(monkeypatch) -> None:
    fake_pg = MagicMock(spec=PostgresDataSource)
    fake_pg.add_stop.return_value = 42
    fake_pg.get_stops.return_value = []
    monkeypatch.setattr(web_mod, "data_source", fake_pg)

    client = app.test_client()
    response = client.post(
        "/registration",
        data={
            "first_name": "Daniel",
            "last_name": "Rajan",
            "address": "742 Evergreen Terrace",
            "city": "Schaumburg",
            "state": "IL",
            "zipcode": "60193",
            "phone": "(847) 555-1234",
            "email": "daniel@example.com",
            "family_members": "Daniel, Priya, Joshua",
            "prayer_request": "Health and blessings for 2027",
            "referral": "Benny",
        },
    )
    assert response.status_code == 200
    assert b"added to the Carols Schedule" in response.data
    assert b"Schaumburg" in response.data
    fake_pg.add_stop.assert_called_once_with(
        name="Daniel Rajan",
        address="742 Evergreen Terrace, Schaumburg, IL",
        family_members=["Daniel", "Priya", "Joshua"],
        contact_number="(847) 555-1234",
        email="daniel@example.com",
        prayer_request="Health and blessings for 2027",
        zipcode="60193",
        day="Tuesday",
        date="2026-12-08",
        scheduled_time=None,
        visit_order=None,
        address_visited=False,
        referral="Benny-CIC",
    )


def test_registration_unknown_city_matches_nearest_area_by_proximity(monkeypatch) -> None:
    fake_pg = MagicMock(spec=PostgresDataSource)
    fake_pg.add_stop.return_value = 99
    fake_pg.get_stops.return_value = []
    monkeypatch.setattr(web_mod, "data_source", fake_pg)
    # Simulate geocoded coordinates for Third Lake, IL (~5.6 mi from Gurnee)
    monkeypatch.setattr(web_mod, "_geocode_us_address", lambda **kw: (42.3575, -87.9881))

    client = app.test_client()
    response = client.post(
        "/registration",
        data={
            "first_name": "Grace",
            "last_name": "Mathew",
            "address": "100 Lakeview Dr",
            "city": "Third Lake",
            "state": "IL",
            "zipcode": "60099",
            "phone": "(847) 555-9999",
        },
    )
    # Note: 60099 is in exact zip map, so pass an unlisted zip to exercise geocoding layer
    fake_pg.add_stop.reset_mock()
    response = client.post(
        "/registration",
        data={
            "first_name": "Grace",
            "last_name": "Mathew",
            "address": "100 Lakeview Dr",
            "city": "Third Lake",
            "state": "IL",
            "zipcode": "60000",
            "phone": "(847) 555-9999",
        },
    )
    assert response.status_code == 200
    assert b"Based on proximity" in response.data
    assert b"Gurnee" in response.data
    fake_pg.add_stop.assert_called_once()
    kwargs = fake_pg.add_stop.call_args.kwargs
    assert kwargs["date"] == "2026-12-15"
    assert kwargs["day"] == "Tuesday"


def test_schedule_displays_2026_areas_and_house_counts(monkeypatch) -> None:
    monkeypatch.setenv("CAROLS_YEAR", "2026")
    fake_pg = MagicMock(spec=PostgresDataSource)
    fake_pg.get_stops.side_effect = lambda date=None, **kw: (
        [
            CarolsStop(
                id=1,
                name="Daniel Rajan",
                family_members=[],
                address="742 Evergreen Terrace, Schaumburg, IL",
                zipcode="60193",
                date="2026-12-08",
                day="Tuesday",
                referral="Carols-CIC",
            )
        ]
        if date in (None, "2026-12-08")
        else []
    )
    monkeypatch.setattr(web_mod, "data_source", fake_pg)

    client = app.test_client()
    resp = client.get("/schedule")
    assert resp.status_code == 200
    for area in [
        b"Buffalo Grove",
        b"Mount Prospect",
        b"Arlington Heights",
        b"Elgin",
        b"Schaumburg",
        b"Hawthorn Woods / Vernon Hills",
        b"Palatine",
        b"Gurnee",
        b"Hoffman Estates",
    ]:
        assert area in resp.data
    assert b"1 house" in resp.data
    assert b"2026-12-01" in resp.data
    assert b"2026-12-16" in resp.data
    assert b"2026-12-17" not in resp.data


def test_registrations_list_and_full_backup_export(monkeypatch) -> None:
    fake_pg = MagicMock(spec=PostgresDataSource)
    fake_pg.get_stops.return_value = [
        CarolsStop(
            id=101,
            name="Daniel Rajan",
            family_members=["Daniel", "Priya"],
            address="742 Evergreen Terrace, Buffalo Grove, IL",
            zipcode="60089",
            contact_number="(847) 555-1234",
            email_id="daniel@example.com",
            prayer_request="Family peace",
            date=None,
            address_visited=False,
            referral="Carols-CIC",
        )
    ]
    monkeypatch.setattr(web_mod, "data_source", fake_pg)

    client = app.test_client()
    reg_resp = client.get("/registrations")
    assert reg_resp.status_code == 200
    assert b"742 Evergreen Terrace, Buffalo Grove, IL" in reg_resp.data
    assert b"Assign Date / Edit" in reg_resp.data

    csv_resp = client.get("/api/backup/export_csv")
    assert csv_resp.status_code == 200
    assert b"742 Evergreen Terrace, Buffalo Grove, IL" in csv_resp.data

    json_resp = client.get("/api/backup/export_json")
    assert json_resp.status_code == 200
    assert json_resp.get_json()["count"] == 1


def test_resolve_pg_dsn_render_url(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://carols_user:secret@dpg-xyz.oregon-postgres.render.com/carols")
    dsn = resolve_pg_dsn()
    assert dsn == "postgresql://carols_user:secret@dpg-xyz.oregon-postgres.render.com/carols?sslmode=require"


def test_normalize_address_key_canonicalizes_suffixes() -> None:
    a = normalize_address_key("742 Evergreen Terrace, Schaumburg, IL")
    b = normalize_address_key("  742 evergreen ter.   schaumburg il ")
    assert a == b


def test_registration_idempotent_duplicate_updates_instead_of_inserting(monkeypatch) -> None:
    fake_pg = MagicMock(spec=PostgresDataSource)
    existing_stop = CarolsStop(
        id=55,
        name="Daniel Rajan",
        family_members=["Daniel"],
        address="742 Evergreen Ter, Schaumburg, IL",
        zipcode="60193",
        date="2026-12-08",
        day="Tuesday",
        referral="Carols-CIC",
    )
    fake_pg.find_stop_by_normalized_address.return_value = existing_stop
    monkeypatch.setattr(web_mod, "data_source", fake_pg)

    client = app.test_client()
    resp = client.post(
        "/registration",
        data={
            "first_name": "Daniel",
            "last_name": "Rajan",
            "address": "742 Evergreen Terrace",
            "city": "Schaumburg",
            "state": "IL",
            "zipcode": "60193",
            "phone": "(847) 555-7777",
            "prayer_request": "Updated prayer request",
        },
    )
    assert resp.status_code == 200
    assert b"without creating a duplicate" in resp.data
    fake_pg.add_stop.assert_not_called()
    fake_pg.update_stop_by_id.assert_called_once()
    call_id, updates = fake_pg.update_stop_by_id.call_args.args
    assert call_id == 55
    assert updates["contact_number"] == "(847) 555-7777"
    assert updates["prayer_request"] == "Updated prayer request"


def test_api_optimize_schedule_reorders_and_saves_miles(monkeypatch) -> None:
    fake_pg = MagicMock(spec=PostgresDataSource)
    fake_pg.get_stops.return_value = [
        CarolsStop(id=1, name="North-1", family_members=[], address="1 N St, BG, IL", visit_order=10, latitude=42.20, longitude=-87.96),
        CarolsStop(id=2, name="South-1", family_members=[], address="2 S St, MP, IL", visit_order=20, latitude=42.06, longitude=-87.96),
        CarolsStop(id=3, name="North-2", family_members=[], address="3 N St, BG, IL", visit_order=30, latitude=42.19, longitude=-87.95),
        CarolsStop(id=4, name="South-2", family_members=[], address="4 S St, MP, IL", visit_order=40, latitude=42.07, longitude=-87.95),
    ]
    monkeypatch.setattr(web_mod, "data_source", fake_pg)

    client = app.test_client()
    resp = client.post("/api/schedule/optimize", json={"date": "2026-12-01", "lock_last_id": 4})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["saved_miles"] > 5.0
    assert data["order_ids"][-1] == 4
    fake_pg.reorder_stops.assert_called_once_with(ids=data["order_ids"], date="2026-12-01")


def test_api_split_teams_assigns_team1_and_team2(monkeypatch) -> None:
    fake_pg = MagicMock(spec=PostgresDataSource)
    fake_pg.get_stops.return_value = [
        CarolsStop(id=1, name="HW-1", family_members=[], address="1 HW, IL", latitude=42.22, longitude=-88.07),
        CarolsStop(id=2, name="VH-1", family_members=[], address="2 VH, IL", latitude=42.22, longitude=-87.95),
        CarolsStop(id=3, name="HW-2", family_members=[], address="3 HW, IL", latitude=42.23, longitude=-88.06),
        CarolsStop(id=4, name="VH-2", family_members=[], address="4 VH, IL", latitude=42.23, longitude=-87.94),
    ]
    monkeypatch.setattr(web_mod, "data_source", fake_pg)

    client = app.test_client()
    resp = client.post("/api/schedule/split_teams", json={"date": "2026-12-11"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["team1_count"] == 2
    assert data["team2_count"] == 2
    assert fake_pg.update_stop_by_id.call_count == 4


def test_sse_event_bus_and_tracker_status_etas(monkeypatch) -> None:
    bus = TrackerEventBus()
    gen = bus.subscribe(heartbeat_seconds=0.05)
    # Trigger publish from main thread after priming generator in background or verify heartbeat + message
    import threading

    def _pub() -> None:
        bus.publish("visited_toggled", {"id": 7, "visited": True})

    t = threading.Timer(0.01, _pub)
    t.start()
    chunk = next(gen)
    gen.close()
    assert chunk.startswith("data: ")
    assert "visited_toggled" in chunk

    client = app.test_client()
    status_resp = client.get("/api/tracker_status?day=Friday")
    assert status_resp.status_code == 200
    payload = status_resp.get_json()
    assert "etas" in payload
    assert "visited_today" in payload
    assert "total_today" in payload

    stream_resp = client.get("/api/stream/tracker?day=Friday", buffered=False)
    assert stream_resp.status_code == 200
    assert stream_resp.mimetype == "text/event-stream"
    first_frame = next(stream_resp.response).decode("utf-8")
    stream_resp.close()
    assert '"event": "snapshot"' in first_frame


