# Tapped belongings scan v2

Scan v2 builds exactly the things selected by the phone user. It does not run automatic object discovery. `POST /v1/scans` continues accepting [scan v1](scan_v1.md) unchanged; the job, proposal, library, layout and minimal GLB contracts are shared.

## Manifest and archive

Use all scan_v1 fields, with `schema="housingar.scan.v2"` and a required `objects` array of **1–30** entries:

```json
{"object_id":"obj_01","seed_frame_index":17,"points":[[960,540,1]],
 "mask":"seeds/obj_01.png","point_anchor":[0.2,0.8,-1.4],
 "phone_coverage":{"views":12,"coverage_deg":240.0}}
```

- Object IDs use `[A-Za-z0-9_-]{1,64}` and are unique. Array order is proposal order.
- `seed_frame_index` must match a frame's **index**, not its array offset.
- `points` is nonempty image-pixel `[x,y,label]` triples, sensor orientation, finite and within that seed image; 1 includes and 0 excludes. At least one positive tap is required.
- `mask` is nullable/optional. When present it must be `seeds/<object_id>.png`, an 8-bit grayscale PNG matching the seed JPEG dimensions, with nonempty 255 foreground and 0 background. It can be the decoded `mask_png_b64` returned by `/v1/segment`.
- `point_anchor` is nullable/optional; when present it is three finite metre coordinates in the scan anchor. It chooses a connected depth component, not a clipping box or a new scale.
- `phone_coverage` is required: nonnegative integer views and finite coverage_deg in 0–360. It is informational and does not replace Mac visibility measurements.
- Unknown fields are rejected at every manifest/object/coverage/frame level.

The ZIP includes the same referenced `frames/`, `depth/`, optional `confidence/` files as v1 plus referenced `seeds/*.png`. Unreferenced seed files are rejected. Missing/incorrect-size/incorrect-encoding masks are 4xx errors. All existing ZIP traversal/symlink/encryption/duplicate protections and 600 MB compressed / 1.8 GB uncompressed / 1000-frame limits remain. Scan anchors are level; all poses remain column-major, camera looks -Z, units metres.

Real Pixel scans commonly have 1920×1080 RGB and 160×90 depth/confidence. RGB and depth need not have the same resolution: their existing intrinsic/pixel-centre mapping is authoritative. The service does not rotate sensor images. Model video proxies are resized with prompt coordinates mapped to them; returned review masks, texture projection and fusion use original image/depth calibration. Video frames and per-object masks are shared or loaded lazily from disk to avoid retaining one full-resolution video per object.

## Processing and exact proposal identity

Each object starts from its supplied mask or official SAM 2.1 point prompts. Other objects' positive taps become negative prompts where their seed frames coincide. Official SAM video follows the scan in both directions from the conditioning seeds. Shared video decoding/encoder work is permitted; a frame before an object's seed uses the reverse result, and a frame after it uses the forward result. Progress identifies individual things, for example “Following thing 2 through the scan”. No scan_v1 discovery/automatic clustering stage chooses additional things.

Before extent refinement, confidence-weighted SAM labels vote across views in 8 mm anchor-space cells. Each keyframe contributes at most one weighted vote per cell/object, so a close camera's denser pixels do not dominate. A label needs at least 55% of the cell's votes to supply extent evidence. A cell consistently assigned to one target cannot be fused into another. This prevents a transient cylinder-to-box leak from enlarging the cylinder, without discovering additional objects. Ambiguous/unobserved cells can contribute geometric completion only inside the refined extent and outside another target's current mask.

Multi-view masked depth is refined as in individual captures: RANSAC on unmasked surrounding depth rejects support/room planes within 1.5 cm, retains the component connected to point_anchor (or approved seed depth when absent), and derives a robust extent before geometric filling/clipping. The connected component must lie within 25 cm of a supplied tap. A plane within 2.5 cm of the tapped surface is protected, so a tapped tabletop/seat is not discarded as background. Plane rejection and geometric completion remain heuristics and are recorded. Other tracked objects are excluded from mask completion.

