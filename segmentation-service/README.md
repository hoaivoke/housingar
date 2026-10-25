# Housing AR: Mac segmentation and reconstruction service

Python **3.10–3.12**. The current flow is **scan belongings → review object proposals → confirm My stuff → save placements**. The Mac reconstructs metric object GLBs, thumbnails and quality reports, then persists confirmed library copies and layouts. Tap segmentation and individual captures remain available. The older capture background output is retained for compatibility; new scans do not build removal backgrounds or run learned inpainting. This service does not modify or build Android.

## Set up on macOS

From `segmentation-service/`, choose an installed Python in the supported range (the system's default may be newer):

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

`requirements.txt` contains runtime dependencies; `requirements-dev.txt` adds pytest and TestClient dependencies. Neither requires torch, SAM, a GPU, or a checkpoint download.

Start connectivity mode:

```bash
python -m housing_segmentation --backend floodfill
curl http://127.0.0.1:8000/health
```

Default binding is **127.0.0.1:8000**. Floodfill reports `backend=floodfill`, `model=opencv-floodfill`, `device=cpu`. It returns three color-tolerance candidates, not semantic object segmentation. Its scores are fixed heuristic rankings, not learned confidence scores. Negative prompts exclude their connected color regions.

## Install and run official SAM 2.1

With the same virtual environment active:

```bash
python -m pip install 'torch>=2.5.1,<3' 'torchvision>=0.20.1,<1' 'huggingface-hub>=0.25,<2'
SAM2_BUILD_CUDA=0 python -m pip install "git+https://github.com/facebookresearch/sam2.git"
```

Alternatively, `SAM2_BUILD_CUDA=0 python -m pip install -r requirements-sam2.txt` installs the same official GitHub source and dependencies. **Do not install the `sam2` package from PyPI; it is not the official Meta package.** The official distribution is named `SAM-2`.

```bash
export PYTORCH_ENABLE_MPS_FALLBACK=1
python -m housing_segmentation --backend sam2 --model tiny
# Or: python -m housing_segmentation --backend sam2 --model small
```

The loader calls `SAM2ImagePredictor.from_pretrained("facebook/sam2.1-hiera-tiny", device=...)` or `facebook/sam2.1-hiera-small`. Initial startup downloads the checkpoint into `.cache/huggingface` unless `HF_HOME` is set. Download time and model initialization are not inference latency. MPS is selected when PyTorch reports it available; otherwise CPU is selected. `PYTORCH_ENABLE_MPS_FALLBACK` defaults to `1` before torch is imported. `--device cpu` forces CPU; `--device mps` fails clearly if unavailable. There is no silent switch from SAM to floodfill.

Environment alternatives:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SEGMENTATION_BACKEND` | `floodfill` | `floodfill` or `sam2` |
| `SAM2_MODEL` | `tiny` | `tiny`, `small`, or the corresponding full Hugging Face model ID |
| `SEGMENTATION_DEVICE` | `auto` | `auto`, `cpu`, or `mps` |
| `SEGMENTATION_DATA_DIR` | this directory's `data/` | Local review data |
| `SEGMENTATION_CACHE_FRAMES` | `2` | Maximum retained image/predictor states |
| `HF_HOME` | this directory's `.cache/huggingface` | Downloaded checkpoints |

CLI flags override backend/model/device environment choices. `--host` and `--port` override the bind address. Use one service worker; independent workers would have separate embedding caches. An alternative ASGI entry point is `uvicorn housing_segmentation.app:app --host 127.0.0.1 --port 8000 --workers 1`.

## Connect the Pixel by USB

Enable USB debugging, connect one phone, unlock it, and accept the computer's debugging authorization. With the service running, from the repository root:

```bash
./scripts/phone_usb_setup.sh
# Optionally install an already built APK:
./scripts/phone_usb_setup.sh /absolute/path/to/app-debug.apk
# Download reports explicitly exported by the app:
./scripts/phone_usb_setup.sh pull-logs
```

The helper requires exactly one attached, authorized device, always selects its serial explicitly, and runs `adb reverse tcp:8000 tcp:8000`. The phone's endpoint is **http://127.0.0.1:8000**. Forwarding may need to be repeated after disconnecting the cable. `ADB=/path/to/adb` overrides tool discovery; otherwise PATH and `ANDROID_HOME`/the standard macOS SDK location are checked.

`pull-logs` copies `/sdcard/Android/data/com.housingar.inspect/files/reports` and `.../drift` under `data/phone/<UTC timestamp>_<serial>/`. It reports missing/unreadable directories and returns nonzero rather than claiming a complete download.

### Connect over Wi-Fi

Put the Mac and phone on the same private network. Stop the existing service, then explicitly enable network listening:

```bash
python -m housing_segmentation --backend sam2 --model tiny --host 0.0.0.0 --port 8000
# In another terminal, find the Mac's active Wi-Fi address:
ipconfig getifaddr en0
```

For example, if that prints `192.168.1.42`, type **`http://192.168.1.42:8000`** into the phone's service URL setting. Use the active network interface if it is not `en0`; the address can change. `0.0.0.0` is not a phone URL. USB forwarding is unnecessary in this mode. Security: this development API has no authentication; use network binding only on a trusted private network.

Run `./scripts/check_mac.sh` from the repository root to inspect macOS, chip, RAM, free disk, Python, Node, Java/Android Studio, adb/devices, Xcode tools/Swift, Object Capture support, the port 8000 listener, and service health. It compiles a small RealityKit probe and prints `PhotogrammetrySession.isSupported`. It prints PASS/FAIL counts and returns nonzero for incomplete prerequisites. Activate this venv first to check the service's Python rather than an unrelated default Python. The script installs nothing.

## Exact phone API

Interactive OpenAPI documentation is at `/docs` while the service runs.

### GET /health

```json
{"status":"ok","backend":"sam2","model":"facebook/sam2.1-hiera-tiny","device":"mps"}
```

`backend` is `sam2` or `floodfill`; `device` is the device actually used (`mps` or `cpu`).

### POST /v1/segment

`multipart/form-data` fields:

| Field | Content |
| --- | --- |
| `frame_id` | Nonempty text, maximum 256 characters; unique for the image within its session |
| `points` | Text JSON `[[x,y,label], ...]`; sensor-image pixel coordinates, `1` positive / `0` negative |
| `metadata` | Text JSON object with the fields below |
| `image` | JPEG file in sensor orientation, typically about 640×480; PNG also accepted |

Metadata example:

```json
{
  "session_id": "room-session-001",
  "timestamp_ns": 1234567890,
  "intrinsics": {"fx": 500.0, "fy": 500.0, "cx": 320.0, "cy": 240.0},
  "camera_pose_world": [0, 0, 0, 0, 0, 0, 1],
  "sensor_to_display_rotation_deg": 90,
  "depth_timestamp_ns": 1234567880
}
```

All listed metadata fields are required. Extra metadata is retained. Pose values are preserved in the phone's supplied order; the service does not reinterpret or rotate them. Session IDs must use 1–128 letters, digits, underscores or hyphens. Timestamps are nonnegative integers; focal lengths must be positive. Rotation is one of 0/90/180/270.

Response shape (mask contents abbreviated):

```json
{
  "frame_id": "frame-001",
  "backend": "sam2",
  "model": "facebook/sam2.1-hiera-tiny",
  "width": 640,
  "height": 480,
  "embedding_cached": false,
  "masks": [
    {
      "score": 0.95,
      "area_fraction": 0.25,
      "mask_png_b64": "<base64 grayscale PNG>",
      "bbox": [20, 30, 220, 330],
      "polygon": [[20, 30], [219, 30], [219, 329], [20, 329]]
    }
  ],
  "timing_ms": {"decode": 2.0, "embed": 120.0, "predict": 15.0, "postprocess": 4.0, "total": 141.0}
}
```

Numbers above illustrate the contract, **not measured performance**. Both backends request/produce multiple masks, sorted by descending score. Every mask is an 8-bit grayscale PNG with exactly the input image's width/height, with `255` foreground and `0` background. The server applies no EXIF or display rotation, resizing, or coordinate normalization to the returned mask. `bbox` uses an exclusive upper-right bound `[x0,y0,x1,y1]`. `polygon` is a simplified external outline of the largest component; the PNG is authoritative for holes and additional components. Empty masks use a zero bbox and empty polygon.

The model and its bounded LRU cache are protected by one inference lock. Cache keys are `(session_id, frame_id)`; a corrected click on a retained frame reuses its predictor/embedding, reports `embedding_cached=true`, and `embed=0`. Intervening frames are supported up to the configured cache capacity. Reusing a cached ID with different image bytes returns 409. In floodfill mode the same flag means cached image preparation was reused; there is no learned embedding.

Model loading, image decoding, embedding, prediction, PNG generation, and file persistence execute outside the async request-parsing event loop. `/health` remains responsive during inference. MPS operations are synchronized for phase timings. `total` includes engine processing and lock wait, but excludes multipart parsing, network time, and artifact writes; measure HTTP round-trip latency separately when evaluating the complete phone flow.

Errors use a clear `detail` field: missing fields or invalid point/metadata values return 422; invalid JSON or undecodable/empty images return 400; images over 10 MiB or 8 megapixels return 413; unsupported image formats return 415. Points outside the image are rejected. JSON values must be finite. SAM startup errors require fixing model setup or explicitly selecting floodfill.

### POST /v1/diagnostics

JSON object with a valid `session_id`; additional fields are saved unchanged to `data/diagnostics/<UTC timestamp>_<session_id>.json`.

```json
{"status":"saved","path":"diagnostics/20261024T140000.000000Z_room-session-001.json"}
```

### POST /v1/logs/drift

JSON object with `session_id`; append one JSON line to `data/drift/<session_id>.jsonl`. Response is `{"status":"saved","path":"drift/<session_id>.jsonl"}`. Concurrent appends in the service process are serialized.

### GET /v1/logs

```json
{
  "diagnostics": [{"path":"diagnostics/example_room-session-001.json","size_bytes":200,"modified_at":"2026-10-24T14:00:00+00:00"}],
  "drift": [{"path":"drift/room-session-001.jsonl","size_bytes":100,"modified_at":"2026-10-24T14:00:00+00:00"}],
  "geometry": [{"path":"geometry/room-session-001_selection-001.json","size_bytes":300,"modified_at":"2026-10-24T14:00:00+00:00"}]
}
```

Paths are relative to the configured data directory, for local inspection; no file-download endpoint is required by the phone contract.

Every successful segmentation request saves a separate `data/frames/<session_id>/<UTC timestamp>_<unique suffix>/` containing the received `image.jpg` (or PNG), `metadata.json` (frame ID, points, metadata), complete `response.json`, and all ranked `mask_0.png`, `mask_1.png`, etc. Corrections preserve previous results. Runtime data and checkpoints are ignored by version control.

### POST /v1/geometry

JSON object containing `session_id` and `selection_id` (1–128 letters, digits, underscores or hyphens each). Additional fields are preserved. Saves atomically to `data/geometry/<session_id>_<selection_id>.json`, replacing an earlier record for the same selection. Returns `{"status":"saved","path":"geometry/<session_id>_<selection_id>.json"}`. The file appears in `/v1/logs`.

## Metric 3D object capture and removal

The exact versioned archive, camera/depth conventions, HTTP endpoints, output transforms, and phone rendering notes are in [capture_v1.md](../docs/capture_v1.md). [Model provenance](../docs/reconstruction_models.md) records the official SAM source, local inpainting checkpoint, licence information, and fallback rules. [Measured synthetic results](../docs/reconstruction_results.md) distinguish tested behaviour from real-room work still to validate.

The removal portion is a compatibility path, not the revised product flow. New scans/library/layouts are described below and in [scan_v1.md](../docs/scan_v1.md).

Install these optional reconstruction dependencies in the same supported Python environment, after the official SAM installation above:

```bash
python -m pip install -r requirements-reconstruction.txt
# Some macOS Open3D wheels reference an unavailable Homebrew libusb path.
# This helper repairs only the installed venv library and ad-hoc signs it:
python scripts/repair_open3d_macos.py
# Requires macOS 14+ and Xcode Command Line Tools:
bash reconstruction/build.sh
# Download the locally executed LaMa TorchScript checkpoint with integrity checking:
python scripts/download_inpainting_model.py
python -m housing_segmentation --backend sam2 --model tiny
```

The repair helper is harmless when the Open3D dependency already resolves. No system/Homebrew libraries are installed or modified. Open3D CPU fusion is independent of MPS model inference. This Mac's scalable TSDF implementation returned an empty mesh in control tests; a bounded uniform TSDF fallback is included and its actual implementation/resolution are reported. Reconstruction dependencies are optional for image-only segmentation; a missing dependency fails the capture job clearly.

The pipeline propagates the approved seed mask with official SAM 2.1 video. The projected seed box is only a recovery prompt: multi-view masked depth refines the actual extent after rejecting support planes and disconnected objects. Reliable depth inside the refined extent augments missed mask edges; masks combine model output and geometry. High-resolution phone objects now use masked RealityKit photogrammetry first, registered to metric ARCore cameras. Their shape is not clipped to the depth extent or vetoed by a TSDF comparison. Photo failure selects an explicit adaptive coarse-depth fallback. Lower-resolution synthetic inputs retain the original TSDF path and optional native check; see the image-based geometry section below. A persistent queue runs one reconstruction subprocess at a time, keeping native-library work and its Python GIL outside the API process. GPU use shares a lock with tap segmentation, so taps wait while a capture job is processing; `/health`, polling, and cancellation remain available. Cancellation terminates the reconstruction process group, including native Object Capture if active.

The background is reconstructed **3D support geometry** with projected image textures, local plane extension bounded to 20 cm from measured support, and texture-space LaMa completion. It is exported in the capture anchor frame. Each GLB is independently simplified to at most 50,000 triangles and one 2048-square PNG. Hidden shape is inferred only near observed support; quality warnings include missing view coverage and over 40% inpainted surface texels. The seed box never clips the object: the report records evidence outside that prior and warns when it was truncating. The measured footprint chooses object yaw; gravity remains +Y for level or tilted anchors.

Noisy floor strips are merged into one support with a measured rejection band and per-frame floor observations. Cross-view silhouettes reject floor depth behind thin legs and prevent its return during filling/fusion. Object components need support from independent depth views; the largest component alone no longer decides which chair parts survive. Component-aware simplification preserves small validated rails/legs. Final mesh height/footprint and fragmentation are checked for quality. The [real-chair fixture](../fixtures/real_chair_capture_v1/README.md) reproduces the original failure without downloading a model; [its results](../docs/real_chair_regression_results.md) include stage counts and remaining raw-depth limits. Metre scale comes from the phone's calibrated depth and poses; manual dimensions are not an input.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CAPTURE_MASK_BACKEND` | `sam2` | `sam2` or explicit `depth_bbox` test/fallback mode |
| `CAPTURE_SAM2_MODEL` | `facebook/sam2.1-hiera-tiny` | Official tiny or small video model ID |
| `CAPTURE_DEVICE` | `auto` | Actual model device selection: `auto`, `mps`, `cpu` |
| `CAPTURE_GEOMETRY_BACKEND` | `auto` | `auto`, `photo`, or explicit `depth` |
| `CAPTURE_OBJECT_CAPTURE` | `1` | Set `0` to skip native reconstruction; real phone inputs use the coarse depth fallback |
| `CAPTURE_OBJECT_CAPTURE_TIMEOUT` | `900` | Maximum seconds for the native subprocess |
| `CAPTURE_INPAINTING` | `lama` | `lama` or explicit `opencv` fallback |
| `LAMA_MODEL_PATH` | `.cache/inpainting/big-lama.pt` here | Local TorchScript checkpoint |

`GET /health` continues to report the **tap segmentation** backend/model/device. Actual capture backends/devices and fallbacks are recorded in that job's `report.json`. OpenCV fallback is clearly reported if LaMa is absent or fails. Use one ASGI worker: the capture root is exclusively locked to prevent duplicate processing. Interrupted jobs become failed after restart; queued/active jobs support cooperative cancellation.

If an MPS video track collapses to empty masks on most non-seed frames, the same official SAM model is repeated on CPU before extent refinement. The report records this explicitly; capture masks must not be interpreted as measured MPS tracking quality. See [the phone fixture regression results](../docs/seed_extent_results.md).

| Request | Result |
| --- | --- |
| `POST /v1/captures` | Multipart `manifest` JSON text plus `archive` ZIP; HTTP 202 with `capture_id` and `job_id` |
| `GET /v1/jobs/{job_id}` | Durable state, progress, message/error, and output URLs when done |
| `POST /v1/jobs/{job_id}/cancel` | Request cancellation; completed/failed jobs return 409 |
| `GET /v1/jobs/{job_id}/files/object.glb` | Object in its bottom-centred local gravity frame |
| `GET /v1/jobs/{job_id}/files/background.glb` | Background patch in anchor coordinates |
| `GET /v1/jobs/{job_id}/files/report.json` | Dimensions, `object_to_anchor`, quality, timings, backends and registration |

Archives are validated and persisted before queuing; upload latency includes that bounded work but excludes model inference/reconstruction. Files are served only for a completed job. Capture IDs cannot be reused. Job artifacts, intermediate meshes, masks/contact sheets, reports, and native logs are retained under `data/captures/<capture_id>/`.

### Offline capture and synthetic checks

```bash
# CPU raycasting: textured box, cylinder, table, surrounding walls; cropped low-res depth.
python scripts/generate_synthetic_capture.py data/my-synthetic --frames 36
# Same pipeline as HTTP; output must be a new folder:
python scripts/process_capture.py data/my-synthetic --output data/my-result
# Validate both downloaded/exported GLBs; optionally ray-test the patch against known geometry:
python scripts/inspect_capture_outputs.py data/my-result --synthetic-source data/my-synthetic
# For the phone-packaged synthetic fixture, compare with the published acceptance targets:
python scripts/assess_phone_capture.py data/my-result --truth ../fixtures/phone_capture_e2e/ground_truth.json
# Explicit model-free CPU smoke run:
python scripts/process_capture.py data/my-synthetic --output data/my-cpu-result \
  --mask-backend depth_bbox --device cpu --no-object-capture --inpainting opencv
```

Use HTTP, or stop the service, when the offline output would share the service's `data/captures/` root. The offline runner refuses a locked output root and existing output folders. `depth_bbox` is a geometric test/fallback mode, not learned segmentation: it uses support-rejected depth connected to approved seed depth rather than treating the seed box as an object. Generated captures prove coordinate handling, scale, basic geometry, and file interoperability; they cannot establish real-room or reflective-object quality. The sub-three-second Milestone 0 target applies to a **single segmentation result**, not this background reconstruction job.

## Scan proposals, My stuff and layouts

Use the same reconstruction dependencies, official SAM install and single running service. The scan pipeline does not need LaMa. High-resolution real tapped scans use native masked Object Capture by default; small synthetic scans retain depth fusion. New scans use [the tapped-object v2 contract](../docs/scan_v2.md): tap each thing to keep, then walk around it. The [v1/library/layout contract](../docs/scan_v1.md) remains supported unchanged.

| Request | Purpose |
| --- | --- |
| `POST /v1/scans` | Upload scan_v1 or scan_v2 manifest JSON text and ZIP; returns scan/job IDs with HTTP 202 |
| `GET /v1/jobs/{job_id}` | Poll the shared scan/capture queue |
| `POST /v1/jobs/{job_id}/cancel` | Stop queued or active work |
| `GET /v1/scans/{scan_id}/proposals` | Review meshes, outlined thumbnails, dimensions, parent links and quality |
| `POST /v1/library` | Explicitly confirm/name a proposal or completed individual capture |
| `GET /v1/library` | List persistent My stuff items and their mesh/thumbnail URLs |
| `PATCH /v1/library/{item_id}` | Rename an item |
| `DELETE /v1/library/{item_id}` | Delete a confirmed item |
| `POST /v1/layouts`, `GET /v1/layouts` | Save and list item references with rigid world poses |

**V2 builds exactly the 1–30 tapped objects, in input order.** Supplied seed masks or point prompts initialize official SAM 2.1 video tracking in both directions. Other objects' taps on the same seed frame become negative prompts. Confidence-weighted surface votes across views remove leaks into other tapped things. Tap-connected depth and surrounding support-plane rejection refine the metric extent before filling and TSDF clipping. For high-resolution phone objects the raw SAM masks feed photogrammetry directly; the depth extent does not clip the photo surface. Every target retains its object ID and null parent; failures retain a null mesh and a reason. Phone coverage is informational; quality comes from the actual captured evidence.

V1 retains automatic discovery through confidence-weighted sparse projective TSDF, room-plane rejection, supported-item separation and SAM image prompts. Its geometric `depth_cluster` mode remains available for model-free tests. V2 requires the official SAM video model. Neither version automatically confirms library items.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCAN_MASK_BACKEND` | `sam2` | Official model; v1 alone accepts explicit `depth_cluster` test mode |
| `SCAN_DEVICE` | `cpu` | Actual scan-model device: `cpu`, `mps`, `auto` |
| `SCAN_SAM2_MODEL` | `facebook/sam2.1-hiera-tiny` | Official tiny/small model ID |
| `SCAN_SAM_VIEWS` | `12` | Angular sectors for v1 SAM image prompts and both versions' texture views; v2 video follows every keyframe |
| `SCAN_GEOMETRY_BACKEND` | `auto` | Real high-resolution tapped objects use photos; `depth` forces the coarse fallback |
| `SCAN_OBJECT_CAPTURE_TIMEOUT` | `900` | Native reconstruction timeout in seconds |
| `SCAN_OBJECT_CAPTURE` | `0` | Optional native check on the legacy low-resolution depth path |

Library files are independent validated copies under `data/library/`; layouts are JSON under `data/layouts/`. Scan inputs, TSDF, masks, candidate reports and outputs stay under `data/scans/<scan_id>/`. Scan and capture jobs share one queue and one process at a time. Incomplete jobs become failed on restart. A failed candidate remains listed with a null mesh and reason. The phone must explicitly confirm a proposal before it becomes a library item.

Synthetic validation and HTTP benchmark (the benchmark needs a running service):

```bash
python scripts/generate_synthetic_scan.py data/my-scan --version 2 --frames 120
python scripts/benchmark_scan_http.py data/my-scan --output data/my-scan-http \
  --truth data/my-scan/ground_truth.json
# Explicitly create synthetic demo library copies and exercise a saved layout:
python scripts/benchmark_scan_http.py data/my-scan --output data/my-library-check \
  --truth data/my-scan/ground_truth.json --confirm-library
```

The generator renders floor/walls, a desk with touching box/cylinder and a separate chair along a 120-frame walking loop, with lower-resolution cropped depth and nonconstant confidence. V2 emits one tap per ground-truth object, mixing supplied masks and point-only seeds; omit `--version 2` for v1. Full ground truth stays outside the upload archive and is read only by the independent assessor. The HTTP benchmark records upload/job/health timings, downloads and validates actual GLBs, checks every object against truth, and optionally exercises explicit library confirmation/rename/delete/layouts. It never asserts real-room quality. See [v2 measurements](../docs/scan_v2_results.md) and [earlier v1 results](../docs/scan_results.md).

For offline processing, `python scripts/process_scan.py data/my-scan --output data/my-result` uses the same pipeline. An explicit `--tracking-source data/scans/<previous-scan-id>` reuses saved SAM video masks only after checking the manifest, model and SHA-256 of every input file. This supports geometry regressions without another model run; it is not a fresh SAM timing measurement and cannot be requested through the upload manifest. Stop the service before writing into its `data/scans/` root.

## Tests and image benchmark

```bash
# Image/API tests need no model downloads. Capture geometry tests require requirements-reconstruction.txt:
python -m pytest -q

# Real room photographs should be JPEG/PNG in a folder:
python scripts/benchmark.py /absolute/path/to/room-photos --runs 5 --warmup 1 --output data/benchmark-sam2.json
# Explicit CPU or MPS runs:
python scripts/benchmark.py /absolute/path/to/room-photos --device cpu --runs 5
# Connectivity backend only:
python scripts/benchmark.py /absolute/path/to/room-photos --backend floodfill --runs 5 --output data/benchmark-floodfill.json
```

The repository also has a `scripts/benchmark.py` entry point that forwards to this benchmark. Each model runs in a fresh child process, so peak process memory is attributable to that model. The report records source labels (`--image-source`), actual image sizes, platform, Python/torch versions, actual device, model-load time, individual runs, mean/median/p95/max embedding and prediction times, total processing time, and cached correction latency. Warmup is excluded from timing samples. Peak RSS includes loading/warmup; sampled MPS tensor/driver memory is reported only for an actual MPS run. Memory uses binary MiB.

The target is **under 3000 ms per uncached result**. Benchmark totals include decoding and mask serialization but exclude network, disk saves, and model startup/download. An engine result below that target does not by itself prove end-to-end phone latency. A failed model writes an error result and gives the benchmark a nonzero exit; it is never relabeled as floodfill performance. Generated images are suitable for smoke testing, but cannot establish room-segmentation quality. Use captured room photographs for that assessment.

## Image-based geometry for real phone recordings

Install `requirements-reconstruction.txt` into the existing Python environment, then run `bash reconstruction/build.sh` on a Mac with supported RealityKit Object Capture hardware and Xcode Command Line Tools. The new CPU repair dependencies are MeshFix (`pymeshfix`, GPL-3.0), scikit-image and manifold3d. Apple photogrammetry uses the native macOS runtime; it is not a measured PyTorch MPS implementation.

`CAPTURE_GEOMETRY_BACKEND=auto` and `SCAN_GEOMETRY_BACKEND=auto` are the defaults. For tapped scans and individual captures with ≥1280px-wide images at least six times their depth width, `auto` reconstructs the original RGB photos inside the raw SAM masks. It does not first create an object TSDF or reject photo size because it differs from noisy depth. The returned method is `apple_masked_photogrammetry`. Supplied masks are enabled, gravity is supplied, and estimated photo cameras are robustly registered to the metric phone trajectory. Apple does not expose pose/intrinsic setters for these samples: this is not a pose-locked MVS implementation. Recovered photo cameras/intrinsics are used for textures when available (intrinsics require macOS 26+).

`photo` forces this path. `depth` explicitly requests the adaptive depth fallback for real phone images: ≥192 confidence, voxel size from metric depth-pixel footprint and measured noise with a 2 cm noise floor, tap-connected component selection, closed/smoothed surface and no disconnected fragments. It reports `adaptive_depth_fallback` and stays low quality. Failed photo reconstruction falls back explicitly; if neither path works, the proposal has a null mesh and a failure reason. Lower-resolution synthetic fixtures keep their existing depth reconstruction and acceptance targets.

```bash
.venv/bin/python scripts/process_scan.py PATH_TO_INPUTS --output data/new-scan \
  --geometry-backend auto --object-capture-timeout 900
.venv/bin/python scripts/process_capture.py PATH_TO_INPUTS --output data/new-capture \
  --geometry-backend photo --object-capture-timeout 900
# Explicit depth fallback:
SCAN_GEOMETRY_BACKEND=depth .venv/bin/python -m housing_segmentation --backend sam2
```

Reports include the actual method, camera registration, per-stage surface/component counts, repair extent checks, upper/lower outline recall, watertightness and observed versus filled texture. Texture visibility uses the mesh rather than sparse raw depth for image-built objects. See [measured before/after results](../docs/image_reconstruction_results.md) and [portable CPU regression fixtures](../fixtures/real_reconstruction/README.md).

For real object reconstruction, capture reports use `quality_scope: "object"`, with separate `object_low_quality` and `background_low_quality`. The legacy removal background can remain unreliable without rejecting a valid item for My stuff; its diagnostics and warnings remain available. Small synthetic removal fixtures retain `quality_scope: "object_and_background"`.

## Rental listings, estimated rooms and My stuff

The Mac service now supplies `/v1/listings`, listing detail, editable estimated room plans, and saved furniture placements. The final request/response shapes, coordinate conventions and validation are in [listings_v1.md](../docs/listings_v1.md); [measured results](../docs/listings_v1_results.md) include cache, room geometry, persistence, HTTP/LAN and library checks.

Set `RENTCAST_API_KEY` in the service's environment and restart it to enable RentCast's real rental listings. Without a key, results are explicitly fictional samples. Successful searches are cached on disk for 24 hours; addresses use the free Census geocoder. The key is never sent to the browser. There is no listing-site scraping.

```bash
python -m pip install -r requirements.txt
python -m housing_segmentation --backend sam2 --host 0.0.0.0 --port 8000
# Example fully offline search:
curl 'http://127.0.0.1:8000/v1/listings?lat=30.2672&lon=-97.7431&max_rent=2500'
```

The service reads `../web/dist` at `/` once the separate website build exists, including when it appears after startup. Browser routes fall back to index.html; API routes and existing GLB/thumbnail URLs retain their behavior. CORS permits localhost:5173 and 127.0.0.1:5173 for `/v1`. Phone over USB: keep `adb reverse tcp:8000 tcp:8000`, then open `http://127.0.0.1:8000/`; on trusted Wi-Fi use the Mac's LAN address.

Room geometry is an estimate from area and room counts, not the property's measured floor plan. Saved user edits and My stuff placements persist per listing under `data/listings/`. The service does not change original library meshes.
