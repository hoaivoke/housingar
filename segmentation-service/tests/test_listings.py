import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from housing_segmentation.app import create_app
from housing_segmentation.config import Settings
from housing_segmentation.listings.rooms import estimated_layout
from housing_segmentation.listings.service import TTL_SECONDS, ListingQuery, Listings

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/rentcast_rentals.json").read_text()
)
CENTER = {"lat": 30.2672, "lon": -97.7431}


def upstream(request):
    if request.url.host == "geocoding.geo.census.gov":
        return httpx.Response(
            200,
            json={
                "result": {
                    "addressMatches": [
                        {
                            "matchedAddress": "100 FICTIONAL EXAMPLE AVE, AUSTIN, TX, 78701",
                            "coordinates": {"x": CENTER["lon"], "y": CENTER["lat"]},
                        }
                    ]
                }
            },
        )
    assert request.url.host == "api.rentcast.io"
    assert request.url.path == "/v1/listings/rental/long-term"
    assert request.headers["X-Api-Key"] == "test-key"
    return httpx.Response(200, json=FIXTURE)


@pytest.fixture
def api(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(upstream)) as remote:
        app = create_app(
            Settings(data_dir=tmp_path / "data"),
            listings_client=remote,
            rentcast_api_key="test-key",
            web_dist=tmp_path / "dist",
        )
        with TestClient(app) as client:
            yield client, app


def search(client, **params):
    r = client.get("/v1/listings", params=dict(CENTER, **params))
    assert r.status_code == 200, r.text
    return r.json()


def edited(plan):
    plan = deepcopy(plan)
    plan.update(estimated=False, source="user_edited")
    return plan


def test_real_shape_mapping_detail_and_no_fabricated_links(api):
    client, app = api
    response = search(client)
    assert response["source"] == "rentcast" and not response["cached"]
    assert len(response["listings"]) == 5
    for i, listing in enumerate(response["listings"]):
        assert listing["source"] == "rentcast" and listing["sample"] is False
        assert listing["bedrooms"] == i and listing["currency"] == "USD"
        assert listing["distance_mi"] >= 0 and listing["photos"] == []
        assert listing["address"]["line2"] == f"Apt {i + 1}"
        assert (
            listing["listed_date"] == "2026-10-20T00:00:00.000Z"
            and listing["days_on_market"] == 5
        )
        assert "for+rent" in listing["links"]["search_url"]
        assert "maps/search/" in listing["links"]["maps_url"]
        assert listing["links"]["listing_url"] == (
            "https://example.invalid/provided-property" if i == 2 else None
        )
        assert client.get("/v1/listings/" + listing["listing_id"]).json() == listing
    assert client.get("/v1/listings/unknown").status_code == 404
    assert client.get("/v1/listings/unknown/layout").status_code == 404
    assert client.get("/v1/listings/unknown/furniture").status_code == 404


def test_filters_are_sent_using_rentcast_ranges_and_enforced_locally(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return upstream(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as remote:
        with TestClient(
            create_app(
                Settings(data_dir=tmp_path),
                listings_client=remote,
                rentcast_api_key="test-key",
            )
        ) as client:
            result = search(
                client,
                min_rent=2200,
                max_rent=2900,
                bedrooms="3+",
                limit=2,
                radius_mi=2,
            )
            assert [item["bedrooms"] for item in result["listings"]] == [3]
            q = calls[-1].url.params
            assert q["bedrooms"] == "3:*" and q["price"] == "2200:2900"
            assert (
                q["radius"] == "2.0" and q["limit"] == "2" and q["status"] == "Active"
            )
            assert q["latitude"] == str(CENTER["lat"]) and "address" not in q
            assert len(search(client, bedrooms="0")["listings"]) == 1
            search(client, min_rent=2000)
            assert calls[-1].url.params["price"] == "2000:*"
            search(client, max_rent=2000)
            assert calls[-1].url.params["price"] == "*:2000"


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"near": " "},
        {"lat": 1},
        {"lon": 2},
        {"near": "Austin", "lat": 1, "lon": 2},
        {"lat": 91, "lon": 0},
        {"lat": 0, "lon": -181},
        {"lat": "NaN", "lon": 0},
        {"lat": 0, "lon": 0, "radius_mi": 26},
        {"lat": 0, "lon": 0, "radius_mi": 0},
        {"lat": 0, "lon": 0, "limit": 101},
        {"lat": 0, "lon": 0, "limit": 0},
        {"lat": 0, "lon": 0, "bedrooms": "-1"},
        {"lat": 0, "lon": 0, "bedrooms": "three"},
        {"lat": 0, "lon": 0, "min_rent": 2000, "max_rent": 1000},
        {"lat": 0, "lon": 0, "min_rent": -1},
        {"lat": 0, "lon": 0, "unknown": 1},
    ],
)
def test_invalid_queries_are_422(api, params):
    assert api[0].get("/v1/listings", params=params).status_code == 422


