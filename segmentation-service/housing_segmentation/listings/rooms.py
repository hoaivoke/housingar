"""Validated rectangular apartment plans in the +Y-up room coordinate frame."""

import math
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StrictBool,
    StrictStr,
    field_validator,
    model_validator,
)

EPS = 1e-6
FiniteNumber = Annotated[FiniteFloat, Field(strict=True)]
Point = Annotated[list[FiniteNumber], Field(min_length=2, max_length=2)]
Identifier = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Room(Record):
    room_id: Identifier
    name: Annotated[StrictStr, Field(min_length=1, max_length=40)]
    kind: Literal["living", "kitchen", "bedroom", "bathroom", "hall", "closet", "other"]
    polygon: Annotated[list[Point], Field(min_length=4, max_length=4)]

    @field_validator("name")
    @classmethod
    def trimmed_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Room name must not be blank")
        return value

    @model_validator(mode="after")
    def rectangle(self):
        x0, z0, x1, z1 = bounds(self.polygon)
        if not (0.6 - EPS <= x1 - x0 <= 20 + EPS and 0.6 - EPS <= z1 - z0 <= 20 + EPS):
            raise ValueError("Each room side must be 0.6–20 metres")
        if any(abs(v) > 10000 for point in self.polygon for v in point):
            raise ValueError(
                "Plan coordinates must be within 10,000 metres of the origin"
            )
        corners = [(x0, z0), (x0, z1), (x1, z1), (x1, z0)]
        if any(not any(close(p, q) for q in self.polygon) for p in corners):
            raise ValueError("Room polygon must be an axis-aligned rectangle")
        for a, b in zip(self.polygon, self.polygon[1:] + self.polygon[:1]):
            if (abs(a[0] - b[0]) < EPS) == (abs(a[1] - b[1]) < EPS):
                raise ValueError("Rectangle corners must follow consecutive wall edges")
        # Negative signed XZ area gives an upward (+Y) 3D normal.
        self.polygon = [list(p) for p in corners]
        return self


class Opening(Record):
    opening_id: Identifier
    kind: Literal["door", "window"]
    room_ids: Annotated[list[Identifier], Field(min_length=1, max_length=2)]
    a: Point
    b: Point
    height_m: Annotated[FiniteNumber, Field(gt=0, le=20)]
    sill_m: Annotated[FiniteNumber, Field(ge=0, le=20)]


