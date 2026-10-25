# Housing AR capture format v1

This is the phone/Mac interchange contract. Android implementation is owned separately. All coordinates and outputs below are normative for `housingar.capture.v1`; incompatible future revisions must use a new schema string.

The 24 October product revision drops removal from the new workflow. This existing capture API retains its background output for compatibility and fixture checks; the core flow adds its object mesh to My stuff. New walkthrough scans, proposals, library confirmation and layouts use [scan_v1.md](scan_v1.md), without a removal background.

## Upload and archive

`POST /v1/captures`, multipart fields `manifest` (JSON text) and `archive` (ZIP file). Structurally invalid uploads return 400/413/422 before a job is queued. Inference and reconstruction never run on this request. A valid upload returns HTTP 202 with `{"capture_id":"...","job_id":"..."}` after bounded input validation/persistence.

```text
frames/0000.jpg
depth/0000.bin
confidence/0000.bin  # optional per frame
...
seed_mask.png
```

RGB files are JPEGs in sensor orientation, without EXIF/display rotation applied. Each frame declares its own image resolution and intrinsics. Depth files are tightly packed, row-major **uint16 little-endian millimetres**, exactly `width*height*2` bytes. Zero means invalid depth. Confidence files, when present, are tightly packed uint8 values in 0–255, exactly `depth.width*depth.height` bytes. The mask is an 8-bit grayscale PNG, same resolution as the seed RGB, 255 object and 0 background.

The ZIP need not contain a manifest: the multipart field is authoritative. Folder-based offline input uses `manifest.json` beside `frames/`, `depth/`, and `seed_mask.png`. Referenced paths must be relative POSIX paths. Absolute paths, `..`, backslashes, duplicate ZIP names, symlinks, encryption, and unexpected special files are rejected. The archive limit is **400,000,000 bytes**; the total uncompressed limit is 1,200,000,000 bytes. At most 4096 files and 1000 frames are accepted. Images are limited to 40 megapixels to bound decoding memory.

## Manifest

```json
{
  "schema": "housingar.capture.v1",
  "capture_id": "capture_001",
  "session_id": "session_001",
  "mode": "object",
  "label": "My chair",
  "units": "meters",
  "conventions": "camera: +X right, +Y up, looks down -Z (ARCore/OpenGL). image: origin top-left, +y down. matrices: 4x4 column-major. 'anchor' = ARCore anchor created at selection; world +Y is up (gravity).",
  "anchor_to_world": [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1],
  "seed": {
    "frame_index": 0,
    "points": [[320,240,1]],
    "mask": "seed_mask.png",
    "bbox_anchor": {"min": [-0.2,0.0,-0.2], "max": [0.2,0.5,0.2]}
  },
  "frames": [{
    "index": 0, "timestamp_ns": 1000000000, "tracking": "TRACKING",
    "image": "frames/0000.jpg", "width": 640, "height": 480,
    "intrinsics": {"fx":500,"fy":500,"cx":320,"cy":240},
    "camera_to_anchor": [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1],
    "camera_to_world": [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1],
    "depth": {
      "file": "depth/0000.bin", "width": 240, "height": 180, "timestamp_ns": 1000000000,
      "intrinsics": {"fx":250,"fy":250,"cx":120,"cy":90},
      "to_image": [2,2,80,60]
    },
    "confidence": {"file":"confidence/0000.bin"},
    "sharpness": 100.0, "orbit_deg": 0.0
  }]
}
```

The example's one frame illustrates field layout; **at least 12 frames are required**, typically 30–90. Capture/session IDs match `[A-Za-z0-9_-]{1,64}`. Frame indices are unique nonnegative integers, and timestamps are nonnegative integers. List order is capture order. All frames must be `TRACKING`. `label` is optional text; `confidence` may be omitted or null. All other illustrated fields are required. All numeric values must be finite. Matrices must be rigid homogeneous transforms, flattened column-major. Bbox max must exceed min on every axis. Points are seed-image pixel coordinates, with labels 1 include / 0 exclude, within image bounds. The declared seed frame must exist. The conventions string must describe the stated axes/layout; the canonical string above is recommended.

## Coordinate operations

High-resolution phone object geometry now uses masked photogrammetry first: see [backend selection and results](image_reconstruction_results.md). The following support/extent/TSDF rules describe the depth backend and its diagnostics; they do not clip or reject the image-built object. Raw depth remains useful for background reconstruction and an explicit coarse object fallback.

`seed.bbox_anchor` is a **single-view prior**, not a measured object extent or clipping boundary. It can contain unseen empty space/support and omit hidden corners. The service refines it from multi-view SAM-masked depth before mask filling or TSDF clipping. RANSAC fits broad support/background planes to unmasked depth around the selection; depth within 1.5 cm of a fitted plane is never fused as object. Support requires measured area greater than twice the largest robust seed face (at least 0.18 m²), preventing small object faces from being treated as support. A spatial component must connect to approved seed depth. This remains a geometric heuristic, not a general semantic support classifier.