def test_normalized_cache_expiry_restart_and_no_extra_geocodes(tmp_path):
    calls = []
    now = [1000000.0]

    def handler(request):
        calls.append(request)
        return upstream(request)

    assets = SimpleNamespace()
    with httpx.Client(transport=httpx.MockTransport(handler)) as remote:
        service = Listings(
            tmp_path, assets, api_key="test-key", client=remote, clock=lambda: now[0]
        )
        a = service.search(
            ListingQuery(near=" 100 Fictional Example Ave,   Austin, TX ")
        )
        b = service.search(
            ListingQuery(
                near="100 FICTIONAL EXAMPLE AVE , Austin , TX", radius_mi=5, limit=40
            )
        )
        assert not a["cached"] and b["cached"] and len(calls) == 2
        # Reconstruct the service: disk cache and listing detail survive.
        service = Listings(
            tmp_path, assets, api_key="test-key", client=remote, clock=lambda: now[0]
        )
        assert service.search(ListingQuery(near="100 Fictional Example Ave,Austin,TX"))[
            "cached"
        ]
        assert len(calls) == 2
        now[0] += TTL_SECONDS - 1
        assert service.search(ListingQuery(near="100 Fictional Example Ave,Austin,TX"))[
            "cached"
        ]
        now[0] += 1
        assert not service.search(
            ListingQuery(near="100 Fictional Example Ave,Austin,TX")
        )["cached"]
        assert len(calls) == 4
        assert service.detail(a["listings"][0]["listing_id"])["source"] == "rentcast"
        assert "test-key" not in "".join(
            p.read_text() for p in (tmp_path / "listings").rglob("*.json")
        )


