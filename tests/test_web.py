from unittest.mock import MagicMock

import tracker.web as web_mod
from tracker.data_sources import CarolsStop, PostgresDataSource, resolve_pg_dsn
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
        day="Wednesday",
        date="2026-12-09",
        scheduled_time=None,
        visit_order=None,
        address_visited=False,
        referral="Benny-CIC",
    )


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
                date="2026-12-09",
                day="Wednesday",
                referral="Carols-CIC",
            )
        ]
        if date in (None, "2026-12-09")
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
    assert b"2026-12-17" in resp.data
    assert b"2026-12-18" not in resp.data


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
