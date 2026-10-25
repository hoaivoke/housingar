# Real Pixel chair: floor, backrest and quality regression

The fix uses the phone's calibrated depth and tracked poses for metre scale. Manual dimensions are not required. Android source was not modified.

## What failed

Capture `d7ce4323_obj2_70861434` has 50 sensor-orientation 1920×1080 frames and 160×90 raw depth. Its original SAM masks contained backrest depth in **all 50 frames**, totaling **37,651 observations above the seat region**. The original TSDF contained 30,320 backrest vertices; extent clipping left 30,247. Largest-component-only cleanup then removed **every one**. Global simplification also tended to erase small remaining islands.

Ten RANSAC strips at offsets 0.36–0.45 m represented one noisy floor. Treating each strip as a support left floor between bands and removed legitimate lower-leg evidence. A normal-only mesh filter was insufficient: noisy floor debris also forms tilted triangles.

## Implemented behaviour

- Merge near-parallel support slices; fit a median offset and measured spread. This capture now has **one floor merged from ten slices**, with a **6.92 cm global half-band** and 50 local floor observations.
- Use other camera views' original SAM silhouettes to reject ambiguous near-floor depth, respecting depth occlusion and a 1.5 cm projection margin. Rejected depth cannot return through geometric filling. Recheck near-floor mesh faces before/after simplification; horizontal wooden foot rails are not rejected solely by a broad floor band.
- Keep TSDF components supported by at least two independent depth views, rather than only the largest. Allocate simplification budgets per supported component, preserving small backrest/leg pieces within the export limit.
- Compare final height/footprint with the refined extent. More than 20% and 3 cm shortfall, or any shortfall over 5 cm, flags low quality. Extreme fragmentation also flags low quality.

## Fresh official-SAM CPU result

The final run recomputed official SAM 2.1 Tiny video on CPU, then used metric TSDF and OpenCV texture completion; native Object Capture was explicitly disabled for this geometry check. It took **105.89 s** on the previously documented M5/16 GiB environment. No GPU performance is claimed.

The output is **83.95 × 75.15 × 62.59 cm** in the contract's long-horizontal-axis × height × short-horizontal-axis convention. The original height was **36.52 cm**. Final vertices span anchor gravity heights **−41.18 to +33.98 cm**, retaining the backrest and lower-leg structure. Height covers **96.62%** of the refined depth extent; footprint coverage is **99.71%**.

**This remains `low_quality=true`**: fusion needed 2,587 disconnected depth-supported components. The observed parts survive, but sparse/noisy raw depth still produces fragmented surfaces. Coarser 6/8/10 mm voxel controls reduced component counts while losing more backrest height, so the finer measured geometry was retained. No unseen geometry was invented to make the model appear complete.

The GLB passes the phone contract: **47,012 triangles**, one embedded **2048×2048** texture, identity node and bottom-centre metre frame. All **256 fixed floor-debris negative samples** from the old mesh are at least **3.48 cm** from the corrected surface. Those samples lie outside the chair outline in at least 90% of ten or more clearly unoccluded views, with a conservative 3 cm silhouette margin. This tests tilted floor debris as well as horizontal floor faces.

### Counts above the seat region

The reference is anchor gravity height 0; the threshold is **+5 cm** to avoid counting seat thickness as backrest. Depth observations and mesh vertices are different quantities. UV seams duplicate exported vertices.

| Stage | Original | Corrected | Quantity |
| --- | ---: | ---: | --- |
| Raw SAM-selected depth | 37,651 | 37,651 | Depth observations |
| Filled, support-rejected, extent-clipped depth | 37,924 | 37,901 | Depth observations |
| Extracted TSDF | 30,320 | 43,628 | Mesh vertices |
| After extent clipping | 30,247 | 43,457 | Mesh vertices |
| After component validation and floor cleanup | **0** | **39,741** | Mesh vertices |
| After simplification | **0** | **9,804** | Mesh vertices |
| Final textured GLB | **0** | **23,987** | Vertices including UV seams |

Actual full diagnostics and assets are under `segmentation-service/data/chair-regression-silhouette/result/outputs/`: `object.glb`, `report.json`, `chair-regression.json`, and `point-stages-per-frame.json`. Original inputs/outputs remain available. The old bad capture's report was reassessed as low quality, preserving `report.before_geometry_audit.json`; its mesh was not silently replaced.

## Regressions

- **177 tests passed** in **203.57 s**. The new real-capture fixture requires no model download: [fixture and targets](../fixtures/real_chair_capture_v1/README.md). It checks floor merging, view occlusion, serialized exclusions, backrest/leg retention, simplification, fixed floor-debris samples, and quality guards.
- **Phone-packaged fixture, fresh official SAM CPU:** passed every README target: world bounds, four top corners, placement, footprint support, no patch inside the object and quality. Detailed output: `data/chair-regression-phone-verified/result/outputs/phone-acceptance.json`.
- **120-frame, four-object synthetic scan:** all four pass; worst dimension error **1.241 cm**, worst placement error **1.482 cm**. Reconstruction was rerun with verified saved official-SAM tracks; video inference was not retimed. Output: `data/chair-regression-scan-verified/result/assessment.json`.

The source fixture is the supplied physical capture; the regression establishes preservation and rejection behaviour, not guaranteed reconstruction of dark or unobserved wooden surfaces.