For v2, the horizontal footprint rejects the outer 0.5% of evidence before choosing its minimum-area rectangle, then uses 0.5%–99.5% width/depth bounds plus a 4 mm margin. This prevents a few surviving depth outliers from rotating or enlarging the object. Vertical bounds keep the capture estimator's 0.1%–99.9% quantiles, preserving sparse furniture feet and the bottom pivot. Both choices are recorded in the refinement report; capture_v1 is unchanged.

High-resolution phone inputs now use masked Apple photogrammetry, with metric VIO registration and explicit coarse-depth fallback; small synthetic inputs retain metric TSDF. Image geometry is not constrained by noisy depth bounds. The existing minimum-area footprint yaw, +Y up, bottom-centre pivot, one-texture GLB export, cropped outlined thumbnail, and measured visibility/appearance quality apply. An object whose available photo and depth paths cannot produce a valid mesh/texture remains a proposal with `mesh_url:null`, `failure_reason`, low_quality and warnings. It never silently vanishes. Phone coverage does not turn a bad reconstruction into a good one.

`GET /v1/scans/<scan_id>/proposals` returns exactly one entry per input object, in order, with **proposal_id=object_id** and **parent_id=null**, including failures. All URLs remain service-relative. A bad object does not prevent other objects from being built; scan-level input/model/runtime failure still uses the normal failed job state. Confirm successful proposals with the existing library API.

## Local processing and review

The default scan device is CPU (`SCAN_DEVICE=cpu`); `/health` describes the separate interactive image backend. Actual video device/model, directions, seed-mask usage and negative-prompt counts are in `data/scans/<scan_id>/masks/<object_id>/tracking.json`. Raw SAM masks, cross-view surface votes, consistent evidence masks and final fusion masks are retained beside them. Tracking decodes one shared proxy video, handles up to four targets together, keeps a bounded CPU encoder cache, and lazily loads sensor-size review masks. CPU attention inputs are aligned to the official model's parameter precision when compressed SAM memory is bfloat16. The model weights/source remain unchanged.

The deterministic generator and production HTTP benchmark are:

```bash
python scripts/generate_synthetic_scan.py data/tapped-demo --version 2 --frames 120
python scripts/benchmark_scan_http.py data/tapped-demo --output data/tapped-demo-result \
  --truth data/tapped-demo/ground_truth.json
```

Run from `segmentation-service/` with the service started. Ground truth stays outside the uploaded ZIP; only the selected tap/mask and ordinary RGB-D inputs enter reconstruction. The independent assessor reloads GLBs and checks dimension error below 2 cm and bottom-centre placement error below 3 cm. [Measured results](scan_v2_results.md) distinguish synthetic reconstruction, phone-export compatibility and actual room evidence.

For offline work, `scripts/process_scan.py <folder> --output <new-folder>` runs the same pipeline. Its optional `--tracking-source <previous-job-folder>` verifies the previous manifest (except scan ID), model and SHA-256 of all input files before copying saved real SAM video tracks. Replay provenance is recorded; replay timing must not be called fresh model inference. HTTP uploads cannot select this mode. Outputs must be new folders; stop the service before writing inside its scan storage root.

## USB and Wi-Fi

The CLI defaults to **127.0.0.1:8000**. USB forwarding still works with `adb reverse tcp:8000 tcp:8000`; the phone setting is `http://127.0.0.1:8000`.

To use the same private Wi-Fi network, stop the existing service and run from `segmentation-service/`:

```bash
.venv/bin/python -m housing_segmentation --backend sam2 --model tiny --host 0.0.0.0 --port 8000
ipconfig getifaddr en0
```

If the address printed is `192.168.1.42`, type **`http://192.168.1.42:8000`** into the phone's Mac/service URL setting. Use the active network interface's address if it is not en0. `0.0.0.0` is the bind address, not the URL to type on the phone. Both devices must be on the same reachable network; normal macOS incoming-connection permission may be needed.

**Security:** this development API has no authentication; enable network binding only on a trusted private network.

Image-based proposals report `method: "apple_masked_photogrammetry"`; fallback proposals report `adaptive_depth_fallback` and low quality. New photo/depth-fallback exports require one watertight component after welding UV seams, with no self-intersections and no more than 50k triangles. See [the backend contract, measured results and before/after renders](image_reconstruction_results.md).
