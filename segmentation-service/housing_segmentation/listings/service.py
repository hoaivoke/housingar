"""Quota-aware RentCast access and local listing/room/furniture persistence."""

import hashlib
import json
import math
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Annotated, Literal
from urllib.parse import urlencode, urlsplit

import logging

import httpx
from pydantic import Field, FiniteFloat, StrictStr, field_validator, model_validator

from ..captures.jobs import atomic_json
from ..engine import InputError
from .rooms import FiniteNumber, Identifier, Record, estimated_layout

RENTCAST_URL = "https://api.rentcast.io/v1/listings/rental/long-term"
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
TTL_SECONDS = 86400
DATASET_SCHEMA = "housingar.listings_dataset.v1"
logger = logging.getLogger(__name__)


def normalize_address(value):
    value = unicodedata.normalize("NFKC", value).strip(" ,").casefold()
    return re.sub(r"\s*,\s*", ",", " ".join(value.split()))


class ListingQuery(Record):
    near: Annotated[str | None, Field(min_length=1, max_length=500)] = None
    lat: Annotated[FiniteFloat | None, Field(ge=-90, le=90)] = None
    lon: Annotated[FiniteFloat | None, Field(ge=-180, le=180)] = None
    radius_mi: Annotated[FiniteFloat, Field(gt=0, le=25)] = 5.0
    min_rent: Annotated[FiniteFloat | None, Field(ge=0, le=10000000)] = None
    max_rent: Annotated[FiniteFloat | None, Field(ge=0, le=10000000)] = None
    bedrooms: str | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 40

    @field_validator("bedrooms")
    @classmethod
    def bedrooms_valid(cls, value):
        if value is None:
            return None
        value = value.strip()
        if not re.fullmatch(r"\d{1,2}\+?", value):
            raise ValueError("bedrooms must be 0–99 or a minimum such as 3+")
        return str(int(value.rstrip("+"))) + ("+" if value.endswith("+") else "")

    @model_validator(mode="after")
    def location(self):
        if self.near is not None:
            self.near = normalize_address(self.near)
            if not self.near:
                raise ValueError("near must not be blank")
            if self.lat is not None or self.lon is not None:
                raise ValueError("Use near OR both lat and lon")
        elif self.lat is None or self.lon is None:
            raise ValueError("Supply near OR both lat and lon")
        if (
            self.min_rent is not None
            and self.max_rent is not None
            and self.min_rent > self.max_rent
        ):
            raise ValueError("min_rent must not exceed max_rent")
        return self

    def normalized(self):
        value = self.model_dump()
        for key in ("lat", "lon"):
            if value[key] is not None:
                value[key] = round(value[key], 6) or 0.0
        return value


