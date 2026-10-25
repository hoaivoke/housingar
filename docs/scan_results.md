# Scan → proposals → My stuff: measured results

24 October 2026. This implements the revised project direction on the Mac. Android files and the supplied phone fixture were not modified. These are synthetic results on Apple M5, 16 GiB RAM, macOS 27.0.1, Python 3.12.13, PyTorch 2.14.1 and Open3D 0.20.0. Real-room reconstruction and phone placement quality remain unmeasured.

The [scan/library/layout contract](scan_v1.md) is the phone integration handoff. The [service README](../segmentation-service/README.md) covers installation, configuration and the repeatable HTTP benchmark.

## Delivered behaviour

- Versioned scan upload shares capture frame/file validation, with a 600 MB compressed limit, 30–1000 frames, no seed mask, and a level scan anchor.
- One queue serializes scans and individual captures; work runs in isolated processes, with progress, cancellation, durable states and restart recovery.
- Confidence-weighted scene TSDF and observed zero-distance samples drive geometric discovery. The measured floor and boundary walls are removed, while desk/chair surfaces remain.
- Furniture/support splitting yields an independent desk proposal and child box/cylinder proposals, including the touching pair. Official SAM uses projected boxes, positive points and other-candidate negative points. Room planes and other objects are excluded from each candidate. Connected furniture structure can retain legs/back omitted by an image prompt; reports record this completion.
- Metric masked TSDF, footprint yaw, bottom-centre pivots, view-selected textures and outlined JPEG thumbnails produce phone-compatible object GLBs. Unmeshable proposals remain listed with null meshes, low quality and reasons.
- Explicit confirmation copies assets into persistent My stuff; rename/delete and saved layouts work. Individual capture jobs can also be confirmed. The scan pipeline creates no removal background and runs no learned inpainting. Legacy capture outputs remain compatible.

## Actual HTTP integration run

The CPU raycast generator produced **120 walking-path frames**, each 320×240, with 140×108 cropped depth, mapping `[2,2,20,12]`, confidence varying from 220 to 255, poses and intrinsics. The scene contains floor/walls, a desk, a touching box/cylinder pair on it, and a separate chair-like object. The scan anchor has yaw/translation independent of the objects. Ground truth was outside the upload ZIP and used only by the independent assessor and synthetic demo naming script.

Upload: `scan_http_sam_verified`; job: `9427d4c7ea0c42818c085e06a23fb22b`.

| Measurement | Actual |
| --- | ---: |
| HTTP upload/validation to 202 | **0.172 s** |
| Engine processing | **136.063 s** |
| Polling until done | **136.647 s** |
| Maximum sampled health latency during processing | **15.54 ms** |
| Depth observations | **1,814,035** |
| Confidence-weighted TSDF voxels | **4,233,251**, 8 mm voxels / 24 mm truncation |
| Weighted observed surface voxels | **798,316** |
| Removed room planes | **1 floor + 4 walls** |
| Proposals / successful meshes | **4 / 4** |

Stage times: scene fusion **6.751 s**, discovery **0.838 s**, SAM masks/geometry ownership **82.776 s**, meshing **12.074 s**, textures/export **33.513 s**. This is one development integration run, not a throughput distribution.

The model was official **SAM 2.1 tiny, actually on CPU**, using image box/point prompts on up to 12 angular sectors per object. No geometric test backend was used in this run. All four methods were `depth_fusion`; optional RealityKit was disabled and no native reconstruction performance is claimed. New-scan texture completion uses explicit OpenCV only, with no LaMa/removal job.

| Proposal | Measured W × H × D (cm) | Largest dimension error | Bottom-centre placement error |
| --- | --- | ---: | ---: |
| Desk | **120.857 × 75.762 × 75.857** | **8.574 mm** | **10.274 mm** |
| Box | **32.872 × 28.192 × 25.881** | **8.812 mm** | **2.125 mm** |
| Cylinder | **18.412 × 24.167 × 18.168** | **4.124 mm** | **4.282 mm** |
| Chair | **46.870 × 94.257 × 44.867** | **8.697 mm** | **10.033 mm** |

