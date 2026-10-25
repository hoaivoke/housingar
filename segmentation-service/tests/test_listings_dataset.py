"""Local listings dataset (listings_data/*.json): used instead of samples without a RentCast key."""

import json
import math
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from housing_segmentation.__main__ import DEFAULT_DATASET
from housing_segmentation.app import create_app
from housing_segmentation.config import Settings
from housing_segmentation.listings.rooms import RoomLayout, estimated_layout
from housing_segmentation.listings.service import (
    DATASET_SCHEMA,
    ListingQuery,
    Listings,
    dataset_listing_valid,
    distance_mi,
)

VT = {"lat": 37.228306, "lon": -80.423206}


def listing(ident, lat, lon, rent, beds=2, baths=1, per="unit", sqft=None):
    return {
        "listing_id": ident,
        "source": "dataset",
        "sample": False,
        "name": f"Test place {ident}",
        "title": f"Test place {ident}",
        "address": {"line1": f"{ident} Test St", "line2": "", "city": "Blacksburg", "state": "VA", "zip": "24060", "formatted": f"{ident} Test St, Blacksburg, VA 24060"},
        "lat": lat,
        "lon": lon,
        "distance_mi": None,
        "rent_monthly": rent,
        "rent_per": per,
        "currency": "USD",
        "bedrooms": beds,
        "bathrooms": baths,
        "square_feet": sqft,
        "property_type": "Apartment",
        "listed_date": None,
        "days_on_market": None,
        "contact": None,
        "links": {"listing_url": f"https://example.org/{ident}", "search_url": "https://example.org/s", "maps_url": "https://example.org/m"},
        "photos": [],
        "collected": "2026-10-25",
    }


def write_dataset(path, listings, **extra):
    path.write_text(json.dumps({
        "schema": DATASET_SCHEMA,
        "name": "Test listings",
        "source": "Test",
        "source_url": "https://example.org",
        "collected": "2026-10-25",
        "note": "Test data.",
        "places": [
            {"label": "Virginia Tech, Blacksburg, VA 24061", "names": ["24061", "virginia tech"], "zips": ["24061"], **VT},
            {"label": "Christiansburg, VA", "names": ["christiansburg", "christiansburg,va"], "zips": ["24073"], "lat": 37.131163, "lon": -80.407338},
        ],
        "listings": listings,
        **extra,
    }))


ROWS = [
    listing("near_1", 37.2290, -80.4230, 900, beds=1),          # ~0.05 mi from VT
    listing("near_2", 37.2400, -80.4300, 1500, beds=3, per="bedroom"),
    listing("mid_1", 37.1500, -80.4100, 1200, beds=2),          # Christiansburg, ~5.5 mi
    listing("far_1", 37.0500, -80.7800, 800, beds=2),           # Pulaski, ~23 mi
]


