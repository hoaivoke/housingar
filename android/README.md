# Room Inspect (Android: scan your stuff, place it in the new room)

Kotlin + ARCore app for the Pixel 9a, extended from Google's `hello_ar_kotlin` sample
(Apache 2.0; see `THIRD_PARTY_ARCORE_SAMPLE_LICENSE`). Package: `com.housingar.inspect`.

## Install on the Pixel

Prebuilt debug APK: `room-inspect-debug.apk` (in this folder).

1. On the phone: Settings → About phone → tap *Build number* 7 times, then enable
   *Developer options → USB debugging*. Connect it by USB and accept the prompt.
2. On the Mac (Android platform-tools installed, e.g. `brew install android-platform-tools`):
   ```
   adb devices
   adb install -r android/room-inspect-debug.apk
   adb reverse tcp:8000 tcp:8000     # lets the phone reach the Mac service at http://127.0.0.1:8000
   ```
3. Open **Room Inspect**, allow the camera, and install/update *Google Play Services for AR* if asked.

To build it yourself, open the `android/` folder in Android Studio (JDK 17+). The build uses
Gradle 8.14.3, Android Gradle Plugin 8.13.2, Kotlin 2.1.21, compileSdk 36 and ARCore 1.57.0.

## What's on screen

- **Scan stuff:** film your belongings; the Mac turns the video into separate 3D objects.
- **My stuff:** your saved objects. Place them in the room you're standing in, rename or delete them.
- **Capture one:** for an object the scan missed: select it, then walk around it.
- **More:** save the layout of placed objects, clear the room, and the milestone 0–1 tools
  (drift measurement, test marker, frame round trip, readiness report).
- **Status panel (top left):** tracking, depth, fps, planes, Mac service status.
- **Settings (gear):** Mac service URL, planes, feature points, depth map, depth occlusion.

The removal and inpainting view was dropped on 24 Oct 2026 (`mesh/RemovalRenderer.kt` is kept but unused).

## 1. Scan stuff

You pick the things; the Mac doesn't guess. (Until 24 Oct the Mac tried to find objects in the
scan on its own; on real rooms it returned 2–3 cm fragments seen in one or two frames.)

1. Press **Scan stuff**. The app puts a level reference anchor where you're standing; every
   keyframe's pose is stored relative to it. You can unplug the phone while filming.
2. **Tap the thing you want to keep, a few spots on it** (for a chair: seat, back, an armrest).
   The first tap starts *thing 1* with a coloured marker and a ring; more taps add to it, each with
   its own marker. Each tap's frame is kept as a keyframe with the tapped point in 3D (from ARCore's
   depth/plane hit test). The Mac's format takes prompt points in one seed frame, so the latest tap's
   keyframe becomes the seed and earlier taps are re-projected into it from their 3D points (those
   still in view there). If the Mac is reachable, the outline from all the taps appears in the
   corner and is saved as the seed mask; if not, the Mac outlines it after upload.
   **New thing** makes the next tap start another thing. **Undo tap** removes the latest tap (and
   the thing, if it was its only tap). Up to 8 taps per thing.
3. **Walk around each thing** until its ring is mostly green. Every keyframe that sees a tapped
   point (in view, 0.2–4 m away, and not hidden behind something nearer according to that frame's
   depth; any of the thing's taps counts) adds a view. Coverage uses the Mac's definition: 360° minus the largest gap between the
   camera's directions around the thing. A thing counts as covered at 8 views and 180° (the Mac
   flags less as low quality). The banner names the least-covered thing.
   Keyframes are kept when you've moved about 10 cm or turned about 10°, at most 4 a second, not
   while turning fast (over 45°/s), not when blurry relative to the rest of the scan, and only with
   a new depth image (raw depth plus confidence when the phone has it).
4. **Finish scan** (shows covered/tapped things and frames) is enabled with at least one tapped
   thing and 30 frames. If some things aren't covered it asks whether to keep filming. A scan holds
   up to 30 things and 400 frames (JPEG quality 85; a full scan is roughly 200 MB).
5. Plug in (and run `adb reverse tcp:8000 tcp:8000`) before pressing Finish: the scan is packaged as
   `housingar.scan.v2` (scan_v1 frames plus `objects[]` and `seeds/<object_id>.png`; see
   `docs/scan_v2.md`), uploaded with progress, and the job card shows the Mac's stage. **Cancel**
   works during packaging, upload and processing. A failed scan stays on the phone
   (`files/scans/<id>/`) and **Retry** re-uploads it with a new ID (`<id>_r2`, …).
   Processing takes several minutes or more (the Mac follows each thing through every frame with
   SAM on its CPU; Codex measured 12 min for a small 120-frame test scan). Once the upload is done
   you can unplug: the card says it can't reach the Mac and carries on by itself when it can again.
   Keep the app open meanwhile.
   **Wi‑Fi instead of USB:** start the Mac service with `--host 0.0.0.0` and type
   `http://<Mac's address>:8000` into the app's settings (details in `docs/scan_v2.md`).
