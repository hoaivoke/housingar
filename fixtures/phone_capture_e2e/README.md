# Phone-packaged capture fixture (capture_v1)

A ground-truth synthetic scene (the Mac's own generator: 40 × 30 × 30 cm textured box on a table,
cylinder, walls; 36 frames, 320×240) **re-packaged by the Android app's code**: the phone's
`CaptureFormat` manifest/zip writer, the phone's level camera-facing selection anchor
(`AnchorFrames`), and the phone's single-view seed box (`SeedExtent`). Upload it exactly as the
phone does:

```
curl -F "manifest=<manifest.json;type=application/json" -F "archive=@archive.zip;type=application/zip" http://127.0.0.1:8000/v1/captures
```

`ground_truth.json` is in world coordinates (`manifest.anchor_to_world` maps anchor → world).
`phone_e2e_result.json` is what the phone measured after a CPU test-mode run
(`CAPTURE_MASK_BACKEND=depth_bbox CAPTURE_OBJECT_CAPTURE=0 CAPTURE_INPAINTING=opencv`).

Acceptance targets for the full pipeline (checked by `android/.../EndToEndMacTest`):
placed object's world bounds within 1.5 cm of the true box on every side (bottom may sit up to
2.5 cm high), all four true top corners reached within 2 cm, placement (bottom centre) within 3 cm,
background patch present under ≥ 95% of the footprint with no patch geometry inside the object,
and `low_quality == false`.
