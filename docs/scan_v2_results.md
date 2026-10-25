# Tapped scan v2: measured results

Measured 24 October 2026 on Apple M5, 16 GiB RAM, macOS 27.0.1, Python 3.12.13, PyTorch 2.14.1 and Open3D 0.20.0. Video inference used **CPU**, official `facebook/sam2.1-hiera-tiny`, from Meta's Git repository at `2b90b9f5ceec907a1c18123530e92e794ad901a4`. These are synthetic reconstruction and phone-export compatibility measurements, not a successful physical-room scan or an MPS performance claim.

## Scene and checks

The generator supplied 120 RGB keyframes at 320×240, cropped 140×108 depth, varying confidence, a yawed level anchor, floor/walls, a desk, touching box/cylinder and a separate chair. The desk and chair had supplied tap-time masks; the box and cylinder had point-only prompts. Full ground truth was excluded from the ZIP and reconstruction. SAM followed every keyframe in both directions from the seed views.

An initial track leaked part of the box into the cylinder. Cross-view surface voting removed the main leak; a subsequent fresh run exposed sensitivity to a few remaining depth outliers in the footprint. V2 now trims horizontal evidence before both minimum-area yaw and width/depth estimation, while preserving the vertical estimator for furniture feet. Five repeated fits per object kept the cylinder's horizontal extents in **18.804–18.822 cm**; full measurements are in `segmentation-service/data/v2-extent-repeatability.json`.

A development replay of actual SAM tracks, after manifest/model/input-SHA verification, produced all four meshes with maximum dimension error **1.235 cm** and bottom-centre error **1.483 cm**. The replay took **34.79 s** for refinement, fusion, textures and export; this is explicitly not fresh model inference time. Its assessment is `segmentation-service/data/v2-robust-footprint-replay/assessment.json`.

## Fresh real-SAM HTTP result

**4/4 passed**, scan `scan_v2_verified_http`, job `c6edb0eca9d141308ab3c87c719e71d9`. This run recomputed official SAM video tracks, with no saved-track replay or geometric discovery. Exactly `obj_01`, `obj_02`, `obj_03`, `obj_04` were returned in input order, each with null parent and a valid mesh.

| Object | Width × height × depth (m) | Largest dimension error | Bottom-centre error |
| --- | --- | --- | --- |
| Desk | 1.209 × 0.749 × 0.759 | 1.061 cm | 1.486 cm |
| Box | 0.328 × 0.273 × 0.259 | 0.870 cm | 1.123 cm |
| Cylinder | 0.186 × 0.241 × 0.186 | 0.628 cm | 1.116 cm |
| Chair | 0.468 × 0.938 × 0.449 | 1.235 cm | 1.446 cm |

All four downloaded GLBs passed the phone contract: at most 20,000 triangles, bottom-centre metre frame, identity node and one embedded 2048×2048 texture. Measured visibility was 106–120 frames, angular coverage 308–356°, observed texture fraction 96.3–98.6%; all four reported `low_quality=false`.

Upload validation/persistence took **0.192 s**. Poll-to-done was **741.15 s** (12 min 21 s); worker total **739.33 s**, including video tracking/refinement **703.71 s**, meshing **7.51 s**, and texturing/export **28.04 s**. The slowest sampled `/health` request was **11.30 ms**. Regression tests also ran during part of the benchmark. These are CPU background-job timings; the single-image selection target of 3 seconds does not apply to a whole scan.

Evidence: `segmentation-service/data/scan-v2-verified-http/benchmark.json`, its downloaded GLBs/thumbnails/proposals, and `segmentation-service/data/scans/scan_v2_verified_http/` with inputs, raw SAM masks, refined masks, surface votes, per-object reports, events and durable job state. The environment/source record is `segmentation-service/data/scan-v2-environment.json`.

## CPU regression tests

**168 passed in 214.13 s**, with no checkpoint downloads required. Tests cover v1 compatibility, strict v2 manifests/archives/masks, full-HD sensor coordinates and non-offset frame indices, bidirectional/batched tracking prompts, exact proposal identity/order including failures, cross-view ownership, tap-connected components, tilted-anchor capture regressions, footprint outliers, verified replay provenance and localhost/LAN options. The official model itself is checked by the separate real-SAM benchmark. TestClient emits one upstream Starlette deprecation warning. Evidence: `segmentation-service/data/service-tests-v2-final.txt`.

## Phone exporter and Wi-Fi

Ran the requested `cd android && ./gradlew testDebugUnitTest -PcaptureOut=/tmp/cap` using a temporary Android SDK and JDK 21: **63 tests, 0 failures, 0 errors, 4 skipped**, build successful in 35 s. Android source was not edited.

Both `/tmp/cap/phone_scan_v2` and `/tmp/cap/phone_scan_v2.zip` passed the Mac validator. The fixture has 36 frames, 640×480 RGB, cropped 158×118 depth, two objects, one supplied mask and one point-only seed. Its ZIP is 430,562 bytes. Actual HTTP upload returned **202**; the job was cancelled while queued because this exporter fixture is compatibility data, not a measured room reconstruction. Evidence: `segmentation-service/data/phone-v2-validation.json`.

A separate `--host 0.0.0.0 --port 8001` floodfill service answered `/health` with HTTP 200 via both localhost and the Mac's then-current `en0` address, `172.30.36.215`. It was shut down after the check. Evidence: `segmentation-service/data/lan-bind-verification.json`. The main service defaults to `127.0.0.1:8000`; use the current address from `ipconfig getifaddr en0` when enabling Wi-Fi on port 8000. A physical phone Wi-Fi connection remains untested.

## Reproduce

From `segmentation-service/`, with official SAM and reconstruction dependencies installed:

```bash
.venv/bin/python -m housing_segmentation --backend sam2 --model tiny
# In another terminal:
.venv/bin/python scripts/generate_synthetic_scan.py data/new-v2 --version 2 --frames 120
.venv/bin/python scripts/benchmark_scan_http.py data/new-v2 --output data/new-v2-result \
  --truth data/new-v2/ground_truth.json
.venv/bin/python -m pytest -q
```

The independent assessor reloads each GLB and checks dimensions below 2 cm error and bottom-centre placement below 3 cm. The benchmark also downloads textures/thumbnails and validates the minimal phone GLB contract. Actual high-resolution room captures, poor depth, reflective objects and incomplete walking coverage need separate evaluation; phone coverage numbers cannot substitute for that evidence.
