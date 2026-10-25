# Measured reconstruction results — 24 October 2026

This is the initial reconstruction baseline. The seed-prior handling and yaw have since been corrected; see [the phone-fixture regression results](seed_extent_results.md) for the current implementation and measurements, including the explicit SAM CPU retry.

These are synthetic integration results on the development Mac: Apple M5, 16 GiB RAM, macOS 27.0.1, Python 3.12.13, PyTorch 2.14.1, Open3D 0.20.0. No real room, phone capture, reflective object, or successful native photogrammetry reconstruction has been measured.

## Completed implementation

The Mac service accepts versioned capture archives, validates their files/geometry, persists a single-job queue, propagates masks, reconstructs an object and a 3D background patch, bakes textures, and serves phone-compatible GLBs plus a report. Geometry telemetry is saved through `/v1/geometry` and listed with other logs. The [capture contract](capture_v1.md) specifies the phone integration; the [service README](../segmentation-service/README.md) contains installation and offline commands. [Model provenance](reconstruction_models.md) records downloads and licences.

One reconstruction subprocess runs at a time. This isolates native-library execution from the API process. Model access is serialized with tap segmentation; health, job polling, cancellation, and completed downloads remain available. Restart recovery and cancellation are persisted. Android and the separate root `object-capture/` implementation were not modified for this extension.

## Final live HTTP run

Input: 36 synthetic 320×240 JPEGs at 10-degree orbit intervals; textured 0.40×0.30×0.30 m box, separate cylinder, textured table, and surrounding room walls. Cropped depth is 140×108, uint16 millimetres, with `to_image=[2,2,20,12]` and explicit confidence. Ground truth is retained separately and is not supplied to the reconstruction worker.

Capture ID: `synthetic_http_isolated`; job ID: `8d7caad90f8a4276bcaf72116fb04d3e`. All 36 frames were used. Artifacts are retained locally under `segmentation-service/data/captures/synthetic_http_isolated/`.

| Measurement | Actual result |
| --- | --- |
| Upload/validation to HTTP 202 | 0.136 s |
| Processing engine total | 175.397 s |
| Polling until `done` | 177.016 s |
| Maximum of sampled health requests during this run | 27.2 ms |
| Mask propagation | Official SAM 2.1 tiny video, MPS |
| Recovery | Projected box/depth recovery on 35 frames; inspect contact sheet |
| Native primary | RealityKit attempted, failed with `processError` after 3.013 s |
| Exported object method | Metric masked depth TSDF (`depth_fusion`) |
| Native registration scale / RMS | null / null, because native reconstruction failed |
| Object dimensions | 0.412000 × 0.302338 × 0.311827 m |
| Absolute dimension errors | 1.200 cm, 0.234 cm, 1.183 cm |
| Triangles | Object 49,999; background 50,000 |
| Textures | One embedded 2048×2048 PNG per GLB |
| Background surface texels | 70.219% observed; 29.781% inpainted |
| Background inpainting | LaMa, actually executed on MPS, no OpenCV fallback |
| Minimum sampled reconstructed-target patch coverage | 99.917% across eight shifted scan views |
| `low_quality` | false under the documented fraction/coverage gates |

Stage timings, in seconds:

| Stage | Seconds |
| --- | ---: |
| Validate persisted capture | 0.061 |
| SAM video propagation, depth checks, recovery, mask artifacts | 118.535 |
| Object depth fusion | 0.299 |
| Native Object Capture attempt | 3.013 |
| Background depth fusion | 0.641 |
| Local supported background completion | 1.303 |
| Object UVs and texture baking | 37.292 |
| Background UVs, baking and LaMa completion | 13.242 |
| GLB export and contract validation | 0.648 |
| Background view coverage validation | 0.218 |

This is one integration run, not a latency distribution. A prior offline learned-model run completed in 115.240 s with fewer recovery prompts. The final live run above is the relevant result for the process-isolated HTTP path. Neither meets an interactive reconstruction target; the original under-three-second goal concerns a single segmentation request.

Open3D's scalable TSDF produced empty meshes on this machine, including a constant-depth control. The bounded uniform TSDF fallback was used and recorded: 4 mm object voxels and approximately 13.3 mm background voxels. TSDF edge spill was constrained to the selected bbox plus one 4 mm voxel, removing 1,531 triangles before simplification. The bbox is a geometric prior; an undersized real selection can truncate the reconstructed object. The scale result is not independent of this prior.

Final masks had mean IoU 0.96693 and minimum IoU 0.95968 against synthetic truth. These are **final geometry-constrained masks**, including depth augmentation/recovery, not raw SAM tracking accuracy. The high recovery count remains a reason to inspect masks and collect real capture evidence before relying on the model.

## 3D background check

CPU raycasting reloaded the actual exported background GLB and tested rays through the known synthetic object from three camera positions shifted by `[0.03,0.01,0.02]` metres from recorded poses:

| Source view | Covered target rays / target rays | Fraction |
| --- | ---: | ---: |
| Frame 0, shifted | 12,989 / 12,989 | 100% |
| Frame 9, shifted | 11,045 / 11,054 | 99.919% |
| Frame 18, shifted | 13,930 / 13,930 | 100% |

This checks that actual 3D patch geometry lies behind the object silhouette. It does not measure texture fidelity, prove hidden shape is correct, or guarantee every nearby view. Completion is limited to measured support and at most 20 cm of local plane extension. The phone must render the patch with the removal-region depth handling described in the contract.

## Verification and saved evidence

- Full service suite: **95 passed in 53.31 s**. Tests cover capture/ZIP validation, geometry telemetry, job sequencing, cancellation, restart recovery, official-video-predictor plumbing with a mock, actual CPU reconstruction through the subprocess/API path, metric scale, object placement/gravity, shifted-view geometry, and GLB reload/structure. Model weights are not downloaded by tests. One upstream TestClient deprecation warning remains.
- Both downloaded GLBs passed strict validation: one identity node, one mesh/primitive, float32 POSITION/NORMAL/TEXCOORD_0, uint32 indices, embedded PNG, no compression/extensions, at most 50,000 triangles. Tests also reload them with trimesh.
- A physical service restart restored the completed job and its three downloads. A separate live job was cancelled in **0.111 s**; its child was confirmed stopped and its downloads returned 409.
- Dependency consistency check passed; Python sources compiled successfully. The Swift CLI compiled and was actually invoked by the pipeline, yielding the recorded native failure rather than a claimed photogrammetry success.

Local evidence files under `segmentation-service/data/`:

```text
service-tests-final.txt
http-capture-verification.json
http-capture-verification-isolated.log
http-restart-cancel-verification.json
http-capture-downloads/{object.glb,background.glb,report.json}
captures/synthetic_http_isolated/outputs/{report.json,validation.json,synthetic-metrics.json}
captures/synthetic_http_isolated/masks/contact_sheet.jpg
captures/synthetic_http_isolated/intermediate/object-capture/swift.log
captures/synthetic_http_isolated/worker.log
```

Runtime artifacts/checkpoints remain ignored by version control. The Mac service is running at `0.0.0.0:8000`, with tap segmentation reporting official SAM 2.1 tiny on MPS. End-to-end Pixel capture/export/rendering and native camera-pose alignment still require a real capture and the separately owned phone implementation.