def no_census(request):
    assert request.url.host == "geocoding.geo.census.gov", "no other provider may be called"
    return httpx.Response(200, json={"result": {"addressMatches": []}})


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "dataset.json"
    write_dataset(path, ROWS)
    calls = []

    def handler(request):
        calls.append(request)
        return no_census(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as remote:
        yield Listings(tmp_path / "data", None, api_key="", client=remote, dataset=path), path, calls


def test_zip_search_uses_dataset_places_without_network(service):
    svc, _, calls = service
    r = svc.search(ListingQuery(near="24061", radius_mi=10))
    assert r["source"] == "dataset" and not r["cached"]
    assert r["center"]["label"].startswith("Virginia Tech") and r["dataset"]["collected"] == "2026-10-25"
    ids = [l["listing_id"] for l in r["listings"]]
    assert ids == ["near_1", "near_2", "mid_1"]  # nearest first; Pulaski is outside 10 mi
    assert all(l["distance_mi"] <= 10 and not l["sample"] for l in r["listings"])
    assert calls == []
    assert svc.search(ListingQuery(near=" 24061 ", radius_mi=10))["cached"]


def test_filters_and_radius(service):
    svc, _, _ = service
    near = lambda **kw: [l["listing_id"] for l in svc.search(ListingQuery(near="24061", **kw))["listings"]]
    assert near(radius_mi=25) == ["near_1", "near_2", "mid_1", "far_1"]
    assert near(radius_mi=1) == ["near_1", "near_2"]
    assert near(radius_mi=25, max_rent=1000) == ["near_1", "far_1"]
    assert near(radius_mi=25, bedrooms="3+") == ["near_2"]
    assert near(radius_mi=25, limit=2) == ["near_1", "near_2"]
    by_coords = svc.search(ListingQuery(lat=37.131163, lon=-80.407338, radius_mi=2))  # mid_1 is ~1.3 mi away
    assert [l["listing_id"] for l in by_coords["listings"]] == ["mid_1"]


def test_street_address_falls_back_to_zip_and_unknown_places_find_nothing(service):
    svc, _, calls = service
    r = svc.search(ListingQuery(near="999 Nowhere Rd, Christiansburg, VA 24073", radius_mi=2))
    assert len(calls) == 1  # Census tried first, no match
    assert r["center"]["label"] == "Christiansburg, VA"
    assert [l["listing_id"] for l in r["listings"]] == ["mid_1"]
    r = svc.search(ListingQuery(near="Tokyo", radius_mi=25))
    assert r["source"] == "dataset" and r["listings"] == [] and r["center"]["lat"] is None


def test_detail_layout_and_furniture_work_for_dataset_listings(service, tmp_path):
    svc, _, _ = service
    svc.search(ListingQuery(near="24061", radius_mi=10))
    record = svc.detail("near_2")
    assert record["rent_per"] == "bedroom" and record["links"]["listing_url"].endswith("near_2")
    layout = svc.layout("near_2")
    assert layout["estimated"] and sum(r["kind"] == "bedroom" for r in layout["rooms"]) == 3


def test_editing_the_dataset_file_takes_effect_without_restart(service):
    svc, path, _ = service
    assert len(svc.search(ListingQuery(near="24061", radius_mi=25))["listings"]) == 4
    write_dataset(path, ROWS[:1])
    r = svc.search(ListingQuery(near="24061", radius_mi=25))
    assert not r["cached"] and [l["listing_id"] for l in r["listings"]] == ["near_1"]


def test_bad_rows_are_skipped_and_a_broken_file_means_samples(service):
    svc, path, _ = service
    broken = dict(ROWS[1], rent_monthly="lots")
    write_dataset(path, [ROWS[0], broken, dict(ROWS[2], listing_id="../evil")])
    assert [l["listing_id"] for l in svc.search(ListingQuery(near="24061", radius_mi=25))["listings"]] == ["near_1"]
    path.write_text("{not json")
    r = svc.search(ListingQuery(lat=VT["lat"], lon=VT["lon"]))
    assert r["source"] == "sample" and all(l["sample"] for l in r["listings"])
    path.unlink()
    assert svc.search(ListingQuery(lat=VT["lat"], lon=VT["lon"], radius_mi=3))["source"] == "sample"


def test_a_rentcast_key_still_wins(tmp_path):
    path = tmp_path / "dataset.json"
    write_dataset(path, ROWS)
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "api.rentcast.io":
            # The dataset's place gave RentCast coordinates for a bare ZIP code.
            assert request.url.params["latitude"] == str(VT["lat"])
            return httpx.Response(200, json=[])
        return no_census(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as remote:
        svc = Listings(tmp_path / "data", None, api_key="key", client=remote, dataset=path)
        r = svc.search(ListingQuery(near="24061"))
    assert r["source"] == "rentcast" and seen == ["api.rentcast.io"] and "dataset" not in r


def test_http_api_and_default_dataset_flag(tmp_path):
    path = tmp_path / "dataset.json"
    write_dataset(path, ROWS)
    with httpx.Client(transport=httpx.MockTransport(no_census)) as remote:
        app = create_app(Settings(data_dir=tmp_path / "data"), listings_client=remote, rentcast_api_key="",
                         web_dist=tmp_path / "dist", listings_dataset=path)
        with TestClient(app) as client:
            r = client.get("/v1/listings", params={"near": "24061", "radius_mi": 10})
            assert r.status_code == 200 and r.json()["source"] == "dataset"
            ident = r.json()["listings"][0]["listing_id"]
            assert client.get(f"/v1/listings/{ident}").json()["name"] == "Test place near_1"
            assert client.get(f"/v1/listings/{ident}/layout").status_code == 200
    assert DEFAULT_DATASET.name == "nrv_vt_offcampus.json"


# ---------------------------------------------------------------- the shipped dataset

SHIPPED = json.loads(DEFAULT_DATASET.read_text()) if DEFAULT_DATASET.exists() else None


@pytest.mark.skipif(SHIPPED is None, reason="listings_data/nrv_vt_offcampus.json not present")
def test_shipped_dataset_is_valid_and_local():
    assert SHIPPED["schema"] == DATASET_SCHEMA and SHIPPED["collected"]
    rows = SHIPPED["listings"]
    assert len(rows) >= 400
    assert all(dataset_listing_valid(r) for r in rows)
    assert len({r["listing_id"] for r in rows}) == len(rows)
    assert all(r["contact"] is None for r in rows)  # no personal contact details kept
    assert all(r["links"]["listing_url"].startswith("https://offcampus.vt.edu/housing/property/") for r in rows)
    vt = next(p for p in SHIPPED["places"] if "24061" in p["zips"])
    miles = [distance_mi(vt["lat"], vt["lon"], r["lat"], r["lon"]) for r in rows]
    assert max(miles) < 25 and sum(m <= 10 for m in miles) >= 400


@pytest.mark.skipif(SHIPPED is None, reason="listings_data/nrv_vt_offcampus.json not present")
def test_every_shipped_listing_gets_a_valid_floor_plan():
    failures = []
    for row in SHIPPED["listings"]:
        try:
            RoomLayout.model_validate(estimated_layout(row))
        except ValueError as exc:
            failures.append((row["listing_id"], row["bedrooms"], row["bathrooms"], row["square_feet"], str(exc)[:80]))
    # A plan the estimator can't fit gets the website's one-room starter instead.
    assert len(failures) <= len(SHIPPED["listings"]) * 0.05, failures[:10]