def test_same_query_concurrent_requests_spend_one_provider_call(tmp_path):
    calls = []
    gate = threading.Lock()

    def handler(request):
        with gate:
            calls.append(request)
        return upstream(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as remote:
        service = Listings(tmp_path, None, api_key="test-key", client=remote)
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(
                pool.map(lambda _: service.search(ListingQuery(**CENTER)), range(5))
            )
        assert len(calls) == 1 and sum(not r["cached"] for r in results) == 1


@pytest.mark.parametrize("failure", ["empty", "timeout", "bad_json", "http"])
def test_geocode_failure_passes_address_and_leaves_distance_null(tmp_path, failure):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "geocoding.geo.census.gov":
            if failure == "timeout":
                raise httpx.ReadTimeout("timeout", request=request)
            if failure == "bad_json":
                return httpx.Response(200, text="broken")
            if failure == "http":
                return httpx.Response(500)
            return httpx.Response(200, json={"result": {"addressMatches": []}})
        return upstream(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as remote:
        with TestClient(
            create_app(
                Settings(data_dir=tmp_path),
                listings_client=remote,
                rentcast_api_key="test-key",
            )
        ) as client:
            r = client.get(
                "/v1/listings", params={"near": "100 Fictional Example Ave, Austin, TX"}
            ).json()
            assert r["source"] == "rentcast" and r["center"]["lat"] is None
            assert all(item["distance_mi"] is None for item in r["listings"])
            assert (
                "address" in calls[-1].url.params
                and "latitude" not in calls[-1].url.params
            )
            assert client.get(
                "/v1/listings", params={"near": "100 Fictional Example Ave, Austin, TX"}
            ).json()["cached"]
            assert len(calls) == 2


@pytest.mark.parametrize(
    "code,expected", [(401, 502), (403, 502), (429, 503), (500, 502)]
)
def test_provider_errors_do_not_turn_into_samples_or_get_cached(
    tmp_path, code, expected
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(code, json={"error": "provider failure"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as remote:
        with TestClient(
            create_app(
                Settings(data_dir=tmp_path),
                listings_client=remote,
                rentcast_api_key="test-key",
            )
        ) as client:
            assert client.get("/v1/listings", params=CENTER).status_code == expected
            assert client.get("/v1/listings", params=CENTER).status_code == expected
            assert len(calls) == 2
            assert not list((tmp_path / "listings/cache").glob("*.json"))


def test_malformed_provider_data_and_timeout(tmp_path):
    for index, handler in enumerate(
        [
            lambda r: httpx.Response(200, json={}),
            lambda r: httpx.Response(200, text="not json"),
            lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("timeout", request=r)),
        ]
    ):
        with httpx.Client(transport=httpx.MockTransport(handler)) as remote:
            with TestClient(
                create_app(
                    Settings(data_dir=tmp_path / str(index)),
                    listings_client=remote,
                    rentcast_api_key="test-key",
                )
            ) as client:
                assert client.get("/v1/listings", params=CENTER).status_code == (
                    504 if index == 2 else 502
                )


def test_samples_offline_filters_cache_and_geocode_failure(tmp_path):
    def offline(request):
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(transport=httpx.MockTransport(offline)) as remote:
        with TestClient(
            create_app(
                Settings(data_dir=tmp_path), listings_client=remote, rentcast_api_key=""
            )
        ) as client:
            result = search(client)
            assert result["source"] == "sample" and len(result["listings"]) == 15
            assert all(
                l["sample"]
                and "FICTIONAL" in l["address"]["formatted"]
                and l["distance_mi"] < 5
                for l in result["listings"]
            )
            assert search(client)["cached"]
            assert all(
                l["bedrooms"] >= 3 for l in search(client, bedrooms="3+")["listings"]
            )
            assert all(
                1800 <= l["rent_monthly"] <= 2600
                for l in search(client, min_rent=1800, max_rent=2600)["listings"]
            )
            assert len(search(client, limit=3)["listings"]) == 3
            unknown = client.get(
                "/v1/listings", params={"near": "Cannot geocode while offline"}
            ).json()
            assert (
                unknown["source"] == "sample"
                and len(unknown["listings"]) == 15
                and unknown["center"]["lat"] is None
            )
            assert all(
                l["lat"] is None and l["distance_mi"] is None
                for l in unknown["listings"]
            )


def assert_plan(plan, beds, expected_area):
    rooms = plan["rooms"]
    rects = []
    assert len([r for r in rooms if r["kind"] == "bedroom"]) == beds
    if beds == 0:
        assert len(rooms) == 2
    else:
        assert {"living", "kitchen"} <= {r["kind"] for r in rooms}
    for r in rooms:
        p = r["polygon"]
        xs = [q[0] for q in p]
        zs = [q[1] for q in p]
        rect = (min(xs), min(zs), max(xs), max(zs))
        rects.append(rect)
        width, depth = rect[2] - rect[0], rect[3] - rect[1]
        assert 0.6 - 1e-6 <= width <= 20 + 1e-6 and 0.6 - 1e-6 <= depth <= 20 + 1e-6
        signed = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(p, p[1:] + p[:1]))
        assert signed < 0  # +Y-up normal, +Z down the plan
        if r["kind"] == "bedroom":
            assert 9 - 1e-6 <= width * depth <= 14 + 1e-6
        if r["kind"] == "bathroom":
            assert 3.5 - 1e-6 <= width * depth <= 6 + 1e-6
    # Independent grid oracle: every cell in the outer apartment has exactly
    # one owning rectangle, including cells adjacent to unequal room widths.
    xs = sorted({v for r in rects for v in (r[0], r[2])})
    zs = sorted({v for r in rects for v in (r[1], r[3])})
    for x0, x1 in zip(xs, xs[1:]):
        for z0, z1 in zip(zs, zs[1:]):
            x, z = (x0 + x1) / 2, (z0 + z1) / 2
            assert sum(a <= x <= c and b <= z <= d for a, b, c, d in rects) == 1
    area = sum((c - a) * (d - b) for a, b, c, d in rects)
    assert abs(area - expected_area) < expected_area * 0.03 and math.isclose(
        area, plan["total_area_m2"]
    )
    byid = {r["room_id"]: rect for r, rect in zip(rooms, rects)}
    graph = {key: set() for key in byid}
    entries = set()
    windows = set()
    for opening in plan["openings"]:
        a, b = opening["a"], opening["b"]
        ids = opening["room_ids"]
        assert len(ids) in (1, 2)
        for key in ids:
            x0, z0, x1, z1 = byid[key]
            assert all(
                x0 - 1e-6 <= p[0] <= x1 + 1e-6 and z0 - 1e-6 <= p[1] <= z1 + 1e-6
                for p in (a, b)
            )
            assert (
                math.isclose(a[0], b[0])
                and any(math.isclose(a[0], x) for x in (x0, x1))
            ) or (
                math.isclose(a[1], b[1])
                and any(math.isclose(a[1], z) for z in (z0, z1))
            )
        if opening["kind"] == "door":
            if len(ids) == 2:
                graph[ids[0]].add(ids[1])
                graph[ids[1]].add(ids[0])
            else:
                entries.add(ids[0])
        else:
            assert len(ids) == 1
            windows.add(ids[0])
        assert opening["height_m"] + opening["sill_m"] <= plan["ceiling_height_m"]
    reached = set(entries)
    while True:
        expanded = reached | {n for key in reached for n in graph[key]}
        if expanded == reached:
            break
        reached = expanded
    assert reached == set(byid)
    assert {
        r["room_id"] for r in rooms if r["kind"] in ("living", "bedroom")
    } <= windows


@pytest.mark.parametrize("beds", range(5))
@pytest.mark.parametrize("baths", [1, 1.5, 2])
@pytest.mark.parametrize("has_area", [False, True])
def test_estimate_properties(beds, baths, has_area):
    sqft = 450 + 250 * beds + 100 * (math.ceil(baths) - 1)
    listing = {
        "listing_id": "test",
        "bedrooms": beds,
        "bathrooms": baths,
        "square_feet": sqft if has_area else None,
    }
    plan = estimated_layout(listing)
    assert plan == estimated_layout(listing)
    assert (
        plan["estimated"]
        and plan["source"] == "estimated"
        and plan["units"] == "meters"
    )
    assert_plan(plan, beds, sqft * 0.09290304)


def test_layout_save_restart_reset(api, tmp_path):
    client, app = api
    listing = search(client)["listings"][2]
    identifier = listing["listing_id"]
    url = f"/v1/listings/{identifier}/layout"
    plan = client.get(url).json()
    changed = edited(plan)
    changed["rooms"][0]["name"] = "My bedroom"
    assert client.put(url, json=changed).json() == changed
    assert client.get(url).json() == changed
    # A separate persistence instance reloads the edited file.
    other = Listings(
        app.state.listings.root.parent,
        app.state.assets,
        api_key="",
        client=app.state.listings.client,
    )
    assert other.layout(identifier) == changed
    reset = client.delete(url)
    assert reset.status_code == 200 and reset.json() == plan
    assert client.get(url).json() == plan
    bad = edited(plan)
    bad["listing_id"] = "other"
    assert client.put(url, json=bad).status_code == 422
    assert client.put(url, json=plan).status_code == 422


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p["rooms"][0].update(name=" "),
        lambda p: p["rooms"][0].update(name="x" * 41),
        lambda p: p["rooms"][0].update(polygon=[[0, 0], [1, 1], [2, 1], [1, 0]]),
        lambda p: p["rooms"][0].update(polygon=[[0, 0], [0, 0.5], [1, 0.5], [1, 0]]),
        lambda p: p["rooms"][0].update(polygon=[[0, 0], [0, 21], [1, 21], [1, 0]]),
        lambda p: p["rooms"][1].update(polygon=p["rooms"][0]["polygon"]),
        lambda p: p["rooms"][1].update(room_id=p["rooms"][0]["room_id"]),
        lambda p: p.update(openings=[]),
        lambda p: p["openings"][0].update(room_ids=["unknown"]),
        lambda p: p["openings"][0].update(room_ids=[p["rooms"][0]["room_id"]] * 2),
        lambda p: p["openings"][0].update(a=[0.1, 0.1], b=[0.8, 0.8]),
        lambda p: p["openings"][0].update(kind="window"),
        lambda p: p["openings"][0].update(height_m=4),
        lambda p: p["openings"][0].update(sill_m=0.1),
        lambda p: p["rooms"].append(deepcopy(p["rooms"][0])),
        lambda p: p.update(rooms=p["rooms"] * 5),
        lambda p: p.update(openings=p["openings"] * 20),
        lambda p: p.update(total_area_m2=1),
        lambda p: p.update(units="feet"),
        lambda p: p.update(estimated=True),
        lambda p: p.update(unknown=1),
    ],
)
def test_layout_validation_failures(api, mutation):
    client, _ = api
    identifier = search(client)["listings"][2]["listing_id"]
    url = f"/v1/listings/{identifier}/layout"
    original = client.get(url).json()
    bad = edited(original)
    mutation(bad)
    response = client.put(url, json=bad)
    assert response.status_code == 422, response.text
    assert client.get(url).json() == original


def seed_library(app, item_id="item_1"):
    folder = app.state.assets.root / "library" / item_id
    folder.mkdir(parents=True)
    (folder / "item.json").write_text(
        json.dumps(
            {
                "item_id": item_id,
                "name": "Chair",
                "dimensions_m": [0.5, 0.8, 0.5],
                "mesh_url": f"/v1/library/{item_id}/object.glb",
                "thumbnail_url": f"/v1/library/{item_id}/thumbnail.jpg",
            }
        )
    )
    (folder / "object.glb").write_bytes(b"GLB test file")
    (folder / "thumbnail.jpg").write_bytes(b"JPEG test file")


def test_furniture_round_trip_restart_and_deleted_items(api):
    client, app = api
    seed_library(app)
    identifier = search(client)["listings"][0]["listing_id"]
    url = f"/v1/listings/{identifier}/furniture"
    empty = client.get(url).json()
    assert empty["placements"] == [] and empty["updated"]
    payload = {
        "schema": "housingar.room_furniture.v1",
        "listing_id": identifier,
        "placements": [
            {
                "placement_id": "placed_1",
                "item_id": "item_1",
                "name": "My chair",
                "position": [1, 0, 2],
                "yaw_deg": 90,
            }
        ],
    }
    saved = client.put(url, json=payload)
    assert saved.status_code == 200, saved.text
    saved = saved.json()
    assert saved["placements"] == payload["placements"] and saved["updated"]
    assert client.get(url).json() == saved
    other = Listings(
        app.state.listings.root.parent,
        app.state.assets,
        api_key="",
        client=app.state.listings.client,
    )
    assert other.furniture(identifier) == saved
    assert client.get("/v1/library/item_1/object.glb").content == b"GLB test file"
    assert client.get("/v1/library/item_1/thumbnail.jpg").content == b"JPEG test file"
    assert client.delete("/v1/library/item_1").status_code == 200
    assert client.get(url).json()["placements"] == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(listing_id="wrong"),
        lambda p: p["placements"][0].update(item_id="missing"),
        lambda p: p["placements"][0].update(position=[1, 2]),
        lambda p: p["placements"][0].update(yaw_deg="NaN"),
        lambda p: p["placements"][0].update(name=" "),
        lambda p: p["placements"][0].update(placement_id="../bad"),
        lambda p: p["placements"].append(deepcopy(p["placements"][0])),
        lambda p: p.update(placements=p["placements"] * 201),
        lambda p: p.update(schema="wrong"),
        lambda p: p.update(unknown=1),
    ],
)
def test_furniture_validation(api, mutation):
    client, app = api
    seed_library(app)
    identifier = search(client)["listings"][0]["listing_id"]
    url = f"/v1/listings/{identifier}/furniture"
    p = {
        "schema": "housingar.room_furniture.v1",
        "listing_id": identifier,
        "placements": [
            {
                "placement_id": "p",
                "item_id": "item_1",
                "name": "Chair",
                "position": [1, 0, 2],
                "yaw_deg": 0,
            }
        ],
    }
    mutation(p)
    assert client.put(url, json=p).status_code in (404, 422)
    assert client.get(url).json()["placements"] == []