6. **Objects found:** one row per tapped thing ("Name thing 2 …"), with its photo, size, views,
   degrees around and warnings. Good ones are ticked; low-quality ones are unticked; ones without a
   mesh can't be kept (use **Capture one**). Name each one you keep and press **Add to My stuff**.

## 2. Capture one object

Press **Capture one** and tap the object. Correct its outline on the review screen (include and
exclude points, Other mask), then press **Create mesh** and walk around it while the ring on the
floor fills in. **Finish** is enabled after 12 frames. When the Mac's mesh is ready, the app asks
you to name it and adds it to My stuff. Details of the capture (seed box, keyframe rules,
`capture_v1`) are below under *Capture one: details*.

## 3. My stuff and placing objects

- **My stuff** syncs with the Mac when it's reachable and downloads every object's mesh and photo
  to the phone (`files/library/`). In the new apartment, where the Mac usually isn't reachable,
  it shows the saved copies; placing works offline, rename and delete need the Mac.
- **Place:** the object is loaded at real size; tap the floor or a table to put it there. It stands
  upright (bottom-centre on the surface) and faces you. You can place several objects, and the
  same object more than once.
- **Tap a placed object** to select it (yellow box). Then **⟲ 15° / ⟳ 15°** turn it, **Move** puts it
  where you tap next, **Delete** removes it from the room (not from My stuff), **Done** deselects.
- **More → Save layout:** records which objects are where (`item_id`, name, world pose) in
  `files/layouts/<id>.json` and posts it to the Mac. Poses are in this AR session's world frame;
  reopening a layout in a later session isn't implemented yet.
- **More → Clear room** removes placed objects, surfaces and markers from the view.

## Capture one: details

1. The selection anchor is **level and faces the camera** (`AnchorFrames`: +Y up, +Z towards the
   viewer, still attached to the tapped surface) and becomes the capture's coordinate frame. The
   app estimates the object's 3D box from that frame's depth (`SeedExtent`). The Mac treats depth
   inside this box as object and clips its mesh to it, so the box:
   - stops 1.2 cm above the supporting surface it sees under the object;
   - adds the unseen depth only on the side away from the camera, and stops 1.5 cm in front of any
     wall or other surface seen behind the object;
   - uses only depth samples wholly inside (object) or wholly outside (background) the mask;
   - is padded by 3 cm elsewhere.
2. Keyframes are kept only when the view has changed (about 7° around the object or 12 cm of
   movement), the phone is not turning quickly, the frame is not blurry relative to the rest of the
   capture, and a *new* depth observation is available. 30–90 frames is typical; it stops at 90.
3. The capture is packaged in the `housingar.capture.v1` format (`docs/capture_v1.md`): JPEG
   keyframes at the camera's highest 30 fps CPU resolution (up to 1920×1440), uint16 depth,
   uint8 confidence, the seed mask, and per-frame intrinsics and poses relative to the anchor.
   Depth is cropped to the part inside the image, and its intrinsics are derived through the
   depth→image mapping so the Mac's consistency check holds.

The selection review on its own (**Anchor surface**) still fits and anchors a flat surface patch
(milestones 2–3); those are saved in `files/geometry/` and posted to `/v1/geometry`.

## Milestone 1 drift test

1. Put a taped "X" on a textured, well-lit tabletop or wall.
2. Move the phone slowly sideways until planes or depth appear, then tap the X to place a marker.
3. Pan away, step about 1 m sideways, come back after about 30 s.
4. Press **Measure drift** and tap the X. The target is under 5 cm at about 2 m. Repeat 3 times.

## Where results go

On the phone, under `/sdcard/Android/data/com.housingar.inspect/files/`: `scans/` (scans and their
zips), `captures/` (single-object captures), `library/` (offline copy of My stuff: `index.json` and
`<item_id>/object.glb`, `thumbnail.jpg`), `layouts/`, `reports/`, `drift/` and `geometry/`. Copy them with:
```
adb pull /sdcard/Android/data/com.housingar.inspect/files/ ./phone-results/
```

## Mac service API this app calls

Existing: `GET /health`, multipart `POST /v1/segment`, `POST /v1/diagnostics`, `POST /v1/logs/drift`,
`POST /v1/geometry`, multipart `POST /v1/captures` (`manifest` + `archive`), `GET /v1/jobs/{id}`,
`POST /v1/jobs/{id}/cancel`, and job output files.