Repeated noisy plane fits are merged when their normals agree within 8° and their offsets within 12 cm. A merged plane uses a median offset and a rejection band covering the 99th percentile absolute residual, with a 1.5 cm minimum. The lowest near-horizontal support is the floor; points below its accepted surface are excluded too. A noisy floor gets local observations from unmasked depth for each frame (normal, offset and robust spread), so tracking/depth variation is not represented as ten separate physical supports. `frame_observations` are measurements of the same floor. They change depth classification, not the recorded camera poses or metre scale.

Object fusion retains disconnected TSDF components supported by at least two masked depth views (at least eight triangles), instead of retaining only the largest component. This matters for thin chair rails and legs that do not connect in sparse raw depth. Near-horizontal floor fragments within the measured floor envelope are removed before and after simplification. Simplification reserves a small triangle budget for each validated component when it fits the export limit; no artificial bridge is added between unobserved surfaces.

For a noisy floor, band rejection alone cannot distinguish tilted floor debris from thin legs. Near-floor depth is therefore checked against the original SAM silhouettes in other views, with depth-based occlusion, a 1.5 cm projection margin, at least two positive views and 60% foreground agreement among visible views. Rejected depth cannot re-enter through extent filling. Compact per-frame bit maps in `floor_visibility_exclusions` carry this constraint into fusion. The same cross-view test rejects unsupported near-floor mesh faces before and after simplification. Verified horizontal foot rails can remain above the 1.5 cm floor core; a broad uncertainty band alone must not classify every horizontal wooden surface as floor.

Matrices operate on column vectors: `p_world = anchor_to_world @ camera_to_anchor @ p_camera`. `camera_to_world` records the physical pose independently; differences from that composition are reported as capture warnings because ARCore world estimates can refine during recording. Gravity in anchor coordinates comes from `anchor_to_world.rotation.T @ [0,1,0]`; anchor axes are **not assumed gravity-aligned**.

Current phone anchors are level (+Y up) and camera-facing (+Z toward the viewer). Tilted anchors remain supported. The object's yaw is chosen from the minimum-area rectangle of its gravity-plane footprint, with the longer side along local X. `object_dimensions_m` is local `[width,height,depth]`, independent of anchor yaw. The complete placement rotation remains in `object_to_anchor`.

For a depth array element at integer column/row `(i,j)`, its continuous pixel centre is `(u,v)=(i+0.5,j+0.5)`. Back-project with its own depth intrinsics:

```text
z = uint16_mm / 1000
X = z * (u - cx) / fx
Y = z * (cy - v) / fy
Z = -z
```

Map that centre to the RGB image using `to_image=[sx,sy,tx,ty]`: `image_u=sx*u+tx`, `image_v=sy*v+ty`. Mapping scales must be positive and their covered rectangle must lie within the declared RGB dimensions. Sample masks/colors using the containing image pixel (`floor(image_u)`, `floor(image_v)`). Intrinsics describe continuous centre coordinates. The depth and RGB rays must correspond under this affine mapping. No display/sensor rotation is applied by the Mac. Capture synchronized depth observations and preserve their actual timestamps rather than relabeling reprojected depth as a fresh measurement.

## Jobs and outputs

`GET /v1/jobs/{job_id}` returns:

```json
{"state":"queued","progress":0,"message":"Queued","error":null,
 "outputs":{"object_glb":null,"background_glb":null,"report":null}}
```

States: `queued`, `validating`, `masking`, `reconstructing_object`, `reconstructing_background`, `texturing`, `exporting`, `done`, `failed`, `cancelled`. Progress is a finite number from 0 to 1. One persistent queue dispatches one reconstruction subprocess at a time. `POST /v1/jobs/{job_id}/cancel` requests cancellation; the manager stops the active subprocess group, including Swift if running. Files are downloadable only after `done`, at the returned relative URLs under `/v1/jobs/{job_id}/files/`. Unknown IDs return 404. Reusing a capture ID returns 409. Interrupted nonterminal jobs become `failed` when the service restarts; they are never presented as successful or silently resumed.

Everything is persisted under `data/captures/<capture_id>/`: `inputs/`, `masks/`, `intermediate/`, `outputs/`, `job.json`, `events.jsonl`, and `worker.log`. Writes of job state are atomic. The worker also records its PID, progress, and any failure for inspection. Review masks and the contact sheet before trusting a result.

Both GLBs are binary glTF 2.0, exactly one identity-transform node, one mesh, one triangle primitive, float32 POSITION/NORMAL/TEXCOORD_0, uint32 indices, and one embedded 2048×2048 base-color PNG. No Draco, meshopt, KTX2, or external resources. At most 50,000 triangles per mesh.