class RoomLayout(Record):
    schema_name: Literal["housingar.room.v1"] = Field(alias="schema")
    listing_id: Identifier
    estimated: StrictBool
    source: Literal["estimated", "user_edited"]
    units: Literal["meters"]
    ceiling_height_m: Annotated[FiniteNumber, Field(ge=0.6, le=20)]
    total_area_m2: Annotated[FiniteNumber, Field(gt=0)]
    rooms: Annotated[list[Room], Field(min_length=1, max_length=20)]
    openings: Annotated[list[Opening], Field(max_length=60)]

    @model_validator(mode="after")
    def geometry(self):
        if self.estimated != (self.source == "estimated"):
            raise ValueError("estimated and source must agree")
        rooms = {r.room_id: r for r in self.rooms}
        if len(rooms) != len(self.rooms):
            raise ValueError("Room IDs must be unique")
        if len({o.opening_id for o in self.openings}) != len(self.openings):
            raise ValueError("Opening IDs must be unique")
        rectangles = {r.room_id: bounds(r.polygon) for r in self.rooms}
        area = sum((r[2] - r[0]) * (r[3] - r[1]) for r in rectangles.values())
        outer = bounds([p for r in self.rooms for p in r.polygon])
        outer_area = (outer[2] - outer[0]) * (outer[3] - outer[1])
        for i, r in enumerate(self.rooms):
            a = rectangles[r.room_id]
            for other in self.rooms[i + 1 :]:
                b = rectangles[other.room_id]
                if (
                    min(a[2], b[2]) - max(a[0], b[0]) > EPS
                    and min(a[3], b[3]) - max(a[1], b[1]) > EPS
                ):
                    raise ValueError("Rooms must not overlap")
        if abs(area - outer_area) > max(1e-5, outer_area * 1e-7):
            raise ValueError("Rooms must tile a rectangle without gaps")
        if abs(area - self.total_area_m2) > max(0.0001, area * 0.03):
            raise ValueError("total_area_m2 disagrees with the room polygons")
        self.total_area_m2 = area
        graph = {key: set() for key in rooms}
        entry = set()
        windows = set()
        for opening in self.openings:
            ids = opening.room_ids
            if len(set(ids)) != len(ids) or any(key not in rooms for key in ids):
                raise ValueError("Opening references unknown or repeated rooms")
            if opening.height_m + opening.sill_m > self.ceiling_height_m + EPS:
                raise ValueError("Opening extends above the ceiling")
            if opening.kind == "door" and opening.sill_m > EPS:
                raise ValueError("Doors must start at floor level")
            if not segment_on_rectangle(rectangles[ids[0]], opening.a, opening.b):
                raise ValueError("Opening must lie along a room wall")
            if len(ids) == 2:
                if opening.kind != "door":
                    raise ValueError("Windows must lie on exterior walls")
                if not segment_on_rectangle(rectangles[ids[1]], opening.a, opening.b):
                    raise ValueError(
                        "Interior opening must lie on the wall shared by both rooms"
                    )
                graph[ids[0]].add(ids[1])
                graph[ids[1]].add(ids[0])
            else:
                if not segment_on_rectangle(outer, opening.a, opening.b):
                    raise ValueError("Single-room opening must lie on an exterior wall")
                if opening.kind == "door":
                    entry.add(ids[0])
                else:
                    windows.add(ids[0])
        reached = set(entry)
        queue = list(entry)
        while queue:
            for key in graph[queue.pop()] - reached:
                reached.add(key)
                queue.append(key)
        if reached != set(rooms):
            raise ValueError(
                "Every room must be reachable through doors from an exterior entry"
            )
        if any(
            r.kind in ("living", "bedroom") and r.room_id not in windows
            for r in self.rooms
        ):
            raise ValueError("Every living room and bedroom needs an exterior window")
        return self


def bounds(points):
    return (
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    )


def close(a, b):
    return all(abs(x - y) < EPS for x, y in zip(a, b))


def segment_on_rectangle(rect, a, b):
    x0, z0, x1, z1 = rect
    if math.dist(a, b) < 0.1:
        return False
    if abs(a[0] - b[0]) < EPS:
        return (
            (abs(a[0] - x0) < EPS or abs(a[0] - x1) < EPS)
            and min(a[1], b[1]) >= z0 - EPS
            and max(a[1], b[1]) <= z1 + EPS
        )
    if abs(a[1] - b[1]) < EPS:
        return (
            (abs(a[1] - z0) < EPS or abs(a[1] - z1) < EPS)
            and min(a[0], b[0]) >= x0 - EPS
            and max(a[0], b[0]) <= x1 + EPS
        )
    return False


