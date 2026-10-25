# Single-view seed prior: fix and measured regression results

Measured on 24 October 2026 on the Apple M5 Mac (16 GiB, Python 3.12.13, PyTorch 2.14.1, Open3D 0.20.0). Android and the supplied fixture files were not modified. The phone-packaged fixture remains synthetic; these results do not establish real-room quality.

## Changed behaviour

The seed bbox is only a recovery prompt. Before mask filling or mesh clipping, the service collects multi-view SAM-masked depth, fits RANSAC support planes to unmasked surrounding depth, excludes observations within 1.5 cm of those planes, and selects the spatial component connected to approved seed depth. A robust oriented extent uses 0.1%–99.9% point quantiles plus a 4 mm margin. TSDF sizing, mask filling and clipping use that extent. Object yaw comes from the minimum-area footprint rectangle, with gravity +Y preserved for level camera-facing and tilted anchors.

Support candidates require broad measured area relative to robust seed dimensions, so small object faces do not automatically become support planes. This is a geometric heuristic; unusual furniture/support configurations still need real captures. Background fusion also excludes missed non-support depth within the refined object region, preventing object base edges from appearing in the removal patch.

The report records fitted planes, refinement parameters, depth-observation fractions outside both boxes, and a truncating-seed warning. Fractions count repeated frame observations, not unique mesh vertices. Raw model masks are retained separately from geometry-augmented masks.

## Real SAM phone-fixture run

Input: the manifest and archive from `fixtures/phone_capture_e2e`, unchanged, unpacked for the offline runner. Official SAM 2.1 tiny video and the full native-attempt/TSDF/texturing/export pipeline ran. Ground truth was used only for independent output assessment, never reconstruction. The Python assessor implements the README's metric checks and the phone's additional removal-region check; the Android Gradle test itself was not run.

An MPS video attempt returned empty masks on **33/35 non-seed frames**. The same official model was automatically repeated on **CPU**, where it propagated without box recovery. This device retry is explicit in the report; no geometric test backend was used. LaMa subsequently ran on **MPS**. RealityKit attempted reconstruction and failed with `processError`; the recorded metric TSDF fallback produced the object.

| Acceptance measurement | Actual result | Target |
| --- | --- | --- |
| Object dimensions, width × height × depth | **40.867 × 28.721 × 30.870 cm** | Truth 40 × 30 × 30 cm |
| Maximum horizontal world-bound error | **4.394 mm** | ≤15 mm |
| Top world-bound error | **1.224 mm** | ≤15 mm |
| Bottom above true table height | **14.012 mm** | ≤25 mm |
| True top corners reached | **4/4**, worst distance **2.435 mm** | 4/4 within 20 mm |
| Bottom-centre placement error | **14.012 mm** | <30 mm |
| Footprint rays hitting table support | **100/100** | ≥95/100 |
| Footprint rays blocked by patch inside object | **0/100** | 0 |
| Patch vertices strictly inside true object | **0** | 0 |
| True corners inside phone removal region | **8/8** | 8/8 |
| `low_quality` | **false** | false |
| Fused observations outside seed bbox | **11.940%** | Reported, truncation warning present |
| Fused observations outside refined bbox | **0%** | Reported |

The height/placement difference includes the deliberate 1.5 cm support rejection band. It is not concealed by shifting the mesh back down. The anchor-to-world and object-to-anchor transforms place the exported geometry at its measured position.

Total engine processing: **161.524 s**, including the failed MPS attempt and CPU repeat. Stage timings: masking/refinement 110.398 s; object TSDF 0.426 s; RealityKit attempt 2.616 s; background TSDF 0.843 s; background completion 1.235 s; object texture 35.666 s; background texture/LaMa 9.554 s; export 0.388 s; shifted-view validation 0.140 s. This is a single integration run during development, not a throughput benchmark.

Both GLBs passed the minimal phone contract, each with **49,999 triangles** and one embedded 2048² PNG. Background surface texels were **69.630% observed / 30.370% inpainted**. The eight sampled shifted views each had 100% patch coverage behind the reconstructed target; this does not guarantee correct unseen appearance or every possible viewpoint.

Local artifacts:

```text
segmentation-service/data/phone-refined-sam-verified/outputs/report.json
segmentation-service/data/phone-refined-sam-verified/outputs/phone-acceptance.json
segmentation-service/data/phone-refined-sam-verified/outputs/validation.json
segmentation-service/data/phone-refined-sam-verified/outputs/{object.glb,background.glb}
segmentation-service/data/phone-refined-sam-verified/masks/{raw_masks/,contact_sheet.jpg}
segmentation-service/data/phone-refined-sam-verified.log
```

Recheck the saved output from `segmentation-service/`:

```bash
.venv/bin/python scripts/assess_phone_capture.py data/phone-refined-sam-verified \
  --truth ../fixtures/phone_capture_e2e/ground_truth.json
.venv/bin/python scripts/inspect_capture_outputs.py data/phone-refined-sam-verified
```

## CPU regressions

The six geometry runs use the explicit seed-connected depth test backend, keeping model downloads/inference out of CPU regression tests. Three additional tests use simulated SAM depth evidence deliberately contaminated with table masks, verifying support rejection independently of the geometric fallback. A further test verifies the MPS-to-CPU official-model retry.

| Anchor | Seed prior | Reconstructed W × H × D (cm) | Outside seed | Outside refined | Placement error |
| --- | --- | --- | ---: | ---: | ---: |
| Level, yawed | Tight | 40.866 × 28.768 × 30.872 | 0% | 0% | 13.586 mm |
| Level, yawed | Loose into table | 40.866 × 28.768 × 30.872 | 0% | 0% | 13.586 mm |
| Level, yawed | Undersized | 40.866 × 28.768 × 30.872 | 97.145% | 0% | 13.586 mm |
| Tilted 0.31 rad | Tight | 40.842 × 28.848 × 30.860 | 0% | 0% | 12.678 mm |
| Tilted 0.31 rad | Loose into table | 40.842 × 28.848 × 30.860 | 0% | 0% | 12.678 mm |
| Tilted 0.31 rad | Undersized | 40.842 × 28.848 × 30.860 | 92.257% | 0% | 12.678 mm |

All six runs reached all four top corners, placed all eight corners inside the removal region, hit support on 100/100 rays, and had zero interior patch hits/vertices. Changing only the seed prior leaves their measured geometry unchanged. The tests also assert no actually fused object point lies within the support rejection band and dimensions remain within 2 cm of truth.

Full service suite: **106 passed in 107.69 s**, with one upstream TestClient deprecation warning. Evidence: `segmentation-service/data/service-tests-refinement-final.txt` and `segmentation-service/data/extent-cpu-regression-metrics.json`. Python compilation succeeded. The idle Mac service was restarted with these fixes; its health endpoint still describes the separate tap-segmentation model, while each capture report states its actual video/inpainting devices.
