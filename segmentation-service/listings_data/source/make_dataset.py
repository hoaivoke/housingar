"""Build listings_data/nrv_vt_offcampus.json from the collected rows and geocodes.

Inputs (same folder as this script):
  vt_offcampus_rows_2026-10-25.json  listing rows collected from offcampus.vt.edu
  geocodes_2026-10-25.json           {"<street>|<city>|<zip>": [lat, lon, how]} plus places

Run:  python3 make_dataset.py   (writes ../nrv_vt_offcampus.json)
"""

import json
import re
from pathlib import Path
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
COLLECTED = "2026-10-25"

rows = json.loads((HERE / "vt_offcampus_rows_2026-10-25.json").read_text())
geo = json.loads((HERE / "geocodes_2026-10-25.json").read_text())
coords = geo["addresses"]


def bed_text(n):
    return "Studio" if n == 0 else f"{n} bd"


listings, dropped = [], []
for row in rows:
    a = row["address"]
    key = f"{row['geo_street']}|{a['city']}|{a['zip']}"
    point = coords.get(key)
    if point is None:
        dropped.append(f"{row['name']} ({a['line1']}, {a['city']})")
        continue
    formatted = f"{a['line1']}, {a['city']}, VA {a['zip']}".strip()
    query_address = f"{row['geo_street']}, {a['city']}, VA {a['zip']}"
    name = row["name"].strip()
    title = f"{name} · {bed_text(row['bedrooms'])} · {row['bathrooms']:g} ba"
    listings.append(
        {
            "listing_id": f"vt_{row['source_id']}_{row['plan_index']}",
            "source": "dataset",
            "sample": False,
            "name": name,
            "plan_name": row.get("plan"),
            "title": title,
            "address": {
                "line1": a["line1"],
                "line2": "",
                "city": a["city"],
                "state": "VA",
                "zip": a["zip"],
                "formatted": formatted,
            },
            "lat": round(point[0], 6),
            "lon": round(point[1], 6),
            "geocoded_by": point[2],
            "distance_mi": None,
            "rent_monthly": row["rent"],
            "rent_per": row["rent_per"],
            "currency": "USD",
            "bedrooms": row["bedrooms"],
            "bathrooms": row["bathrooms"],
            "square_feet": row["square_feet"],
            "property_type": row["property_type"],
            "listed_date": None,
            "days_on_market": None,
            "contact": None,
            "links": {
                "listing_url": row["url"],
                "search_url": "https://www.google.com/search?" + urlencode({"q": f"{name} {formatted}"}),
                "maps_url": "https://www.google.com/maps/search/?" + urlencode({"api": 1, "query": query_address}),
            },
            "photos": [],
            "collected": COLLECTED,
        }
    )

dataset = {
    "schema": "housingar.listings_dataset.v1",
    "name": "Virginia Tech off-campus housing listings around Blacksburg and Christiansburg",
    "source": "Virginia Tech Off-Campus Housing",
    "source_url": "https://offcampus.vt.edu/housing",
    "collected": COLLECTED,
    "note": (
        "Rents, rooms and addresses as listed on offcampus.vt.edu on 25 October 2026 "
        "(the listing pages the site showed that day, not every listing). Prices change; "
        "each listing links to its current page. Rent is per bedroom when rent_per is "
        "'bedroom' (by-the-bed leases and rooms), otherwise for the whole place. "
        "Contact details were left out. For personal, non-commercial use only."
    ),
    "places": geo["places"],
    "listings": sorted(listings, key=lambda l: l["listing_id"]),
}
(HERE.parent / "nrv_vt_offcampus.json").write_text(json.dumps(dataset, indent=1, ensure_ascii=False) + "\n")
print(f"{len(listings)} listings written; {len(dropped)} dropped without coordinates:")
for d in dropped:
    print("  ", d)
