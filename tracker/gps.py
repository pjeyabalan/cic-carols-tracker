from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Generator, Iterable, List, Optional, Tuple

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


# Real Chicagoland zipcode centroids so stops without street-level lat/lon still map accurately
CHICAGOLAND_ZIP_COORDS: Dict[str, Tuple[float, float]] = {
    "60089": (42.1663, -87.9631),  # Buffalo Grove
    "60090": (42.1392, -87.9290),  # Wheeling
    "60056": (42.0664, -87.9373),  # Mount Prospect
    "60016": (42.0451, -87.8834),  # Des Plaines
    "60070": (42.1095, -87.9281),  # Prospect Heights
    "60025": (42.0795, -87.8156),  # Glenview
    "60062": (42.1275, -87.8289),  # Northbrook
    "60004": (42.1084, -87.9776),  # Arlington Heights North
    "60005": (42.0645, -87.9845),  # Arlington Heights South
    "60008": (42.0750, -88.0254),  # Rolling Meadows
    "60007": (42.0039, -87.9973),  # Elk Grove Village
    "60120": (42.0354, -88.2626),  # Elgin East
    "60123": (42.0464, -88.3162),  # Elgin West
    "60124": (42.0620, -88.3900),  # Elgin NW
    "60177": (41.9942, -88.2995),  # South Elgin
    "60103": (41.9792, -88.1848),  # Bartlett
    "60193": (42.0111, -88.0826),  # Schaumburg South
    "60194": (42.0439, -88.1112),  # Schaumburg Central
    "60195": (42.0684, -88.0681),  # Schaumburg North
    "60173": (42.0469, -88.0398),  # Schaumburg East
    "60067": (42.1103, -88.0462),  # Palatine
    "60074": (42.1389, -88.0195),  # Palatine North
    "60010": (42.1539, -88.1362),  # Barrington
    "60047": (42.2156, -88.0679),  # Hawthorn Woods / Lake Zurich
    "60061": (42.2267, -87.9715),  # Vernon Hills
    "60060": (42.2631, -88.0039),  # Mundelein
    "60048": (42.2831, -87.9531),  # Libertyville
    "60069": (42.1900, -87.9159),  # Lincolnshire
    "60031": (42.3703, -87.9320),  # Gurnee
    "60030": (42.3442, -88.0417),  # Grayslake
    "60073": (42.3536, -88.0934),  # Round Lake
    "60085": (42.3636, -87.8448),  # Waukegan
    "60169": (42.0475, -88.1481),  # Hoffman Estates
    "60192": (42.0731, -88.1981),  # Hoffman Estates West
    "60107": (42.0256, -88.1784),  # Streamwood
}


def _coords_from_zip(zipcode: str) -> Coordinate:
    clean_zip = (zipcode or "").strip()[:5]
    if clean_zip in CHICAGOLAND_ZIP_COORDS:
        lat, lon = CHICAGOLAND_ZIP_COORDS[clean_zip]
        return Coordinate(latitude=lat, longitude=lon)
    base_lat = 42.08
    base_lon = -88.03
    hashed = hashlib.sha256((zipcode or "00000").encode("utf-8")).hexdigest()
    number = int(hashed[:8], 16)
    lat_offset = (number % 1000) / 1000 * 0.25
    lon_offset = ((number >> 8) % 1000) / 1000 * 0.25
    return Coordinate(latitude=base_lat + lat_offset, longitude=base_lon + lon_offset)


def resolve_stop_coordinate(stop: CarolsStop) -> Coordinate:
    """Return exact stop Coordinate if geocoded, else deterministic fallback from zipcode/address."""
    if stop.latitude is not None and stop.longitude is not None:
        return Coordinate(latitude=float(stop.latitude), longitude=float(stop.longitude))
    seed = (stop.zipcode or "").strip()[:5]
    if seed in CHICAGOLAND_ZIP_COORDS:
        base_lat, base_lon = CHICAGOLAND_ZIP_COORDS[seed]
        # Tiny deterministic street-level jitter (~150m) when only zipcode is known so distinct houses don't stack at 0.0 km
        addr_hash = int(hashlib.sha256((stop.address or stop.name).encode("utf-8")).hexdigest()[:8], 16)
        jitter_lat = ((addr_hash % 200) - 100) * 0.00008
        jitter_lon = (((addr_hash >> 8) % 200) - 100) * 0.00008
        return Coordinate(latitude=base_lat + jitter_lat, longitude=base_lon + jitter_lon)
    return _coords_from_zip(stop.zipcode or stop.address or "00000")


def haversine_km(a: Coordinate, b: Coordinate) -> float:
    lat1, lon1 = math.radians(a.latitude), math.radians(a.longitude)
    lat2, lon2 = math.radians(b.latitude), math.radians(b.longitude)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    r = 6371.0
    x = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(x)))


