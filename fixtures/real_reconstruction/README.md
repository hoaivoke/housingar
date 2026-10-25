# Real Pixel image-geometry regressions

Source recordings, all supplied locally by the user:

- `segmentation-service/data/scans/6fb50237_scan_93413134`: black chair, 55 views.
- `segmentation-service/data/scans/baae385b_scan_91807239`: second chair, 60 views.
- `segmentation-service/data/captures/d7ce4323_obj2_70861434`: wooden chair, 50 views.

Each NPZ contains the **actual registered masked Apple photogrammetry surface** before repair (`vertices`, `faces`) and the original sensor-resolution SAM masks packed along image columns (`masks`). Its JSON contains original VIO calibration, recovered texture cameras, metric registration, input manifest/RGB/mask SHA256 and measured expected dimensions. No RGB photos or learned weights are required by these CPU tests. These files are recorded outputs, not synthetic geometry or a substitute for executing photogrammetry.

`tests/test_image_reconstruction.py` substitutes only the hardware-dependent native call. It executes the real repair/closure, mask-supported joining, metric frame, and all-view silhouette validation. Targets: one watertight component, no self-intersection, ≤50k triangles, dimensions within 2 cm of the recorded repaired result, median RGB recall >90%, lower-region recall >70%, upper-region recall >90%. The black-chair test must reject a destructive general repair and use bounded surface closure. A separate test squashes the wooden chair and requires a quality failure.

For a fresh native reconstruction and texture measurement on all three original recordings:

```bash
cd segmentation-service
.venv/bin/python scripts/regress_real_reconstruction.py --output data/new-image-regression
.venv/bin/python scripts/render_reconstruction.py data/new-image-regression \
  --output ../docs/assets/new-image-regression
```

The runner reuses the existing official SAM video masks, hashes inputs, preserves original jobs, and reruns native geometry, metric registration, repair, texture and GLB export. It does **not** report fresh SAM inference time. See [results and limitations](../../docs/image_reconstruction_results.md).
