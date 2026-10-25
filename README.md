# Housing AR

GDG Housing Hackathon project.

Find a rental near you, open it as an editable floor plan, and walk through it in 3D with your own
furniture, scanned with a phone.

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