`object.glb` has a gravity-aligned +Y up frame with origin at its bottom centre and units in metres. **Apply `report.object_to_anchor`** (16 column-major values) to return it to its captured position. `background.glb` vertices already use the **anchor frame** in metres; use no extra object transform. Texture-space completion is baked on real reconstructed/locally extended 3D support geometry. It does not promise correct hidden geometry from every viewpoint or replace the room image with a screen-space effect.

`report.json` records frame usage, per-frame mask coverage/recovery, method, registration scale/RMS, object dimensions, placement transform, triangle counts, observed/inpainted fractions, stage timings, and capture/quality warnings. An inpainted background fraction over 0.40 is explicitly low quality. Missing support surfaces cause a clear failure rather than invented distant geometry. Real-room quality remains unmeasured until real captures are supplied.

`object_geometry_completeness` compares the final mesh with the refined extent in the same oriented frame. An axis covering less than 80% with more than 3 cm missing, or any absolute shortfall over 5 cm, sets `low_quality=true` and an object warning. Object texture quality also contributes to the overall flag. `object_depth_comparison.geometry_evidence` records accepted depth observations and mesh-vertex counts through TSDF extraction, extent clipping, component validation and floor cleanup. Counts use the gravity plane through the capture anchor as their reference; `above_reference_plus_5cm` avoids seat thickness for a seat-level chair anchor. Observation counts and mesh-vertex counts are different quantities, and texture seams can duplicate exported vertices. `object_simplification` records before/after height evidence separately. See the [real-chair regression](../fixtures/real_chair_capture_v1/README.md).

Over 200 retained depth-supported components also flags extreme fragmentation; this is a conservative prototype quality heuristic, not proof that fewer components form a complete object. `scripts/audit_capture.py <job-folder> --write-report` applies the current guard to a saved GLB/report, preserving `report.before_geometry_audit.json`. It does not reconstruct or alter the mesh.

The report also ray-tests the patch behind the reconstructed object from up to eight scan cameras shifted by `[0.03,0.01,0.02]` metres in anchor coordinates. Less than 98% coverage in any tested view sets `low_quality=true`. This is a sampled geometry check using the reconstructed target, not proof of correct unseen geometry or appearance. Plane extension is limited to 20 cm from measured support.

`extent_refinement` records support planes, the rejection distance, robust box (0.1%–99.9% quantiles plus a 4 mm margin), basis, and evidence fractions. Mask filling, TSDF volume sizing and clipping use this oriented refined box, never the seed box. `fused_object_points_outside_seed_fraction` and `fused_object_points_outside_refined_fraction` count accepted masked depth observations after support rejection, before final TSDF extent clipping; repeated observations across frames are counted separately. The refinement section separately records fractions for its connected evidence set before mask filling. Significant evidence beyond the seed box sets `seed_was_truncating` and adds a warning. Background fusion excludes non-support depth inside the refined object region so missed base edges cannot become replacement geometry.

## Phone rendering integration

Keep the ARCore selection anchor alive during capture and use its current tracked pose when displaying results. With column vectors, the captured object's model-to-world transform is `current_anchor_to_world @ report.object_to_anchor`; the background uses `current_anchor_to_world`. Moving an object replaces its placement transform but preserves its metre scale and gravity-up convention. GLB node transforms are identity because placement is supplied separately.

For removal, draw the background patch over the live camera image where the selected object was. The original physical object's real-depth samples must not occlude its replacement patch in that selected removal region. Preserve occlusion from unrelated foreground objects and the remainder of the room. Merely loading the background mesh into an ordinary real-depth occlusion pass would leave the original object visible. The phone rendering implementation and device verification are owned separately.

## Geometry telemetry

`POST /v1/geometry` accepts a JSON object with safe `session_id` and `selection_id`; all fields are stored under `data/geometry/<session_id>_<selection_id>.json`. Corrections atomically replace that selection's record. `GET /v1/logs` adds a `geometry` file list alongside diagnostics and drift.

### Image-based object reports

Real image-built objects report `method: "apple_masked_photogrammetry"` and an `image_reconstruction` section with actual camera registration, surface closure, watertightness, silhouette recall/precision and per-stage counts. No object TSDF is required; the root fused-depth fractions are null when it was not run. Texture visibility uses the reconstructed mesh and recovered photo cameras, rather than raw-depth agreement. `adaptive_depth_fallback` explicitly identifies the coarse confidence-filtered path and always flags low object quality. A repair that discards significant photo extent is rejected.

For these real object workflows, `quality_scope: "object"` makes `low_quality` describe the item intended for My stuff; `object_low_quality` and `background_low_quality` are also reported separately. Background-removal diagnostics remain available. Synthetic removal fixtures retain `quality_scope: "object_and_background"` and their original acceptance checks.
