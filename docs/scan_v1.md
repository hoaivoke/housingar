# Scan, proposals, library and layouts v1

The 24 October revision in PROJECT_PLAN.md makes scan → proposals → My stuff → real-size placement the core flow. New scans do not reconstruct a removal background or run learned inpainting. Individual `/v1/captures` uploads remain compatible, including their existing output contract and seed-prior refinement.

New phone scans use [tap-selected scan v2](scan_v2.md), which builds exactly the selected things with SAM video. This document remains the unchanged v1 upload contract and the shared proposal/library/layout contract.

## Scan upload

`POST /v1/scans` is multipart/form-data with `manifest` JSON text and `archive` ZIP. HTTP 202 returns `{"scan_id":"...","job_id":"..."}` after validation/persistence, before reconstruction. A reused scan ID returns 409. Invalid JSON/ZIP returns 400, invalid fields/files 422, size limits 413. IDs match `[A-Za-z0-9_-]{1,64}`.

```json
{
  "schema": "housingar.scan.v1",
  "scan_id": "belongings_001",
  "session_id": "home_001",
  "label": "My room",
  "units": "meters",
  "conventions": "camera: +X right, +Y up, looks down -Z (ARCore/OpenGL). image: origin top-left, +y down. matrices: 4x4 column-major. 'anchor' = ARCore anchor created at selection; world +Y is up (gravity).",
  "anchor_to_world": [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1],
  "frames": []
}
```

`frames` above is abbreviated: **30–1000 frames are required**, typically 100–400. Each is EXACTLY the frame object in [capture_v1.md](capture_v1.md), including RGB/depth intrinsics, poses, timestamps, cropped pixel-centre mapping, confidence (optional), sharpness and orbit_deg. The same frame validator is used. The canonical conventions string above is required. The scan anchor must be level (+Y world up); arbitrary yaw and translation are supported. RGB images remain in sensor orientation. All transforms are rigid, finite, 4×4 column-major, all distances metres.

ZIP entries are `frames/*.jpg`, `depth/*.bin`, and optional `confidence/*.bin`, with identical byte/format rules to capture v1. There is no seed mask. The multipart manifest is authoritative; offline folders contain `manifest.json`. The compressed limit is **600,000,000 bytes**, uncompressed limit **1,800,000,000 bytes**, and at most 4096 entries. Traversal, absolute/noncanonical paths, duplicates, symlinks, encryption, special files and missing/size-mismatched references are rejected. Ground truth is never an input to reconstruction.

## Jobs

Scans and captures share ONE persistent queue/worker and `/v1/jobs/{job_id}`, `/cancel`. Scan states: `queued`, `validating`, `fusing_scene`, `finding_objects`, `segmenting_objects`, `meshing_objects`, `texturing`, `exporting`, `done`, `failed`, `cancelled`. Progress is 0–1, with message and nullable error. Heavy processing runs in an isolated subprocess; cancellation stops its process group. An interrupted nonterminal job becomes failed on restart. Outputs are unavailable before done. For done scans:

```json
{"state":"done","progress":1,"message":"...","error":null,
 "outputs":{"proposals":"/v1/scans/belongings_001/proposals"}}
```

Artifacts persist under `data/scans/<scan_id>/`: archive, inputs, job/events, scene TSDF/cloud, discovery, masks, per-proposal meshes/textures/thumbnails/reports and outputs. Partial candidate failures are retained as proposals with null meshes; a scan-level failure remains a failed job.

## Proposals and coordinates

`GET /v1/scans/<scan_id>/proposals` returns:

```json
{"scan_id":"belongings_001","proposals":[{
  "proposal_id":"object_001","parent_id":null,
  "thumbnail_url":"/v1/scans/belongings_001/proposals/object_001/thumbnail.jpg",
  "mesh_url":"/v1/scans/belongings_001/proposals/object_001/object.glb",
  "dimensions_m":[1.2,0.75,0.7],
  "object_to_anchor":[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1],
  "method":"depth_fusion",
  "quality":{"views":20,"coverage_deg":280,"observed_fraction":0.8,
             "low_quality":false,"warnings":[]}
}]}
```

