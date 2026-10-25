# Listings, estimated rooms and furnishing v1

The Mac service supplies housing data and room geometry. The separately built website renders the rooms and loads existing My stuff GLBs; Android and website source are not changed by this feature. All API and asset URLs are same-origin when the built site is served by the Mac.

## Rental search

`GET /v1/listings?near=<US street address>` **or** `GET /v1/listings?lat=30.2672&lon=-97.7431`.

| Parameter | Type / default | Validation |
| --- | --- | --- |
| `near` | address string | 1–500 characters, nonblank; mutually exclusive with coordinates |
| `lat`, `lon` | numbers | both required together; latitude −90…90, longitude −180…180 |
| `radius_mi` | number / `5` | greater than zero, at most 25 |
| `min_rent`, `max_rent` | USD per month / omitted | nonnegative, at most 10,000,000; minimum ≤ maximum |
| `bedrooms` | string / omitted | exact integer `0`–`99` or a minimum such as `3+`; `0` means studio |
| `limit` | integer / `40` | 1–100 |

Use URLSearchParams or encode `+` as `%2B`, e.g. `bedrooms=3%2B`. Unknown parameters and malformed/nonfinite values return 422.

```json
{
  "source": "rentcast",
  "cached": false,
  "center": {"lat": 30.2672, "lon": -97.7431, "label": "30.267200, -97.743100"},
  "listings": []
}
```

