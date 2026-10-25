# Roomfit website

Find a rental, open it as a floor plan you can adjust, and walk through it in 3D with the furniture
you scanned with the phone app. It works in a laptop browser and in the phone's browser.

The Mac service serves the built site (`web/dist`) at its own address, so open
**http://127.0.0.1:8000** on the Mac. On the phone, plug in, run `adb reverse tcp:8000 tcp:8000`
and open the same address in Chrome. Looking around by moving the phone needs that
`127.0.0.1` address (or https): browsers only share motion sensors with secure pages.

## Pages

- **Find a place** (`#/`): an address, city or ZIP (or your location), rent limit, bedrooms and
  distance (10 miles by default), nearest first, up to 100. Results come from RentCast through the
  Mac when it has a key. Without one, the Mac uses its saved listings
  (`segmentation-service/listings_data/`: 428 rentals around Blacksburg and Christiansburg from
  Virginia Tech Off-Campus Housing, collected 25 Oct 2026), or clearly labelled samples if that
  file is missing. Saved listings say when they were collected; *a month per bedroom* means one
  person rents one bedroom (by-the-bed leases, rooms in shared places), and the rent filter
  compares that price.
- **Listing** (`#/listing/<id>`): name, address, rent, rooms, size, contact, *Open the listing*
  (its page, where you apply; RentCast gives no page link, so then *Find this listing online*
  searches the web for the address), *Open in Maps*, and the floor plan.
- **Plan and furnish** (`#/room/<id>`):
  - **Plan:** the estimated floor plan drawn with dimension lines. Select a room, then drag its
    walls (yellow handles; arrow keys work too, Shift for 1 ft steps) or type its width and depth
    (`12' 4"`, `12 4`, `3.7 m`, `370 cm`). Neighbouring rooms adjust so the plan stays tiled.
    Rename a room or change its kind. Changes save automatically; *Go back to the estimated plan*
    undoes them. The site checks the same rules as the Mac before saving (every room reachable
    through doors from the front door, living rooms and bedrooms keep a window to the outside,
    no room under 0.6 m), and says which one a change breaks instead of saving it. If the Mac
    can't estimate rooms for a listing (say five bedrooms in 600 sq ft), you get one open room of
    the listing's size to start from.
  - **3D:** *Overview* (drag to orbit, scroll or pinch to zoom; outer walls facing you fade) or
    *Walk through* (eye height 1.6 m; drag to look, W A S D or arrow keys, click/tap the floor to
    walk there; arrow buttons on touch screens; *Look around* uses the phone's motion sensors).
    You can't walk through walls, only through doors.
  - **Add furniture:** places a My stuff item where you're looking, at the nearest spot where it
    fits. Drag it to move it (it slides along walls and can't go into them), *Turn left / right*
    (15°, or Q / E), *Remove* (or Delete). Overlapping pieces are outlined in orange. Placements save
    automatically and also show on the plan.
- **My stuff** (`#/stuff`): everything in the Mac's library with its size; photo-quality models
  first, older depth-only ones marked *Rough model*.

Feet and inches by default; *Metres* switches the plan (remembered per browser).

## API it uses

Everything is same-origin under `/v1` (contract: `docs/listings_v1.md`, written by the Mac side):
`GET /v1/listings`, `GET /v1/listings/{id}`, `GET|PUT|DELETE /v1/listings/{id}/layout`
(`housingar.room.v1`), `GET|PUT /v1/listings/{id}/furniture` (`housingar.room_furniture.v1`),
`GET /v1/library` and the library's GLB and thumbnail URLs. Plan coordinates are metres, x to the
right, z down the plan, y up; furniture positions are the GLB's bottom centre.

## Develop

```
npm install
npm run mock          # stand-in API on :8000 (sample listings, simple plans, My stuff from ../phone-library2)
npm run dev           # site on http://localhost:5173, /v1 proxied to :8000 (the Mac service or the mock)
npm test              # floor-plan geometry, wall cutting, editing, collisions, and the Mac's own
                      # generated plans (test/fixtures/mac-layouts.json) (Vitest)
npm run build         # type-check and build to dist/ (the Mac serves this)
node mock/server.mjs --port 8790 --dist dist & node e2e/run.mjs http://127.0.0.1:8790 e2e-out
                      # drives the built site at laptop and phone sizes and saves screenshots;
                      # it puts the listing it edits back the way it was when it's done
```

`mock/estimate.mjs` is only a stand-in for the Mac's estimated plans; `mock/server.mjs --real` makes
listings look like RentCast results (with fictional 555-01xx contacts).

Built with TypeScript, Vite 5.4, three.js 0.170 (WebGL; GLTFLoader for the scanned models) and the
Archivo typeface (self-hosted, so the site works offline).
