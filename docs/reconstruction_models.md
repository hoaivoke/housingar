# Reconstruction model provenance

## Segmentation

Only the official [facebookresearch/sam2](https://github.com/facebookresearch/sam2) code is used, installed with `SAM2_BUILD_CUDA=0`. The video loader is `SAM2VideoPredictor.from_pretrained("facebook/sam2.1-hiera-tiny")` (or `-small`). It receives the approved seed mask, propagates in both directions, and accepts projected box prompts for recovery. Before geometric filling, multi-view masked depth refines the actual object extent, with support planes and disconnected objects excluded. The seed bbox is a prompt prior; it never supplies object depth or clips the mesh. The report distinguishes recovery and geometric augmentation from model-only tracking. Check masks/contact_sheet.jpg and masks/raw_masks/ before trusting a real capture.

On this Mac, an MPS video run can return finite `-1024` (object absent) logits throughout non-seed frames while the same model produces valid masks on CPU. If more than half of non-seed video masks are empty, the service repeats official SAM video on CPU before deriving geometry. It records `masking.device=cpu`, the requested device, and `device_fallback_reason`; this is not a switch to the geometric test backend. Tests exercise that retry. Texture completion independently records its own actual device.

## Texture completion

- Original model/source: [LaMa](https://github.com/advimman/lama).
- Source-code licence checked: [Apache-2.0](https://github.com/advimman/lama/blob/main/LICENSE).
- TorchScript distribution referenced by [IOPaint's LaMa implementation](https://github.com/Sanster/IOPaint/blob/main/iopaint/model/lama.py): [big-lama.pt](https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt).
- Upstream integrity check: MD5 `e3aa4aaa15225a33ec84f9f4bc47e500`.
- Download measured in this project: SHA-256 `344c77bbcb158f17dd143070d1e789f38a66c04202311ae3a258ef66667a9ea9`.

The code licence and checkpoint publisher are recorded separately; the release does not include a separate weight-specific licence file. This document does not infer additional checkpoint rights beyond the publisher's terms. The application does not redistribute the downloaded weights.

Default local checkpoint: `segmentation-service/.cache/inpainting/big-lama.pt`; override with `LAMA_MODEL_PATH`. The model is loaded locally with PyTorch, choosing MPS when available. `PYTORCH_ENABLE_MPS_FALLBACK=1` is set before torch import. Texture completion runs at 512×512 and is composited only into unobserved surface texels of the final 2048×2048 atlas. It does not inpaint the room photograph. Measured texels remain measured, and unused UV padding does not enter the observed/inpainted fraction.

If the checkpoint is missing, corrupt, or inference fails, the report explicitly records `backend=opencv_telea`, `device=cpu`, and the reason. `CAPTURE_INPAINTING=opencv` requests that fallback deliberately. An atlas with no missing texels reports `none_needed`, without claiming a learned-model run.

In the initial full synthetic run, SAM video and LaMa both actually ran on MPS. Object Capture failed with `processError`; metric TSDF was used. See the final measured reconstruction results for the current complete run; no claim of real-room reconstruction or inpainting quality follows from synthetic testing.

## Native reconstruction

RealityKit is supplied by macOS. `segmentation-service/reconstruction/ObjectCapture.swift` creates `PhotogrammetrySample` objects with explicit per-frame masks and gravity, requests `.poses` plus the mesh, and exports a mesh through ModelIO. ARCore depth is deliberately not supplied as an uncalibrated full-image depth map: its crop/resolution require separate handling and it is always fused in the metric TSDF comparison.

Recovered camera centres are registered to ARCore camera-to-anchor centres using RANSAC and Umeyama similarity registration. At least six correspondences and 70% inliers are required; registration RMS over 3 cm, a failed mesh conversion, or strong dimension disagreement with TSDF causes an explicit fallback. A failed primary method has null registration scale/RMS, not fabricated measurements. Full native alignment still needs verification on a real capture.