class Placement(Record):
    placement_id: Identifier
    item_id: Identifier
    name: Annotated[StrictStr, Field(min_length=1, max_length=80)]
    position: Annotated[list[FiniteNumber], Field(min_length=3, max_length=3)]
    yaw_deg: FiniteNumber

    @field_validator("name")
    @classmethod
    def trimmed(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Placement name must not be blank")
        return value


class RoomFurniture(Record):
    schema_name: Literal["housingar.room_furniture.v1"] = Field(alias="schema")
    listing_id: Identifier
    placements: Annotated[list[Placement], Field(max_length=200)]
    updated: str | None = None

    @model_validator(mode="after")
    def unique_ids(self):
        if len({p.placement_id for p in self.placements}) != len(self.placements):
            raise ValueError("placement_id values must be unique")
        return self


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def number(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        return None
    return value


def text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def distance_mi(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    h = (
        math.sin((b - a) / 2) ** 2
        + math.cos(a) * math.cos(b) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 3958.7613 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def urls(address, listing_url=None):
    try:
        parsed = urlsplit(listing_url or "")
        valid = parsed.scheme in ("https", "http") and bool(parsed.hostname)
    except ValueError:
        valid = False
    return {
        "search_url": "https://www.google.com/search?"
        + urlencode({"q": address + " for rent"}),
        "maps_url": "https://www.google.com/maps/search/?"
        + urlencode({"api": 1, "query": address}),
        "listing_url": listing_url if valid else None,
    }


def normalize_listing(raw, center):
    if not isinstance(raw, dict) or raw.get("status", "Active") != "Active":
        return None
    address = text(raw.get("formattedAddress")) or ", ".join(
        filter(
            None,
            [
                text(raw.get("addressLine1")),
                text(raw.get("addressLine2")),
                text(raw.get("city")),
                text(raw.get("state")),
                text(raw.get("zipCode")),
            ],
        )
    )
    rent, bedrooms, bathrooms = (
        number(raw.get("price")),
        number(raw.get("bedrooms")),
        number(raw.get("bathrooms")),
    )
    if (
        not address
        or not text(raw.get("id"))
        or rent is None
        or rent <= 0
        or bedrooms is None
        or bedrooms < 0
        or bedrooms != int(bedrooms)
        or bathrooms is None
        or bathrooms < 0
    ):
        return None
    lat, lon = number(raw.get("latitude")), number(raw.get("longitude"))
    if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        lat = lon = None
    distance = (
        distance_mi(center["lat"], center["lon"], lat, lon)
        if center["lat"] is not None and lat is not None
        else None
    )
    agent = raw.get("listingAgent") or {}
    office = raw.get("listingOffice") or {}
    if not isinstance(agent, dict):
        agent = {}
    if not isinstance(office, dict):
        office = {}
    contact = {
        "name": text(agent.get("name")),
        "phone": text(agent.get("phone")) or text(office.get("phone")),
        "email": text(agent.get("email")) or text(office.get("email")),
        "office": text(office.get("name")),
    }
    property_type = text(raw.get("propertyType")) or "Rental"
    title = (
        "Studio" if bedrooms == 0 else f"{int(bedrooms)} bd"
    ) + f" · {bathrooms:g} ba {property_type.lower()}"
    sqft = number(raw.get("squareFootage"))
    days = number(raw.get("daysOnMarket"))
    return {
        "listing_id": "rc_" + digest(raw["id"])[:32],
        "source": "rentcast",
        "sample": False,
        "title": title,
        "address": {
            "line1": text(raw.get("addressLine1")) or "",
            "line2": text(raw.get("addressLine2")) or "",
            "city": text(raw.get("city")) or "",
            "state": text(raw.get("state")) or "",
            "zip": text(raw.get("zipCode")) or "",
            "formatted": address,
        },
        "lat": lat,
        "lon": lon,
        "distance_mi": round(distance, 4) if distance is not None else None,
        "rent_monthly": rent,
        "currency": "USD",
        "bedrooms": int(bedrooms),
        "bathrooms": bathrooms,
        "square_feet": sqft if sqft is not None and sqft > 0 else None,
        "property_type": property_type,
        "listed_date": text(raw.get("listedDate")),
        "days_on_market": int(days) if days is not None and days >= 0 else None,
        "contact": contact if any(contact.values()) else None,
        "links": urls(
            address,
            text(raw.get("listingUrl"))
            or text(raw.get("listingURL"))
            or text(raw.get("url")),
        ),
        "photos": [],
    }


def dataset_listing_valid(item):
    """A dataset row must already be a normalized listing (same shape RentCast rows get)."""
    try:
        return (
            isinstance(item, dict)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item["listing_id"]) is not None
            and number(item["rent_monthly"]) is not None
            and item["rent_monthly"] > 0
            and isinstance(item["bedrooms"], int)
            and not isinstance(item["bedrooms"], bool)
            and item["bedrooms"] >= 0
            and number(item["bathrooms"]) is not None
            and item["bathrooms"] >= 0
            and (item["square_feet"] is None or (number(item["square_feet"]) or 0) > 0)
            and isinstance(item["address"], dict)
            and text(item["address"].get("line1")) is not None
            and isinstance(item["links"], dict)
            and (item.get("lat") is None) == (item.get("lon") is None)
            and (item.get("lat") is None or (number(item["lat"]) is not None and number(item["lon"]) is not None))
        )
    except (KeyError, TypeError):
        return False


class Listings:
    def __init__(self, root, assets, *, api_key=None, client=None, clock=time.time, dataset=None):
        self.root = Path(root) / "listings"
        self.assets = assets
        self.clock = clock
        self._key = (
            os.getenv("RENTCAST_API_KEY", "").strip()
            if api_key is None
            else api_key.strip()
        )
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=False,
            headers={"User-Agent": "HousingAR/1"},
        )
        self._owned_client = client is None
        # Optional local listings file, used instead of fictional samples without a key.
        self._dataset_path = Path(dataset) if dataset else None
        self._dataset = None
        self._search_lock = RLock()
        self._store_lock = RLock()
        for name in ("cache", "records", "geocodes", "layouts", "furniture"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def close(self):
        if self._owned_client:
            self.client.close()

    @staticmethod
    def _read(path):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def _fresh(self, record, ttl=TTL_SECONDS):
        return (
            isinstance(record, dict)
            and isinstance(record.get("created_at"), (int, float))
            and 0 <= self.clock() - record["created_at"] < ttl
        )

    def _load_dataset(self):
        """The dataset file, reloaded when it changes; None if absent or unusable."""
        if self._dataset_path is None:
            return None
        try:
            stat = self._dataset_path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if self._dataset is not None and self._dataset["stamp"] == stamp:
                return self._dataset
            raw = self._dataset_path.read_bytes()
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("schema") != DATASET_SCHEMA:
                raise ValueError(f"expected schema {DATASET_SCHEMA}")
            rows = data.get("listings")
            if not isinstance(rows, list):
                raise ValueError("listings must be a list")
            listings = [dict(r, source="dataset", sample=False) for r in rows if dataset_listing_valid(r)]
            if len(listings) != len(rows):
                logger.warning("Skipped %d malformed dataset listings", len(rows) - len(listings))
            places = [
                p for p in data.get("places") or []
                if isinstance(p, dict) and number(p.get("lat")) is not None and number(p.get("lon")) is not None
            ]
            self._dataset = {
                "stamp": stamp,
                "version": hashlib.sha256(raw).hexdigest()[:16],
                "listings": listings,
                "places": places,
                "about": {
                    key: data.get(key)
                    for key in ("name", "source", "source_url", "collected", "note")
                },
            }
            return self._dataset
        except (OSError, ValueError) as exc:
            if self._dataset is None or self._dataset.get("error") != str(exc):
                logger.warning("Listings dataset %s not used: %s", self._dataset_path, exc)
            self._dataset = {"stamp": None, "error": str(exc)}
            return None

    @staticmethod
    def _place(near, places, loose):
        """A dataset place for a bare city/ZIP query (or, loosely, any ZIP or city in it)."""
        for place in places:
            if near in place.get("names", []) or near in place.get("zips", []):
                return place
        if loose:
            for zip_code in reversed(re.findall(r"\b\d{5}\b", near)):
                for place in places:
                    if zip_code in place.get("zips", []):
                        return place
            for part in (p.strip() for p in near.split(",")):
                for place in places:
                    if part in place.get("names", []):
                        return place
        return None

    def _located(self, query, places):
        """Census geocoding, with the dataset's places for city names and ZIP codes."""
        if query["near"] is not None and places:
            place = self._place(query["near"], places, loose=False)
            if place is not None:
                return {"lat": place["lat"], "lon": place["lon"], "label": place.get("label") or query["near"]}
        center = self._center(query)
        if center["lat"] is None and query["near"] is not None and places:
            place = self._place(query["near"], places, loose=True)
            if place is not None:
                return {"lat": place["lat"], "lon": place["lon"], "label": place.get("label") or query["near"]}
        return center

    @staticmethod
    def _from_dataset(dataset, center):
        if center["lat"] is None:
            # Unknown place: distances can't be measured, so nothing is "nearby".
            return []
        found = []
        for item in dataset["listings"]:
            item = dict(item)
            item["distance_mi"] = (
                round(distance_mi(center["lat"], center["lon"], item["lat"], item["lon"]), 4)
                if item.get("lat") is not None
                else None
            )
            if item["distance_mi"] is not None:
                found.append(item)
        found.sort(key=lambda i: (i["distance_mi"], i["rent_monthly"], i["listing_id"]))
        return found

    def _center(self, query):
        if query["near"] is None:
            return {
                "lat": query["lat"],
                "lon": query["lon"],
                "label": f"{query['lat']:.6f}, {query['lon']:.6f}",
            }
        path = self.root / "geocodes" / (digest(query["near"]) + ".json")
        record = self._read(path)
        if self._fresh(
            record, TTL_SECONDS if record and record.get("matched") else 300
        ):
            return record["center"]
        center = {"lat": None, "lon": None, "label": query["near"]}
        matched = False
        try:
            response = self.client.get(
                CENSUS_URL,
                params={
                    "address": query["near"],
                    "benchmark": "Public_AR_Current",
                    "format": "json",
                },
                timeout=5.0,
            )
            response.raise_for_status()
            matched_address = response.json()["result"]["addressMatches"][0]
            lat, lon = (
                number(matched_address["coordinates"]["y"]),
                number(matched_address["coordinates"]["x"]),
            )
            if (
                lat is not None
                and lon is not None
                and -90 <= lat <= 90
                and -180 <= lon <= 180
            ):
                center = {
                    "lat": lat,
                    "lon": lon,
                    "label": text(matched_address.get("matchedAddress"))
                    or query["near"],
                }
                matched = True
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            pass
        atomic_json(
            path, {"created_at": self.clock(), "center": center, "matched": matched}
        )
        return center

    def _rentcast(self, query, center):
        params = {
            "status": "Active",
            "radius": query["radius_mi"],
            "limit": query["limit"],
        }
        if center["lat"] is None:
            params["address"] = query["near"]
        else:
            params.update(latitude=center["lat"], longitude=center["lon"])
        if query["bedrooms"] is not None:
            params["bedrooms"] = query["bedrooms"].replace("+", ":*")
        if query["min_rent"] is not None or query["max_rent"] is not None:

            def value(v):
                return "*" if v is None else format(v, ".12g")

            params["price"] = value(query["min_rent"]) + ":" + value(query["max_rent"])
        try:
            response = self.client.get(
                RENTCAST_URL,
                params=params,
                headers={"X-Api-Key": self._key, "Accept": "application/json"},
            )
            if response.status_code == 429:
                raise InputError("RentCast request limit reached; try again later", 503)
            if response.status_code in (401, 403):
                raise InputError(
                    "RentCast authentication failed; check RENTCAST_API_KEY", 502
                )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("Expected listing array")
        except InputError:
            raise
        except httpx.TimeoutException as exc:
            raise InputError("RentCast timed out", 504) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise InputError(
                "RentCast returned an invalid or unsuccessful response", 502
            ) from exc
        return [
            listing
            for raw in rows
            if (listing := normalize_listing(raw, center)) is not None
        ]

    def _samples(self, query, center):
        records = []
        seed = digest(
            {"near": query["near"], "lat": query["lat"], "lon": query["lon"]}
        )[:12]
        for i in range(15):
            beds = i % 5
            baths = 1 if beds < 3 else 2
            lat = lon = distance = None
            if center["lat"] is not None:
                angle = i * math.pi * (3 - math.sqrt(5))
                radius = query["radius_mi"] * 0.85 * math.sqrt((i + 0.5) / 15)
                # Spherical destination, including poles and the date line.
                a, b = math.radians(center["lat"]), math.radians(center["lon"])
                delta = radius / 3958.7613
                lat = math.degrees(
                    math.asin(
                        math.sin(a) * math.cos(delta)
                        + math.cos(a) * math.sin(delta) * math.cos(angle)
                    )
                )
                lon = (
                    math.degrees(
                        b
                        + math.atan2(
                            math.sin(angle) * math.sin(delta) * math.cos(a),
                            math.cos(delta) - math.sin(a) * math.sin(math.radians(lat)),
                        )
                    )
                    + 180
                ) % 360 - 180
                distance = round(distance_mi(center["lat"], center["lon"], lat, lon), 4)
            address = f"FICTIONAL sample {i + 1:02d} near {center['label']}"
            records.append(
                {
                    "listing_id": f"sample_{seed}_{i + 1:02d}",
                    "source": "sample",
                    "sample": True,
                    "title": f"Fictional sample · {'Studio' if beds == 0 else str(beds) + ' bd'} · {baths} ba apartment",
                    "address": {
                        "line1": f"FICTIONAL {100 + i} Demo Lane",
                        "line2": "",
                        "city": "Fictional sample",
                        "state": "",
                        "zip": "",
                        "formatted": address,
                    },
                    "lat": lat,
                    "lon": lon,
                    "distance_mi": distance,
                    "rent_monthly": 950 + beds * 450 + (i // 5) * 125,
                    "currency": "USD",
                    "bedrooms": beds,
                    "bathrooms": baths,
                    "square_feet": None
                    if i % 3 == 0
                    else 450 + 250 * beds + 100 * (baths - 1),
                    "property_type": "Apartment",
                    "listed_date": None,
                    "days_on_market": None,
                    "contact": None,
                    "links": urls(address),
                    "photos": [],
                }
            )
        return records

    def _remember(self, listings):
        for listing in listings:
            atomic_json(
                self.root / "records" / (listing["listing_id"] + ".json"), listing
            )

    def search(self, params):
        query = params.normalized()
        dataset = self._load_dataset()
        source = "rentcast" if self._key else "dataset" if dataset else "sample"
        identity = {"version": 1, "source": source, "query": query}
        if dataset:
            identity["dataset"] = dataset["version"]
        key = digest(identity)
        with self._search_lock:
            path = self.root / "cache" / (key + ".json")
            record = self._read(path)
            if (
                self._fresh(record)
                and isinstance(record.get("response"), dict)
                and isinstance(record["response"].get("listings"), list)
            ):
                response = record["response"]
                self._remember(response["listings"])
                return dict(response, cached=True)
            center = self._located(query, dataset["places"] if dataset else [])
            if self._key:
                listings = self._rentcast(query, center)
            elif dataset:
                listings = self._from_dataset(dataset, center)
            else:
                listings = self._samples(query, center)

            def selected(item):
                if (
                    query["min_rent"] is not None
                    and item["rent_monthly"] < query["min_rent"]
                ):
                    return False
                if (
                    query["max_rent"] is not None
                    and item["rent_monthly"] > query["max_rent"]
                ):
                    return False
                if query["bedrooms"] is not None:
                    minimum = int(query["bedrooms"].rstrip("+"))
                    if (
                        item["bedrooms"] < minimum
                        if query["bedrooms"].endswith("+")
                        else item["bedrooms"] != minimum
                    ):
                        return False
                return (
                    item["distance_mi"] is None
                    or item["distance_mi"] <= query["radius_mi"] + 0.0001
                )

            unique = {item["listing_id"]: item for item in listings if selected(item)}
            response = {
                "source": source,
                "cached": False,
                "center": center,
                "listings": list(unique.values())[: query["limit"]],
            }
            if source == "dataset":
                response["dataset"] = dataset["about"]
            self._remember(response["listings"])
            atomic_json(
                path, {"created_at": self.clock(), "query": query, "response": response}
            )
            return response

    def detail(self, identifier):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier):
            raise InputError("Unknown listing", 404)
        path = self.root / "records" / (identifier + ".json")
        record = self._read(path)
        if record is None or path.is_symlink():
            raise InputError("Unknown listing", 404)
        return record

    def layout(self, identifier):
        listing = self.detail(identifier)
        with self._store_lock:
            path = self.root / "layouts" / (identifier + ".json")
            if path.exists():
                saved = self._read(path)
                if saved is None:
                    raise InputError(
                        "Saved room layout could not be read; delete it to regenerate",
                        500,
                    )
                return saved
            try:
                return estimated_layout(listing)
            except ValueError as exc:
                raise InputError(
                    f"Cannot estimate a valid room plan for these listing dimensions: {exc}"
                ) from exc

    def save_layout(self, identifier, layout):
        self.detail(identifier)
        if layout.listing_id != identifier:
            raise InputError("Layout listing_id must match the URL")
        if layout.estimated or layout.source != "user_edited":
            raise InputError(
                "Saved layouts must have estimated=false and source=user_edited"
            )
        payload = layout.model_dump(by_alias=True)
        with self._store_lock:
            atomic_json(self.root / "layouts" / (identifier + ".json"), payload)
        return payload

    def reset_layout(self, identifier):
        self.detail(identifier)
        with self._store_lock:
            (self.root / "layouts" / (identifier + ".json")).unlink(missing_ok=True)
            return self.layout(identifier)

    def furniture(self, identifier):
        self.detail(identifier)
        with self.assets.lock, self._store_lock:
            path = self.root / "furniture" / (identifier + ".json")
            saved = self._read(path)
            if path.exists() and saved is None:
                raise InputError("Saved furniture could not be read", 500)
            saved = saved or {
                "schema": "housingar.room_furniture.v1",
                "listing_id": identifier,
                "placements": [],
                "updated": None,
            }
            valid = []
            for placement in saved["placements"]:
                try:
                    self.assets.item(placement["item_id"])
                    valid.append(placement)
                except InputError as exc:
                    if exc.status_code != 404:
                        raise
            if saved["updated"] is None or len(valid) != len(saved["placements"]):
                saved["placements"] = valid
                saved["updated"] = self._updated()
                atomic_json(path, saved)
            return saved

    @staticmethod
    def _updated():
        return (
            datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    def save_furniture(self, identifier, furniture):
        self.detail(identifier)
        if furniture.listing_id != identifier:
            raise InputError("Furniture listing_id must match the URL")
        with self.assets.lock, self._store_lock:
            for placement in furniture.placements:
                self.assets.item(placement.item_id)
            payload = furniture.model_dump(by_alias=True)
            payload["updated"] = self._updated()
            atomic_json(self.root / "furniture" / (identifier + ".json"), payload)
            return payload
