# Mac object-capture tools

An isolated implementation of the Mac-side capture preparation and photogrammetry commands from `PROJECT_PLAN.md` (personal-object capture, milestone 5). All source, generated files, and build caches live under `object-capture/`. These tools do not change Android, the shared app, the segmentation API, or the running service.

## What is implemented

- Prepare overlapping object photographs or sample frames from a phone video into a new JPEG folder.
- Filter unreadable images, low-sharpness views, exact duplicate pixels, and nearly identical views; retain a review manifest with source/frame provenance and rejection reasons.
- Check RealityKit Object Capture support and validate photo folders without starting reconstruction.
- Run a background-friendly command-line reconstruction using Apple's `PhotogrammetrySession`, producing USDZ, a job manifest, and JSON-line progress events.
- Record user-supplied dimensions separately from reconstructed geometry. Never silently treat an unknown-scale model as a measured object.
- Refuse overwriting outputs or writing generated artifacts into source-photo folders. Handle timeout/interrupt cancellation and preserve a failed job record.

**Not yet validated end to end:** reconstruction of a real owned object, mesh orientation/bottom-pivot correction, metric scale calibration, USDZ-to-GLB conversion, or appearance in Android/web viewers. This folder advances the capture pipeline; it does not complete the full personal-object placement milestone.

## Build and check the native Mac command

Requires macOS 13+ and Xcode Command Line Tools. No Swift package downloads are needed. From the repository root:

```bash
bash object-capture/build.sh
object-capture/.build/housing-object-capture check
```

The support command prints JSON with `supported` and the macOS version. Its exit status is nonzero if unsupported. It does not start reconstruction. Under a restricted execution sandbox, GPU discovery can report unavailable even when the ordinary Mac process reports support.

The command compiled on this Mac (Apple M5, macOS 27.0.1), and the actual support check returned **true**. Build artifacts and module caches are confined to `.build/`.

## Prepare photos or video frames

For the preparation script, use Python 3.10–3.12 with `requirements.txt` installed in a virtual environment. The existing segmentation-service environment already contains these dependencies and can run the script without changing that environment:

```bash
# Direct photographs:
segmentation-service/.venv/bin/python object-capture/prepare_capture.py \
  /absolute/path/to/object-photos \
  --output object-capture/data/my-object/photos

# An orbit video, sampled at candidate intervals of 0.5 seconds:
segmentation-service/.venv/bin/python object-capture/prepare_capture.py \
  /absolute/path/to/object-video.mp4 \
  --output object-capture/data/my-object/video-frames \
  --sample-every 0.5 --max-frames 120
```

Or create a separate environment under this folder:

```bash
python3.12 -m venv object-capture/.venv
object-capture/.venv/bin/python -m pip install -r object-capture/requirements.txt
```

Output folders must be new. An existing folder is never overwritten. Photo input supports JPEG/PNG/TIFF. The native reconstruction command can also consume original HEIC/HEIF photographs directly.

Preparation normalizes photo EXIF orientation, preserves aspect ratio, limits the longest edge to 2048 pixels, and writes JPEG quality 95. For video, automatic rotation is disabled for consistent behavior across OpenCV builds; pass `--rotate 90`, `180`, or `270` when needed. The video manifest records estimated frame times from the reported frame rate, not precision synchronization timestamps.

Defaults: at least 10 accepted views, at most 120, Laplacian-variance sharpness threshold 30, and thumbnail mean-difference threshold 2. Adjust `--min-sharpness` or `--min-difference` for a particular capture; those numerical thresholds are heuristics, not capture-quality certification. Exact duplicate pixels are always rejected. The script stops once the maximum accepted count is reached.

Each prepared folder contains `frame_00000.jpg`, etc., and `manifest.json` with accepted/rejected frames, dimensions, quality measurements, source filenames/video frame numbers, and pixel hashes. An insufficient capture returns nonzero and retains a `failed` manifest for inspection. No model inference occurs during preparation.

Prepared JPEGs do **not** retain depth metadata. If original photographs contain useful depth data, use them directly with the native command rather than this RGB preparation step.

## Validate and reconstruct

```bash
object-capture/.build/housing-object-capture validate \
  --input object-capture/data/my-object/photos

object-capture/.build/housing-object-capture reconstruct \
  --input object-capture/data/my-object/photos \
  --output object-capture/data/models/my-object.usdz \
  --detail preview \
  --width-m 0.45 --height-m 0.60 --depth-m 0.40
```

`validate` reads image metadata and requires at least 10 readable images. That minimum is an application choice; it does not establish sufficient overlap, sharpness, or all-side coverage. A single photograph is not a multi-view reconstruction input.

Reconstruction options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--detail` | `reduced` | `preview`, `reduced`, `medium`, or `full` |
| `--sequential` | off | Use when filenames follow capture order, such as prepared video frames |
| `--object-masking` | off | Request RealityKit's automatic object masking; inspect results before relying on it |
| `--timeout-seconds` | `600` | Cooperative cancellation deadline, up to 86400 seconds |
| `--width-m`, `--height-m`, `--depth-m` | unset | Optional real measurements; provide all three together |

Reconstruction can use substantial GPU/memory resources. Run it when it will not compete with another worker's device testing or the live segmentation demo. Preparation/validation do not start this GPU reconstruction job.

Progress events go to stdout and `<name>.events.jsonl`. Ctrl-C/SIGTERM and timeouts ask RealityKit to cancel. The result paths are:

```text
my-object.usdz
my-object.capture.json
my-object.events.jsonl
```

A successful manifest uses `status=model_generated` only after RealityKit reports completion and a nonempty model file exists. Failed/cancelled jobs use `status=failed`; partial files must not be treated as finished assets. The manifest records elapsed time, photo metadata, skipped/invalid sample counts, and available reconstructed bounds.

The manifest intentionally retains:

```json
{
  "representation": "captured_mesh",
  "format": "usdz",
  "scale_status": "unverified",
  "pivot_status": "unverified",
  "orientation_status": "unverified",
  "glb_conversion_status": "not_performed",
  "measured_dimensions_m": {"width": 0.45, "height": 0.60, "depth": 0.40}
}
```

The dimensions are metadata, not an automatic rescaling operation. The next integration step is to inspect a real reconstructed object, calibrate its scale, correct orientation/pivot, convert it into the agreed GLB format, and verify the same asset in both viewers. No new shared application/API contract is imposed by this standalone tool.

## Capture guidance

Move around one stationary, textured, nonreflective object in steady light. Capture overlapping views from several heights; aim for 30–100 useful photographs. Avoid a video that only pans from one position: it does not observe the object from different positions. Mirrors, glass, fine legs, featureless surfaces, changing shadows, and occluded sides can cause gaps or reconstruction failure. Retake weak coverage rather than treating inferred or missing geometry as measured.

Primary references: [Apple command-line photogrammetry](https://developer.apple.com/documentation/realitykit/creating-a-photogrammetry-command-line-app), [Apple capture guidance](https://developer.apple.com/documentation/realitykit/capturing-photographs-for-realitykit-object-capture).

## Tests

```bash
bash object-capture/build.sh
segmentation-service/.venv/bin/python -m pytest object-capture/tests -q
```

The tests exercise the compiled Swift command, real JPEG/video decoding, selection and orientation behavior, error handling, and overwrite/source-folder protection. Synthetic fixture photographs and video test the file pipeline only; they are not used to claim reconstruction quality. No checkpoint download or full photogrammetry job is required.
