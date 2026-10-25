# Housing AR

GDG Housing Hackathon project.

Housing AR helps users find housing listings that fit their needs and experience an immersive
3D preview of setting up their dream home remotely. Search for a rental, open its editable floor
plan, and furnish it with 3D scans of furniture you already own.

The app is designed to be easy to use, even for people with no experience in 3D development.
Arrange your existing furniture in your new home, explore the layout on a laptop or phone, and
preview it at full size in AR on the phone. Generated floor plans are estimates that users can
adjust to match known room dimensions.

## AI models and 3D reconstruction

- **SAM 2.1 Tiny (Meta's Segment Anything 2)** separates the object you tap from its background.
- **Apple Object Capture (RealityKit)** builds detailed, textured 3D models from photos captured
  on the phone.
- **Open3D** handles the older depth-based reconstruction path and geometry cleanup.
- **Trimesh, xatlas, pymeshfix and manifold3d** repair meshes, lay out texture coordinates and
  export the GLB files loaded by the phone and website.
- **OpenCV, scikit-image, SciPy and NumPy** handle image processing and geometry calculations.

## Phone app

The Android app uses **Kotlin** with **Google ARCore 1.57** for motion tracking, depth and anchors.
It is built from Google's **hello_ar** sample renderer, which uses **OpenGL ES**.

## Website

- **TypeScript** is the programming language, and **Vite** is the build tool.
- **three.js** renders the 3D room and loads your GLB furniture models.
- **Archivo** is the typeface.
- **Vitest and Playwright** are used for testing.

## Project structure

| Folder | What it is |
| --- | --- |
| `android/` | Pixel app (Kotlin, ARCore): scan things by tapping them, capture single objects, place your 3D models in AR. |
| `segmentation-service/` | Mac service (Python, FastAPI): segmentation (SAM 2.1), 3D reconstruction, the My stuff library, rental listings, floor plans and furniture layouts. It also serves the website. |
| `object-capture/` | Apple Object Capture (RealityKit photogrammetry) helper the Mac service uses for photo-quality models. |
| `web/` | Website (TypeScript, Vite, three.js): search rentals, edit the estimated floor plan, place furniture in 3D, walk through on a laptop or phone. |
| `docs/` | Data formats and API contracts (`capture_v1`, `scan_v2`, `listings_v1`) and measured results. |
| `fixtures/` | Test fixtures. |

## Run it

On the Mac:

```
cd segmentation-service
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # plus requirements-sam2.txt for SAM
.venv/bin/python -m housing_segmentation --backend sam2 --model tiny --port 8000
```

Then open http://127.0.0.1:8000. To use it on the phone, plug it in, run
`adb reverse tcp:8000 tcp:8000`, and open the same address in Chrome. The phone app connects the
same way.

Listings come from RentCast when `RENTCAST_API_KEY` is set. Without a key, the service uses a local
listings file if there is one (`segmentation-service/listings_data/`). Otherwise it shows clearly
labelled sample listings. The saved Virginia Tech listings file isn't in this repository; its
README explains why and how to rebuild it.

Each folder has its own README with details and tests.