def test_static_site_routes_dev_cors_and_build_appearing_later(tmp_path):
    dist = tmp_path / "dist"
    with TestClient(
        create_app(
            Settings(data_dir=tmp_path / "data"), rentcast_api_key="", web_dist=dist
        )
    ) as client:
        assert client.get("/").status_code == 404
        dist.mkdir()
        (dist / "index.html").write_text("<html>Built site</html>")
        (dist / "assets").mkdir()
        (dist / "assets/app.js").write_text('console.log("site")')
        assert "Built site" in client.get("/").text
        assert client.get("/listing/sample/decorate").text == client.get("/").text
        assert client.head("/").status_code == 200 and client.head("/").content == b""
        js = client.get("/assets/app.js")
        assert "javascript" in js.headers["content-type"] and js.text.startswith(
            "console.log"
        )
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/v1/missing").status_code == 404
        assert client.get("/v1").status_code == 404
        outside = tmp_path / "secret.txt"
        outside.write_text("PRIVATE")
        (dist / "outside.txt").symlink_to(outside)
        assert client.get("/outside.txt").status_code == 404
        assert client.get("/%2e%2e/secret.txt").status_code == 404
        for origin in ["http://localhost:5173", "http://127.0.0.1:5173"]:
            r = client.options(
                "/v1/listings/x/furniture",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "PUT",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
            assert (
                r.status_code == 200
                and r.headers["access-control-allow-origin"] == origin
            )
            assert (
                client.get("/v1/library", headers={"Origin": origin}).headers[
                    "access-control-allow-origin"
                ]
                == origin
            )
        assert (
            "access-control-allow-origin"
            not in client.get("/", headers={"Origin": "http://localhost:5173"}).headers
        )
        assert (
            "access-control-allow-origin"
            not in client.get(
                "/v1/library", headers={"Origin": "https://unknown.invalid"}
            ).headers
        )


def test_layout_rejects_a_gap_and_an_interior_single_room_window(api):
    client, _ = api
    identifier = search(client)["listings"][2]["listing_id"]
    url = f"/v1/listings/{identifier}/layout"
    original = client.get(url).json()
    gap = edited(original)
    # Narrow only the first private room, leaving an unowned strip beside it.
    for point in gap["rooms"][0]["polygon"]:
        if point[0] > 0:
            point[0] -= 0.15
    assert client.put(url, json=gap).status_code == 422
    interior = edited(original)
    window = next(
        o
        for o in interior["openings"]
        if o["kind"] == "window" and o["room_ids"] == ["bed_1"]
    )
    depth = max(p[1] for p in interior["rooms"][0]["polygon"])
    window["a"][1] = window["b"][1] = depth
    assert client.put(url, json=interior).status_code == 422
    duplicate = edited(original)
    duplicate["openings"].append(deepcopy(duplicate["openings"][0]))
    assert client.put(url, json=duplicate).status_code == 422


def test_furniture_accepts_200_distinct_copies_of_one_library_item(api):
    client, app = api
    seed_library(app)
    identifier = search(client)["listings"][0]["listing_id"]
    url = f"/v1/listings/{identifier}/furniture"
    payload = {
        "schema": "housingar.room_furniture.v1",
        "listing_id": identifier,
        "placements": [
            {
                "placement_id": f"p_{i}",
                "item_id": "item_1",
                "name": "Chair",
                "position": [1, 0, 2],
                "yaw_deg": i,
            }
            for i in range(200)
        ],
    }
    assert client.put(url, json=payload).status_code == 200
    assert len(client.get(url).json()["placements"]) == 200


def test_bad_cache_is_replaced_and_details_outlive_search_cache(tmp_path):
    calls = []

    def handler(r):
        calls.append(r)
        return upstream(r)

    with httpx.Client(transport=httpx.MockTransport(handler)) as remote:
        service = Listings(tmp_path, None, api_key="test-key", client=remote)
        first = service.search(ListingQuery(**CENTER))
        identifier = first["listings"][0]["listing_id"]
        path = next((tmp_path / "listings/cache").glob("*.json"))
        path.write_text("{broken")
        assert not service.search(ListingQuery(**CENTER))["cached"] and len(calls) == 2
        path.unlink()
        assert service.detail(identifier)["listing_id"] == identifier
        record = tmp_path / "listings/records" / f"{identifier}.json"
        record.unlink()
        assert not service.search(ListingQuery(**CENTER))["cached"]
        record.unlink()
        assert service.search(ListingQuery(**CENTER))["cached"]
        assert service.detail(identifier)["listing_id"] == identifier


def test_provider_records_without_rent_or_inactive_are_skipped(tmp_path):
    rows = deepcopy(FIXTURE)
    rows[0]["status"] = "Inactive"
    rows[1]["price"] = None
    rows[2]["listingUrl"] = "http://[bad"
    rows[3]["latitude"] = None
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=rows))
    ) as remote:
        service = Listings(tmp_path, None, api_key="test-key", client=remote)
        listings = service.search(ListingQuery(**CENTER))["listings"]
        assert [l["bedrooms"] for l in listings] == [2, 3, 4]
        assert listings[0]["links"]["listing_url"] is None
        assert listings[1]["lat"] is None and listings[1]["distance_mi"] is None


@pytest.mark.parametrize("bad_number", [True, "2.44"])
def test_room_and_furniture_json_require_numbers_not_booleans_or_strings(
    api, bad_number
):
    client, app = api
    seed_library(app)
    identifier = search(client)["listings"][0]["listing_id"]
    prefix = f"/v1/listings/{identifier}"
    plan = edited(client.get(prefix + "/layout").json())
    plan["ceiling_height_m"] = bad_number
    assert client.put(prefix + "/layout", json=plan).status_code == 422
    furniture = {
        "schema": "housingar.room_furniture.v1",
        "listing_id": identifier,
        "placements": [
            {
                "placement_id": "p",
                "item_id": "item_1",
                "name": "Chair",
                "position": [bad_number, 0, 2],
                "yaw_deg": 0,
            }
        ],
    }
    assert client.put(prefix + "/furniture", json=furniture).status_code == 422


def test_site_fallback_preserves_api_404_405_and_trailing_slash_redirects(api):
    client, _ = api
    for method in ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]:
        assert client.request(method, "/v1/missing-route").status_code == 404
    assert client.post("/v1/listings").status_code == 405
    assert client.get("/v1/listings/", params=CENTER).status_code == 200
