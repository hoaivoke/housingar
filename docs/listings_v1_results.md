# Listings / rooms v1: measured results

Measured **25 October 2026** on the project's Mac. Implementation is confined to the Mac service, its tests/scripts and documentation; `android/` and `web/` were not edited.

## Verified behaviour

- Rental search uses RentCast's documented long-term rental endpoint, with Active status, radius, rent and bedroom filters. Provider-shaped fixtures use fictional addresses and contacts; they test mapping and filtering without spending request quota.
- A normalized query is cached on disk for 24 hours, including empty successful results. Cache expiry, restart reuse and five concurrent identical searches were tested; the concurrent case makes exactly **one** provider request.
- No key means **15 explicitly fictional samples** before filtering/limit. Provider errors with a key remain errors, not fictional replacements.
- Census geocoding was exercised against the real public endpoint: `4600 Silver Hill Rd, Washington, DC 20233` resolved to **38.84505589808, −76.92836638093**. Mocked no-match, timeout, malformed JSON and HTTP failures verify address forwarding to RentCast with null distances.
- Studio through four-bedroom plans were checked with/without square footage and with bathroom counts 1, 1.5 and 2: **30 deterministic variants**. Independent grid checks verify no overlaps/gaps, exact area, bedroom/bathroom areas, upward winding, connected doors, windows and wall-aligned openings. Invalid edits leave the old layout intact.
- Furniture PUT/GET validates library references and supports **200 distinct placements**, including copies of one item. Round trip, restart persistence, finite numeric inputs, duplicate IDs, missing items and pruning after library deletion were tested.
- A temporary built-site fixture tested HTML/JS serving, browser-route fallback, late creation of the build, path/symlink containment and API error/redirect preservation. CORS accepts the two specified Vite origins only for `/v1`.

## Real HTTP / LAN / My stuff check

`scripts/check_listings.py` launched an isolated floodfill service at **0.0.0.0:18001**, then stopped it after the checks. It copied an existing confirmed My stuff item into its temporary store; the original item was not changed.

| Check | Actual result |
| --- | --- |
| First coordinate sample search | **15 results**, `source:sample`, `cached:false`, **7.42 ms** localhost wall time |
| Repeated search | `cached:true` |
| Generated two-bedroom example | **6 rooms**, relative area error **1.61×10⁻¹⁶** |
| Existing scanned asset | Black chair, item `02a5343b594c4ea8825027f160b1e5e7`, existing `depth_fusion` library copy |
| GLB served through original library URL | **4,242,580 bytes**, identical to the copied source |
| Thumbnail served | HTTP 200 |
| Placement | `[1,0,2]` m, yaw 45°; GET matches PUT |
| Restart | Cache hit, edited room and furniture all preserved |
| LAN access | `/health` HTTP **200** via then-current `172.31.74.161:18001` |
| Vite preflight | Allowed `http://localhost:5173` |

This tests furnishing with an existing user-scanned library asset; it does not replace that asset with the newer photo reconstruction from the prior task. The sample timing is a local housing-API measurement, not model inference, RentCast latency or 3D rendering performance.

Evidence: `segmentation-service/data/listings-http-verification/result.json`, its temporary store and `server.log`. The verification script also refuses a busy port, avoiding accidental writes to another worker's service.

## Real RentCast request

**Skipped: `RENTCAST_API_KEY` is not set. Zero real RentCast requests were made.** No live rental availability or provider-account access is claimed. The current service returns marked fictional samples. `check_listings.py` conditionally makes exactly one real coordinate request with `limit=3` when the key is present, and records response/status without credentials.

## Main service and website

The idle existing service was restarted with its original command: SAM 2.1 Tiny, automatic device selection, **0.0.0.0:8000**. Health reports the existing MPS backend; no new GPU performance measurement was made. The live main service returns listings and room plans with HTTP 200, and **all 10 existing My stuff records remain present**. API and asset URLs remain same-origin. Final live checks also confirm LAN health HTTP 200, all six unknown-API method checks return 404, and an unsupported method on an existing route returns 405. Evidence: `data/listings-main-service.json`.

**`web/dist` does not exist yet**, so `/` correctly reports that the website has not been built. The separate website worker can create it without another service restart: build lookup happens on each request. Actual frontend rendering was not tested; read-only static serving was verified with a temporary build. No files were written in `web/`.

## Tests

**289 tests passed in 221.11 s**, including **102 new listing/static/furniture tests**. The standalone feature suite passed in **2.88 s**, without external requests or model downloads. The full log is `segmentation-service/data/listings-all-tests-final.txt`. Formatting, lint and bytecode compilation checks also pass. There are 50 upstream Starlette/scikit-image deprecation warnings and no failures.

```bash
cd segmentation-service
.venv/bin/pytest -q
.venv/bin/python scripts/check_listings.py --output data/new-listings-verification
```

The final API and geometry contract is [listings_v1.md](listings_v1.md).