def haversine_miles(a: Coordinate, b: Coordinate) -> float:
    return haversine_km(a, b) * 0.621371


_haversine_distance = haversine_km


def route_total_miles(coords: List[Coordinate]) -> float:
    if len(coords) < 2:
        return 0.0
    return sum(haversine_miles(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


def optimize_route_2opt(
    stops: List[CarolsStop],
    *,
    start_coord: Optional[Coordinate] = None,
    lock_last_id: Optional[int] = None,
) -> Tuple[List[CarolsStop], float, float]:
    """Optimize stop visit sequence using Nearest-Neighbor initialization + 2-Opt local search.

    If `lock_last_id` matches a stop's id, that stop is pinned as the final destination
    (e.g., the dinner host / last house of the night).
    Returns `(optimized_stops, baseline_miles, optimized_miles)`.
    """
    n = len(stops)
    if n <= 2:
        coords = [resolve_stop_coordinate(s) for s in stops]
        dist = route_total_miles(coords)
        return list(stops), round(dist, 2), round(dist, 2)

    coords_by_idx = [resolve_stop_coordinate(s) for s in stops]
    baseline_miles = route_total_miles(coords_by_idx)

    # Separate optional pinned final stop
    pinned_idx: Optional[int] = None
    if lock_last_id is not None:
        for i, s in enumerate(stops):
            if s.id == lock_last_id:
                pinned_idx = i
                break

    free_indices = [i for i in range(n) if i != pinned_idx]

    # Precompute symmetric pairwise distance matrix in miles
    dist_matrix = [
        [haversine_miles(coords_by_idx[i], coords_by_idx[j]) for j in range(n)]
        for i in range(n)
    ]

    # Pass 1: Nearest-Neighbor Greedy Tour Construction
    if start_coord is not None:
        first_idx = min(
            free_indices,
            key=lambda idx: haversine_miles(start_coord, coords_by_idx[idx]),
        )
    else:
        # Start from northernmost/westernmost stop for a natural sweep across the neighborhood
        first_idx = max(
            free_indices,
            key=lambda idx: (coords_by_idx[idx].latitude, -coords_by_idx[idx].longitude),
        )

    order: List[int] = [first_idx]
    remaining = set(free_indices) - {first_idx}
    while remaining:
        last = order[-1]
        nxt = min(remaining, key=lambda idx: dist_matrix[last][idx])
        order.append(nxt)
        remaining.remove(nxt)

    if pinned_idx is not None:
        order.append(pinned_idx)

    # Pass 2: 2-Opt Segment-Reversal Local Search (uncrosses any X-shaped path intersections)
    # Valid reversal indices: [start_i .. end_k]; if final stop is pinned, never move order[-1]
    max_reversible = len(order) - 2 if pinned_idx is not None else len(order) - 1

    def path_cost(seq: List[int]) -> float:
        total = 0.0
        if start_coord is not None and seq:
            total += haversine_miles(start_coord, coords_by_idx[seq[0]])
        for pos in range(len(seq) - 1):
            total += dist_matrix[seq[pos]][seq[pos + 1]]
        return total

    improved = True
    best_cost = path_cost(order)
    iterations = 0
    while improved and iterations < 200:
        improved = False
        iterations += 1
        for i in range(0, max_reversible):
            for k in range(i + 1, max_reversible + 1):
                candidate = order[:i] + list(reversed(order[i : k + 1])) + order[k + 1 :]
                cand_cost = path_cost(candidate)
                if cand_cost + 1e-6 < best_cost:
                    order = candidate
                    best_cost = cand_cost
                    improved = True

    optimized_stops = [stops[idx] for idx in order]
    optimized_coords = [coords_by_idx[idx] for idx in order]
    optimized_miles = route_total_miles(optimized_coords)
    return optimized_stops, round(baseline_miles, 2), round(optimized_miles, 2)


def cluster_two_teams(stops: List[CarolsStop]) -> Tuple[List[CarolsStop], List[CarolsStop]]:
    """Partition stops into two geographically cohesive, size-balanced teams (TEAM1, TEAM2)
    and optimize each team's route via 2-Opt TSP.
    """
    if len(stops) <= 1:
        return list(stops), []

    coords = [resolve_stop_coordinate(s) for s in stops]
    n = len(stops)

    # Pick the two mutually farthest stops as initial centroids
    best_pair = (0, 1)
    max_dist = -1.0
    for i in range(n):
        for j in range(i + 1, n):
            d = haversine_miles(coords[i], coords[j])
            if d > max_dist:
                max_dist = d
                best_pair = (i, j)

    c1 = coords[best_pair[0]]
    c2 = coords[best_pair[1]]
    max_team_size = (n + 1) // 2 + 1  # Keep teams balanced within ~1-2 houses

    assignments = [0] * n
    for _ in range(15):
        # Sort stops by strongest preference difference |d(c1) - d(c2)| so clear cluster members claim spots first
        scored = sorted(
            range(n),
            key=lambda idx: abs(haversine_miles(coords[idx], c1) - haversine_miles(coords[idx], c2)),
            reverse=True,
        )
        count1 = 0
        count2 = 0
        new_assign = [0] * n
        for idx in scored:
            d1 = haversine_miles(coords[idx], c1)
            d2 = haversine_miles(coords[idx], c2)
            if count1 >= max_team_size:
                new_assign[idx] = 1
                count2 += 1
            elif count2 >= max_team_size:
                new_assign[idx] = 0
                count1 += 1
            elif d1 <= d2:
                new_assign[idx] = 0
                count1 += 1
            else:
                new_assign[idx] = 1
                count2 += 1
        if new_assign == assignments:
            break
        assignments = new_assign

        g1 = [coords[i] for i in range(n) if assignments[i] == 0]
        g2 = [coords[i] for i in range(n) if assignments[i] == 1]
        if g1:
            c1 = Coordinate(
                latitude=sum(p.latitude for p in g1) / len(g1),
                longitude=sum(p.longitude for p in g1) / len(g1),
            )
        if g2:
            c2 = Coordinate(
                latitude=sum(p.latitude for p in g2) / len(g2),
                longitude=sum(p.longitude for p in g2) / len(g2),
            )

    team1_raw = [stops[i] for i in range(n) if assignments[i] == 0]
    team2_raw = [stops[i] for i in range(n) if assignments[i] == 1]
    team1_opt, _, _ = optimize_route_2opt(team1_raw)
    team2_opt, _, _ = optimize_route_2opt(team2_raw)
    return team1_opt, team2_opt


def forecast_dynamic_etas(
    stops: List[CarolsStop],
    reference_time: datetime,
    *,
    dwell_minutes: int = 15,
    avg_speed_mph: float = 22.0,
    road_detour_factor: float = 1.28,
) -> Dict[int, str]:
    """Compute live estimated arrival times (by stop index) for unvisited stops.

    Uses real Haversine distance * road detour factor + `dwell_minutes` per house visit.
    """
    etas: Dict[int, str] = {}
    if not stops:
        return etas

    first_unvisited: Optional[int] = None
    for idx, s in enumerate(stops):
        if not bool(s.address_visited):
            first_unvisited = idx
            break
    if first_unvisited is None:
        return etas

    # Determine realistic anchor timestamp
    any_visited = any(bool(s.address_visited) for s in stops)
    if any_visited:
        cursor_time = reference_time
    else:
        # Before night begins, anchor at 6:00 PM if earlier than 6:00 PM
        six_pm = reference_time.replace(hour=18, minute=0, second=0, microsecond=0)
        cursor_time = reference_time if reference_time >= six_pm else six_pm

    coords = [resolve_stop_coordinate(s) for s in stops]
    for idx in range(first_unvisited, len(stops)):
        stop = stops[idx]
        if stop.scheduled_time:
            try:
                parsed = datetime.strptime(stop.scheduled_time.strip(), "%H:%M")
                etas[idx] = parsed.strftime("%-I:%M %p")
                cursor_time = cursor_time.replace(hour=parsed.hour, minute=parsed.minute)
                continue
            except ValueError:
                pass
        if idx == first_unvisited:
            if any_visited and first_unvisited > 0:
                drive_miles = haversine_miles(coords[idx - 1], coords[idx]) * road_detour_factor
                drive_min = max(3, round((drive_miles / avg_speed_mph) * 60))
                cursor_time = cursor_time + timedelta(minutes=drive_min)
            etas[idx] = f"~{cursor_time.strftime('%-I:%M %p')}"
        else:
            drive_miles = haversine_miles(coords[idx - 1], coords[idx]) * road_detour_factor
            drive_min = max(3, round((drive_miles / avg_speed_mph) * 60))
            cursor_time = cursor_time + timedelta(minutes=dwell_minutes + drive_min)
            etas[idx] = f"~{cursor_time.strftime('%-I:%M %p')}"
    return etas


def simulate_route(
    stops: Iterable[CarolsStop],
    start_coordinate: Optional[Coordinate] = None,
    speed_kmh: float = 25.0,
    start_time: Optional[datetime] = None,
) -> RoutePlan:
    stop_list = list(stops)
    if start_coordinate is None:
        start_coordinate = (
            resolve_stop_coordinate(stop_list[0])
            if stop_list
            else Coordinate(latitude=42.1663, longitude=-87.9631)
        )
    start_time = start_time or datetime.now()
    previous_coordinate = start_coordinate
    current_time = start_time
    points: List[RoutePoint] = []
    for stop in stop_list:
        coordinate = resolve_stop_coordinate(stop)
        distance = haversine_km(previous_coordinate, coordinate)
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
    for point in plan.points:
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