Scan and library (the phone's expectations; `LibraryClientTest` runs them against a fake server):

| Call | Phone sends | Phone reads |
|---|---|---|
| `POST /v1/scans` | multipart `manifest` (scan_v2 JSON; scan_v1 if nothing was tapped) + `archive` (zip) | `job_id`, `scan_id` |
| `GET /v1/jobs/{id}` | | `state` (`done`, `failed`, `cancelled`, else in progress), `progress`, `message`, `error`, `outputs.proposals` (default `/v1/scans/{scan_id}/proposals`) |
| `GET /v1/scans/{id}/proposals` | | `proposals[]`: `proposal_id`, `parent_id`, `thumbnail_url`, `mesh_url` (null = no usable mesh), `dimensions_m` [w, h, d], `method`, `quality` {`views`, `coverage_deg`, `observed_fraction`, `low_quality`, `warnings[]`} |
| `POST /v1/library` | `{"name", "source": {"scan_id", "proposal_id"}}` or `{"name", "source": {"job_id"}}` | the new item |
| `GET /v1/library` | | `items[]`: `item_id`, `name`, `dimensions_m`, `mesh_url`, `thumbnail_url`, `method`, `low_quality`, `created` |
| `PATCH /v1/library/{item_id}` | `{"name"}` | the item |
| `DELETE /v1/library/{item_id}` | | any 2xx |
| `POST /v1/layouts` | `{"layout_id", "session_id", "name", "placements": [{"item_id", "pose_world": [16, column-major]}]}` (the phone's own copy also has each object's `name`) | any 2xx |

Names are 1–80 characters. URLs may be absolute or service-relative. Library meshes follow the same GLB rules as capture meshes
(one textured triangle mesh, metres, +Y up, origin at the bottom centre). The phone re-downloads an
item whenever any of its fields change.

## Tests

```
./gradlew testDebugUnitTest            # geometry, scan/capture rules and formats, library sync and API, GLB loader (JVM, no phone)
./gradlew testDebugUnitTest -PsegmentationUrl=http://127.0.0.1:8000   # + live segmentation contract test
./gradlew testDebugUnitTest -PcaptureOut=/tmp/cap    # writes sample capture_v1 and scan_v1 folders + zips for the Mac validators
./gradlew testDebugUnitTest -PglbIn=/path/box.glb    # loads a GLB written by the Mac's writer (24-vertex box)
./gradlew testDebugUnitTest -PsegmentationUrl=http://127.0.0.1:8000 -Pe2eDir=/tmp/e2e   # whole Capture-one chain
./gradlew testDebugUnitTest -PsegmentationUrl=http://127.0.0.1:8000 -PscanE2eDir=/tmp/scan-e2e   # whole Scan stuff chain
```
`ScanEndToEndMacTest` needs the Mac's synthetic scan in `<scanE2eDir>/synth/`
(`python scripts/generate_synthetic_scan.py <scanE2eDir>/synth --version 2 --frames 120`; without
`--version 2` it tests the old automatic discovery). With taps, it turns them into the phone's
`TappedObject`s, measures their coverage with the phone's own visibility rule on every frame's
depth (all four must count as covered), and expects exactly those things back, in order. It re-expresses the
walk-through in the level scan anchor the phone's renderer creates, packages it as scan_v1 with the
phone's code, uploads and polls it, checks every proposal (thumbnail, GLB with `GlbLoader`, size,
position from `object_to_anchor`, parent links) against the ground truth, confirms them into My stuff,
syncs the offline copy with `LibraryStore.sync`, renames, deletes, saves a layout, checks the offline
fallback, and checks that a reused scan ID is refused (409) while the suffixed retry is accepted and
can be cancelled. It writes `<scanE2eDir>/phone_scan_e2e_result.json`.

Last runs (24 Oct): with taps, against Codex's v2 service without SAM, the phone-packaged scan was
accepted and validated (processing needs SAM, so it stopped there); the phone's coverage for the
four things was 100–111 views and 311–338°, against the Mac's own 106–120 frames and 308–356°.
Codex's real-SAM run of the same scene: 4/4 things within 1.3 cm in size and 1.5 cm in position
(`docs/scan_v2_results.md`). Earlier, automatic discovery (geometric test mode): 4/4 within 1.4 cm.
The Capture-one chain also confirmed its result into My stuff.
`EndToEndMacTest` takes the Mac's synthetic ground-truth scene from `<e2eDir>/synth/`, re-expresses it in
the anchor the phone would create, computes the seed box with the phone's `SeedExtent`, packages and
uploads it with the phone's code, polls the real job, downloads with the phone's URL handling, loads
both GLBs with `GlbLoader`, places the object with `object_to_anchor`, and checks it against the
ground truth. The packaged capture is saved as `fixtures/phone_capture_e2e/` at the repo root.
The contract test sends the phone's real requests to a running service; start it with
`--backend floodfill` so the expected masks are predictable.