def estimated_layout(listing):
    beds = int(listing["bedrooms"])
    baths = max(1, math.ceil(listing["bathrooms"]))
    if beds + baths + 3 > 20:
        raise ValueError("Too many bedrooms/bathrooms for a 20-room estimate")
    area = (
        float(listing["square_feet"] or (450 + 250 * beds + 100 * (baths - 1)))
        * 0.09290304
    )
    rooms = []
    openings = []

    def room(identifier, name, kind, x0, z0, x1, z1):
        rooms.append(
            dict(
                room_id=identifier,
                name=name,
                kind=kind,
                polygon=[[x0, z0], [x0, z1], [x1, z1], [x1, z0]],
            )
        )

    def opening(kind, ids, a, b):
        openings.append(
            dict(
                opening_id=f"{kind}_{len(openings) + 1:02d}",
                kind=kind,
                room_ids=ids,
                a=a,
                b=b,
                height_m=2.03 if kind == "door" else 1.1,
                sill_m=0 if kind == "door" else 0.9,
            )
        )

    def horizontal(kind, ids, z, lo, hi):
        width = min(0.9 if kind == "door" else 1.2, (hi - lo) * 0.65)
        mid = (lo + hi) / 2
        opening(kind, ids, [mid - width / 2, z], [mid + width / 2, z])

    if beds == 0:
        # With exactly two rectangular rooms, the bathroom is a full-width strip.
        width = max(3.2, min(4.0, math.sqrt(area / 2)), (area - 4.8) / 19.9)
        bath_depth = 4.8 / width
        depth = area / width
        room("bath_1", "Bathroom", "bathroom", 0, 0, width, bath_depth)
        room("living", "Main room", "living", 0, bath_depth, width, depth)
        horizontal("door", ["bath_1", "living"], bath_depth, 0, width)
        horizontal("door", ["living"], depth, 0, width * 0.45)
        horizontal("window", ["living"], depth, width * 0.55, width)
    else:
        bed_area = max(9.0, min(12.0, (0.5 * area - 4.5 * baths) / beds))
        private_area = bed_area * beds + 4.5 * baths
        if area <= private_area:
            raise ValueError("Listing area is too small for the stated room counts")
        sizes = [bed_area] * beds + [4.5] * baths
        minimum = max(
            0.6,
            max(sizes) / 20,
            private_area / 20,
            1.6 * private_area / (area - private_area),
        )
        maximum = min(
            20,
            min(sizes) / 0.6,
            21 * private_area / (area - private_area),
            private_area * 0.35 / 0.6,
        )
        if minimum > maximum:
            raise ValueError(
                "Listing area cannot accommodate the stated rooms and hall"
            )
        private_depth = min(
            maximum, max(minimum, 2.7, min(3.8, private_area / math.sqrt(area * 1.3)))
        )
        width = private_area / private_depth
        hall_depth = 1.0
        public_start = private_depth + hall_depth
        depth = area / width
        split = width * 0.65
        x = 0.0
        for i, (kind, size) in enumerate(
            [("bedroom", bed_area)] * beds + [("bathroom", 4.5)] * baths
        ):
            identifier = f"bed_{i + 1}" if kind == "bedroom" else f"bath_{i - beds + 1}"
            name = (
                f"Bedroom {i + 1}" if kind == "bedroom" else f"Bathroom {i - beds + 1}"
            )
            end = x + size / private_depth
            if i == beds + baths - 1:
                end = width
            room(identifier, name, kind, x, 0, end, private_depth)
            horizontal("door", [identifier, "hall"], private_depth, x, end)
            if kind == "bedroom":
                horizontal("window", [identifier], 0, x, end)
            x = end
        room("hall", "Hall", "hall", 0, private_depth, width, public_start)
        room("living", "Living room", "living", 0, public_start, split, depth)
        room("kitchen", "Kitchen", "kitchen", split, public_start, width, depth)
        horizontal("door", ["hall", "living"], public_start, 0, split)
        horizontal("door", ["hall", "kitchen"], public_start, split, width)
        horizontal("window", ["living"], depth, 0, split)
        horizontal("window", ["kitchen"], depth, split, width)
        opening("door", ["hall"], [0, private_depth + 0.1], [0, public_start - 0.1])
    payload = {
        "schema": "housingar.room.v1",
        "listing_id": listing["listing_id"],
        "estimated": True,
        "source": "estimated",
        "units": "meters",
        "ceiling_height_m": 2.44,
        "total_area_m2": area,
        "rooms": rooms,
        "openings": openings,
    }
    return RoomLayout.model_validate(payload).model_dump(by_alias=True)