Numbers illustrate the schema, not measured performance. All URLs are service-relative. `failure_reason` is added for unmeshable proposals and also appears in quality warnings. A thumbnail is JPEG, longest side ≤512 px, cropped from the best captured view with an outline.

Discovery removes measured floor and boundary wall planes while retaining above-floor horizontal furniture surfaces. A furniture cluster is split at its tabletop/support into a furniture proposal and child item proposals. The furniture mesh excludes its children, allowing a user to keep furniture and items independently; parent_id is a discovery relationship, not a transform hierarchy. Geometry is only proposed: the user supplies the final name/confirmation. Plane/clustering heuristics can miss or merge unfamiliar or poorly observed objects; failures/quality warnings must remain visible.

Each GLB obeys the [capture phone loader constraints](capture_v1.md): one identity node/mesh/triangle primitive, float32 POSITION/NORMAL/TEXCOORD_0, uint32 indices, one embedded 2048² PNG, no external/compressed resources, ≤50k triangles. Origin is bottom-centre, +Y gravity up, units metres; minimum-area footprint rectangle chooses yaw with its longer side on local X. `dimensions_m=[width,height,depth]`. `object_to_anchor` places it in the scan anchor; the phone uses `current_anchor_to_world @ object_to_anchor` for scan review. A library mesh is independently placed in the new room at its metre scale.

Quality reports visible depth keyframes contributing geometry, angular coverage (360° minus largest camera-azimuth gap), and observed surface-atlas texel fraction, excluding unused UV padding. Appearance/SAM use a coverage-selected subset, recorded separately per proposal. Once SAM validates furniture, its connected measured structure can complete legs/back/seat omitted by a sparse prompt; this structural completion is recorded, with room planes and other candidates excluded. Missing views, poor orbit coverage, significant unseen texture and failed operations produce explicit warnings/low_quality. Unobserved texels can receive explicit OpenCV texture-space completion; no removal model is produced. Per-proposal reports record actual SAM device/backend, geometry method and any optional native attempt.

## Persistent My stuff library

- `POST /v1/library` JSON: `{"name":"My desk","source":{"scan_id":"...","proposal_id":"..."}}`, or `{"name":"My box","source":{"job_id":"<completed capture job>"}}`.
- Returns the created item. Copies the validated local GLB and thumbnail under `data/library/<item_id>/`; later scan/source removal cannot affect the copy. A capture thumbnail is cropped/outlined from its seed frame. Null/unfinished/failed source meshes are rejected with 409.
- `GET /v1/library` → `{"items":[{"item_id","name","dimensions_m","mesh_url","thumbnail_url","source","method","low_quality","created"}]}`.
- `PATCH /v1/library/<item_id>` with `{"name":"New name"}` renames it; names are trimmed, 1–80 characters.
- `DELETE /v1/library/<item_id>` deletes the item and its copied files. Unknown IDs return 404.
- Item URLs: `/v1/library/<item_id>/object.glb` and `/thumbnail.jpg`; only these files are served.

Library writes are atomic and survive service restarts. No item is added automatically by discovery: explicit confirmation is required through POST.

## Layouts

`POST /v1/layouts` JSON:

```json
{"layout_id":"apartment_001","session_id":"visit_001","name":"Living room",
 "placements":[{"item_id":"<library item>","pose_world":[1,0,0,0,0,1,0,0,0,0,1,0,1,0,2,1]}]}
```

The item must exist at save time. `pose_world` is a finite rigid 4×4 column-major model-to-current-room-world transform; no hidden rescaling. Names use the same 1–80 character rule. Layout/session IDs use the safe ID rule. Saves replace the same layout_id atomically under `data/layouts/`. Response is the saved layout; `GET /v1/layouts` → `{"layouts":[...]}`. Deleting a library item does not rewrite historical layouts. Saved world poses require alignment/relocalization to reuse in a later physical AR session.
