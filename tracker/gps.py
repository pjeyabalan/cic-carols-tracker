from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Generator, List, Optional

from tracker.data_sources import CarolsStop


@dataclass(frozen=True)
class Coordinate:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class RoutePoint:
    stop: CarolsStop
    coordinate: Coordinate
    distance_from_previous_km: float
    duration: timedelta
    eta: datetime


@dataclass
class RoutePlan:
    start_time: datetime
    start_coordinate: Coordinate
    points: List[RoutePoint]


def _coords_from_zip(zipcode: str) -> Coordinate:
    base_lat = 40.0
    base_lon = -74.0
    hashed = hashlib.sha256(zipcode.encode("utf-8")).hexdigest()
    number = int(hashed[:8], 16)
    lat_offset = (number % 1000) / 1000 * 0.5
    lon_offset = ((number >> 8) % 1000) / 1000 * 0.5
    return Coordinate(latitude=base_lat + lat_offset, longitude=base_lon + lon_offset)


def _haversine_distance(a: Coordinate, b: Coordinate) -> float:
    lat1, lon1 = math.radians(a.latitude), math.radians(a.longitude)
    lat2, lon2 = math.radians(b.latitude), math.radians(b.longitude)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    r = 6371.0
    x = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


def simulate_route(
    stops: Iterable[CarolsStop],
    start_coordinate: Optional[Coordinate] = None,
    speed_kmh: float = 25.0,
    start_time: Optional[datetime] = None,
) -> RoutePlan:
    start_coordinate = start_coordinate or Coordinate(latitude=40.0, longitude=-74.0)
    start_time = start_time or datetime.now()
    previous_coordinate = start_coordinate
    current_time = start_time
    points: List[RoutePoint] = []
    for stop in stops:
        coordinate = _coords_from_zip(stop.zipcode or "00000")
        distance = _haversine_distance(previous_coordinate, coordinate)
        hours = distance / speed_kmh if speed_kmh > 0 else 0
        duration = timedelta(hours=hours)
        arrival_time = current_time + duration
        points.append(
            RoutePoint(
                stop=stop,
                coordinate=coordinate,
                distance_from_previous_km=distance,
                duration=duration,
                eta=arrival_time,
            )
        )
        previous_coordinate = coordinate
        current_time = arrival_time
    return RoutePlan(start_time=start_time, start_coordinate=start_coordinate, points=points)


def interpolate_coordinate(a: Coordinate, b: Coordinate, fraction: float) -> Coordinate:
    return Coordinate(
        latitude=a.latitude + (b.latitude - a.latitude) * fraction,
        longitude=a.longitude + (b.longitude - a.longitude) * fraction,
    )


def simulate_live_updates(
    plan: RoutePlan, update_interval_seconds: int = 15
) -> Generator[dict, None, None]:
    current_point = plan.start_coordinate
    current_time = plan.start_time
    for idx, point in enumerate(plan.points):
        segments = max(2, int(point.duration.total_seconds() / update_interval_seconds))
        for step in range(segments):
            fraction = (step + 1) / segments
            timestamp = current_time + timedelta(seconds=update_interval_seconds * (step + 1))
            if timestamp > point.eta:
                timestamp = point.eta
            location = interpolate_coordinate(current_point, point.coordinate, fraction)
            eta_remaining = max(point.eta - timestamp, timedelta(seconds=0))
            yield {
                "timestamp": timestamp,
                "latitude": location.latitude,
                "longitude": location.longitude,
                "eta_to_destination": eta_remaining,
                "next_stop": point.stop.name,
                "distance_to_next_km": point.distance_from_previous_km * (1 - fraction),
            }
        current_point = point.coordinate
        current_time = point.eta