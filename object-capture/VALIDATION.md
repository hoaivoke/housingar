# Validation — October 24, 2026

Scope: new `object-capture/` files only. Android, shared application files, root scripts, and the existing segmentation-service implementation were not modified for this work.

- Native Swift command compiled successfully with the installed macOS SDK, targeting macOS 13+.
- Read-only support probe on this Apple M5 Mac returned `PhotogrammetrySession.isSupported = true`.
- **28 object-capture tests passed**, covering the compiled Swift photo validator/arguments, existing-output and source/symlink protection, real JPEG decoding, synthetic video decoding/frame selection, orientation, rejected frames, insufficient captures, and configuration errors.
- Combined regression suite: **84 passed in 3.13 seconds** (56 existing segmentation/USB tests plus 28 new tests). One existing Starlette TestClient deprecation warning remained.
- Shell syntax check passed for `build.sh`.
- The existing server still answered `{"status":"ok","backend":"sam2","model":"facebook/sam2.1-hiera-tiny","device":"mps"}` after the work.

No complete photogrammetry job was started. This avoided competing with the live MPS service or other workers' device tests. The tests' generated photographs and video establish preparation/validation behavior, not reconstruction quality.

The remaining evidence needed for the personal-object milestone is a successful reconstruction from real overlapping object views, followed by scale/pivot/orientation validation, USDZ-to-GLB conversion, and rendering the same calibrated asset on Android and in the shared room viewer. Supplied physical dimensions are retained as metadata; this command does not yet apply them as mesh calibration.