The key is read at service startup; restart after setting or changing it. With `RENTCAST_API_KEY` set, search calls **only** `GET https://api.rentcast.io/v1/listings/rental/long-term`, authenticated with `X-Api-Key`. It requests Active listings, radius, limit, rent range and bedroom range. `3+` maps to `3:*`; an open rent boundary uses `*`. Local filtering also enforces the requested criteria. One provider request is made per cache miss, without retries, pagination or scraping. [RentCast endpoint](https://developers.rentcast.io/reference/rental-listings-long-term), [range syntax](https://developers.rentcast.io/reference/search-queries).

Successful query responses, including empty results, are cached on disk for **24 hours**. Normalization trims/case-folds addresses, collapses whitespace and spaces around commas, rounds coordinate queries to six decimal places, and includes source, filters and limit. Concurrent searches share the cache-fill lock. Cache hits set `cached:true`, survive restart and make no RentCast or geocoder call. Changing between sample and RentCast mode does not reuse the other source's query cache. Provider failures are not cached or disguised as samples: authentication/upstream failures return 502, quota errors 503, timeouts 504.

Address search uses the public US Census `locations/onelineaddress` geocoder with `Public_AR_Current` and JSON output. Successful geocodes are cached for 24 hours; failures for five minutes. If lookup fails, RentCast receives the address plus radius, `center.lat/lon` are null, and every returned `distance_mi` is null. No extra provider call is spent obtaining coordinates. [Census API](https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html).

Without a key, the response has `source:"sample"`: **15 deterministic, explicitly fictional listings before filtering/limit**, all with `sample:true`, fictional titles/addresses, no contacts or photos. With a known center their positions lie inside the radius. Without a usable center during offline address lookup, sample coordinates and distances are null rather than an invented location. Coordinate search works entirely offline.

### Listing record and detail

`GET /v1/listings/{listing_id}` returns a single record from the persistent store, with no new provider request; unknown IDs return 404. IDs are opaque safe strings, derived from the provider ID for stable reuse. Every returned listing is retained independently of query-cache expiry. User room/furniture files survive future listing refreshes. `distance_mi` is straight-line haversine distance, not driving distance. Distance on the detail record refers to the most recent search that returned that listing.

```json
{
  "listing_id": "rc_opaque_hash",
  "source": "rentcast",
  "sample": false,
  "title": "2 bd · 1 ba apartment",
  "address": {
    "line1": "100 Fictional Example Ave", "line2": "Apt 3",
    "city": "Austin", "state": "TX", "zip": "78701",
    "formatted": "100 Fictional Example Ave, Apt 3, Austin, TX 78701"
  },
  "lat": 30.2672, "lon": -97.7431, "distance_mi": 0,
  "rent_monthly": 2200, "currency": "USD",
  "bedrooms": 2, "bathrooms": 1, "square_feet": 950,
  "property_type": "Apartment", "listed_date": "2026-10-20T00:00:00.000Z",
  "days_on_market": 5,
  "contact": {"name": "Fictional Agent", "phone": "5125550100", "email": "agent@example.invalid", "office": "Fictional Rental Office"},
  "links": {
    "search_url": "https://www.google.com/search?q=100+Fictional+Example+Ave+for+rent",
    "maps_url": "https://www.google.com/maps/search/?api=1&query=100+Fictional+Example+Ave",
    "listing_url": null
  },
  "photos": []
}
```

The example above is fictional documentation data. Address components missing upstream become empty strings. Coordinates, distance, square feet, listed date and days on market may be null. A contact is null if no agent/office information exists; its individual fields may also be null. Rent, bedroom count and bathroom count are required for a usable provider record; inactive or incomplete records are skipped. Counts are not invented. RentCast fields map from `price`, `squareFootage`, `listedDate`, `daysOnMarket`, `listingAgent` and `listingOffice`. [Provider schema](https://developers.rentcast.io/reference/property-listings-schema).

Search and maps links are address searches, not claimed listing sources. `listing_url` is populated only from an explicit provider-supplied HTTP(S) listing URL (`listingUrl`, `listingURL`, or `url`); an agent/office website is not substituted. `photos` is empty; the service does not fetch listing websites or fabricate interior photos.

## Estimated 3D room plan

- `GET /v1/listings/{id}/layout`: saved edited plan, otherwise a deterministic estimate from current listing attributes.
- `PUT /v1/listings/{id}/layout`: validate and save the whole plan; require `estimated:false`, `source:"user_edited"` and matching listing ID.
- `DELETE /v1/listings/{id}/layout`: remove the edit and return a fresh estimate. Furniture placements are retained.

```json
{
  "schema": "housingar.room.v1",
  "listing_id": "sample_example",
  "estimated": true, "source": "estimated", "units": "meters",
  "ceiling_height_m": 2.44, "total_area_m2": 24,
  "rooms": [
    {"room_id":"bath_1", "name":"Bathroom", "kind":"bathroom", "polygon":[[0,0],[0,1.2],[4,1.2],[4,0]]},
    {"room_id":"living", "name":"Main room", "kind":"living", "polygon":[[0,1.2],[0,6],[4,6],[4,1.2]]}
  ],
  "openings": [
    {"opening_id":"door_1", "kind":"door", "room_ids":["bath_1","living"], "a":[1.5,1.2], "b":[2.4,1.2], "height_m":2.03, "sill_m":0},
    {"opening_id":"door_2", "kind":"door", "room_ids":["living"], "a":[0.4,6], "b":[1.3,6], "height_m":2.03, "sill_m":0},
    {"opening_id":"window_1", "kind":"window", "room_ids":["living"], "a":[2.4,6], "b":[3.6,6], "height_m":1.1, "sill_m":0.9}
  ]
}
```

**Coordinates:** metres, `[x,z]` on the plan: +X right, +Z down the plan/toward the viewer in 3D; +Y up, floor y=0. Polygons follow `[top-left, bottom-left, bottom-right, top-right]`. They have negative signed area in numeric XZ coordinates and an upward +Y normal, hence counter-clockwise when viewed from above. PUT accepts either cyclic winding and returns this canonical ordering; unordered/bow-tie corners are rejected.

Room kinds: `living|kitchen|bedroom|bathroom|hall|closet|other`. IDs are unique, 1–64 ASCII letters/digits/underscores/hyphens. Names are trimmed and contain 1–40 characters. At most 20 rooms and 60 openings; each room is a four-corner, axis-aligned rectangle with sides 0.6–20 m. The union must tile one outer rectangle, without overlaps or gaps. Plan coordinates must stay within ±10,000 m. `total_area_m2` must agree with polygon area within 3%; the saved response uses the exact computed area.

An opening's segment must be at least 0.1 m long, axis-aligned and wholly on its referenced wall. Two room IDs mean a shared interior wall; one means an exterior wall. Windows reference one room on an exterior wall. Doors begin at floor level and connect adjacent rooms, or form an exterior entry. Opening height and sill must fit under the ceiling. Every room must be reachable from an exterior door; living rooms and bedrooms need at least one exterior window. These invariants apply to edited plans too. Unknown fields, invalid references and invalid geometry return 422 without overwriting a saved plan.

**Generation:** `1 ft² = 0.09290304 m²`; supplied area is matched exactly. Missing square footage uses `450 + 250×bedrooms + 100×(ceil(bathrooms)−1)` ft², with at least one bathroom. Studios have one main living room and one bathroom. Other apartments have separate living/kitchen rooms, one room per bedroom, full rectangular bathroom estimates for rounded-up fractional bathroom counts, and a hall. Estimated bedrooms are 9–12 m² (within the requested 9–14 range), bathrooms 4.5–4.8 m² (within 3.5–6). Dimensions and openings are derived deterministically; there is no claim that these are the rental's actual walls. If area/counts cannot produce a valid constrained estimate, GET returns 422 and the listing can still receive a valid user-edited plan.

## Furniture / My stuff

`GET` and `PUT /v1/listings/{id}/furniture` use:

```json
{
  "schema":"housingar.room_furniture.v1",
  "listing_id":"sample_example",
  "placements":[
    {"placement_id":"chair_01", "item_id":"existing_library_item_id", "name":"My chair", "position":[1,0,2], "yaw_deg":45}
  ],
  "updated":"2026-10-25T12:00:00.000000Z"
}
```

Positions are the GLB's **bottom-centre** in room coordinates `[x,y,z]`, in metres. Yaw is right-handed rotation about +Y, in degrees. Geometry is not rescaled; use `/v1/library` dimensions and its existing `mesh_url` / `thumbnail_url`. Multiple copies of an item are allowed, but placement IDs must be unique. At most 200 placements; names are nonblank 1–80 character strings, positions have three finite numbers and yaw is finite. The service validates item existence; a missing listing or library item returns 404. Furniture `listing_id` must match the URL. Unknown fields and malformed values return 422.

`updated` is server-owned UTC ISO 8601; omit it on PUT or echo the old value. A first GET creates a stable empty document with an update time. GET/PUT survive restart. If a library item is subsequently deleted, the next furniture GET removes its stale placements and updates the timestamp. Layout reset does not remove furniture. Collision, wall containment and furniture fit are left to the room editor; the service preserves finite poses, including elevated positions for stacking.

## Website hosting and development

The service reads **`housing-ar/web/dist`**, without building or editing it. It becomes available as soon as `index.html` exists, including after the service starts. Files are served at `/`; unknown non-API paths return `index.html` for browser routing. Unknown `/v1` paths return API 404s, never HTML. Path traversal and symlinks escaping the build directory are rejected. API, GLB and thumbnail routes take precedence and retain their existing paths. HTML uses `Cache-Control: no-cache`.

CORS permits `http://localhost:5173` and `http://127.0.0.1:5173` only for `/v1`: GET/POST/PUT/PATCH/DELETE/OPTIONS and Content-Type. No browser credential/cookie flow is required. During Vite development, call `http://localhost:8000/v1/...`; in the served build use relative URLs.

```bash
cd segmentation-service
python -m pip install -r requirements.txt
# Set RENTCAST_API_KEY in the service's environment for real listings.
python -m housing_segmentation --backend sam2 --host 0.0.0.0 --port 8000
```

Mac: `http://localhost:8000/`. Pixel over USB: run `adb reverse tcp:8000 tcp:8000`, then open `http://127.0.0.1:8000/`. Over Wi-Fi: open `http://<Mac-LAN-IP>:8000/`; get the current address with `ipconfig getifaddr en0`. LAN mode is an unauthenticated local demo: use a trusted network. The provider key stays on the Mac and is never included in responses or browser assets.

## Disk state and verification

`data/listings/cache/` holds 24-hour search responses; `geocodes/` holds Census results; `records/` holds returned listings; `layouts/` holds edited room plans; `furniture/` holds saved placements. Writes use atomic replacement. Keep one service worker, as for existing scan/capture jobs.

```bash
.venv/bin/pytest -q tests/test_listings.py
# Local HTTP/LAN + restart check, read-only copy of a current library item:
.venv/bin/python scripts/check_listings.py --output data/new-listings-check
```

The check uses an isolated demo store and stops its temporary server. If a key is set, it additionally makes **one** real RentCast request with coordinates and limit 3, records the response and status without credentials, and performs no additional provider requests. Otherwise the real request is explicitly skipped. See [measured results](listings_v1_results.md).