Every dimension passes the **2 cm** target and every placement passes **3 cm**. Local X follows the longer footprint side, so the chair's local width/depth correspond to its 46/44 cm footprint rather than world X/Z. The desk mesh contains furniture structure without its child items; box and cylinder parent_id both reference the desk. Naming above comes from the synthetic oracle for test display, not automatic semantic recognition.

| Proposal | Visible geometry views | Angular coverage | Observed surface texels | Triangles |
| --- | ---: | ---: | ---: | ---: |
| Desk | 120 | 356.25° | 97.423% | 20,000 |
| Box | 120 | 355.58° | 98.724% | 19,999 |
| Cylinder | 100 | 283.85° | 96.933% | 17,862 |
| Chair | 120 | 354.08° | 98.699% | 20,000 |

All four report `low_quality=false`. Appearance/SAM used 12, 12, 11 and 12 selected views respectively. Observed fraction excludes UV padding; high observed texture fraction does not prove complete unseen geometry. Each downloaded GLB passed the minimal loader checks: one identity node, one mesh/triangle primitive, float32 position/normal/UV, uint32 indices, one embedded 2048² PNG, no compression/extensions, fewer than 50k triangles. Thumbnail JPEGs were downloaded and checked at ≤512 px.

## Persistence and verification

The HTTP benchmark explicitly confirmed four **Synthetic demo** library items, downloaded their copies, exercised a temporary item's rename/delete, and saved a four-placement layout. The scanner itself never confirms items. After a physical service restart, the job/proposals and all four copied assets remained available; mesh bytes matched the original downloads, thumbnails answered, and the saved layout matched. A separate existing synthetic individual-capture job was also successfully confirmed into My stuff with a seed-frame thumbnail.

**129 tests passed in 142.35 s**, with one upstream TestClient deprecation warning. Coverage includes common frame/scan validation, unsafe ZIP/missing/size errors, nonbinary confidence weighting, serial mixed scan/capture jobs, cancellation/restart, every synthetic object and parent link, dimensions/placement/GLB constraints, numeric SAM mask dtype and prompt plumbing, explicit unmeshable proposals, library CRUD/independent copies/restart, individual-capture confirmation and layout validation/persistence. Python compilation and dependency consistency checks passed.

Individual capture seed-prior/yaw handling remains fixed. The full suite retains its tight/loose/undersized and tilted-anchor regressions. Rechecking the saved real-SAM phone fixture with the current assessor still passes all README targets: 4/4 corners, worst top-corner distance 2.435 mm, placement 14.012 mm, support 100/100 rays, zero interior patch geometry, low_quality false. See [the original seed-prior measurements](seed_extent_results.md); this recheck does not claim a newly captured real room or a new Android device run.

Saved evidence under `segmentation-service/data/`:

```text
scan-synthetic-v1/{manifest.json,ground_truth.json}
scan-synthetic-v1.zip
scan-http-sam-verified/{benchmark.json,proposals.json,object_*/object.glb,object_*/thumbnail.jpg}
scan-http-sam-verified.log
scans/scan_http_sam_verified/{job.json,events.jsonl,worker.log}
scans/scan_http_sam_verified/intermediate/{scene-tsdf.npz,scene-observed.ply,discovery.json}
scans/scan_http_sam_verified/outputs/{report.json,proposals.json,object_*/report.json}
scan-library-restart-verification.json
service-tests-scan-verified.txt
scan-sam-dtype-regression.txt
scan-persistence-regression.txt
```

## Practical limits

Geometric room-plane and support splitting are heuristics. Unusual furniture, shared surfaces, thin/reflective/transparent objects, depth holes and weak coverage can merge, miss or distort objects. Confirm/reject proposals and capture missed items individually; no promise of real-room completeness follows from this synthetic scene. Phone UI/device integration is owned separately and was not verified in this run. Saved world poses also need alignment to reuse in another AR session.

The Mac service is running on port 8000 with the new APIs. Its `/health` describes the separate tap-segmentation backend (SAM tiny on MPS); scan per-proposal reports state actual CPU inference.
