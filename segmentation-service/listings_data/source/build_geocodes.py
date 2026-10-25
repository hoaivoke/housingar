"""Turn the US Census batch geocoder output into geocodes_2026-10-25.json.

Usage: python3 build_geocodes.py <addresses.csv sent to the Census batch geocoder> <its result.csv>

Addresses the Census batch could not match were looked up one at a time (Census
single-address lookups, else OpenStreetMap Nominatim, 25 Oct 2026); they are listed in OSM_FIXES.
"""

import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sent, result = sys.argv[1], sys.argv[2]

# The first batch stripped a direction or a street name from these; the result rows
# are re-keyed to the corrected address the dataset uses.
ALIASES = {"301 Knollwood Dr": "301 N Knollwood Dr"}

# street|city|zip -> (lat, lon, how)
OSM_FIXES = {
    "1506 N Main St|Blacksburg|24060": (37.250106913549, -80.412104346613, "census"),
    "1820 N Main St|Blacksburg|24061": (37.255399366755, -80.410649248998, "census"),
    "470 College St|Christiansburg|24073": (37.126630240045, -80.415100196772, "census"),
    "1001 University City Blvd|Blacksburg|24060": (37.2380885, -80.4351555, "openstreetmap"),
    "1001 University City Boulevard|Blacksburg|24060": (37.2380885, -80.4351555, "openstreetmap"),
    "131 Huntington Lane|Blacksburg|24060": (37.2230065, -80.4472724, "openstreetmap"),
    "1435 Toms Creek Rd|Blacksburg|24060": (37.2446050, -80.4301367, "openstreetmap"),
    "200 Cedar Pointe Dr|Blacksburg|24060": (37.2003371, -80.3985103, "openstreetmap"),
    "200 Cedar Pointe Drive|Blacksburg|24060": (37.2003371, -80.3985103, "openstreetmap"),
    "207 Jackson St NE|Blacksburg|24060": (37.2297591, -80.4151124, "openstreetmap street"),
    "218 Huntington Lane|Blacksburg|24060": (37.2227124, -80.4475951, "openstreetmap"),
    "300 New Kent Road|Blacksburg|24060": (37.2093228, -80.3956024, "openstreetmap"),
    "301 Webb St|Blacksburg|24060": (37.2357208, -80.4220311, "openstreetmap"),
    "309 Apartment Heights Dr|Blacksburg|24060": (37.2356049, -80.4093518, "openstreetmap"),
    "410 Pin Oak Drive Northwest|Christiansburg|24073": (37.1493720, -80.4247308, "openstreetmap"),
    "428 Midtown Way|Blacksburg|24060": (37.2287035, -80.4073890, "openstreetmap"),
    "500 Broce Dr|Blacksburg|24060": (37.2419573, -80.4252860, "openstreetmap"),
    "516 Belvedere Ave|Blacksburg|24060": (37.2278015, -80.4079602, "openstreetmap"),
    "535 Blackrock Dr|Blacksburg|24060": (37.1854491, -80.4223756, "openstreetmap"),
    "555 Bend Trail Dr|Christiansburg|24073": (37.1634558, -80.4457397, "openstreetmap street"),
    # Terrace View's own page gives no address; 413 Hunt Club Rd per news of its 2024 sale.
    "413 Hunt Club Rd|Blacksburg|24060": (37.243503775255, -80.424446918037, "census"),
}

PLACES = {
    "p1": ("Virginia Tech, Blacksburg, VA 24061", ["24061", "virginia tech", "vt", "virginia tech,blacksburg", "virginia tech,blacksburg,va", "blacksburg,va 24061"], ["24061"]),
    "p2": ("Blacksburg, VA", ["blacksburg", "blacksburg,va", "blacksburg,virginia", "blacksburg va", "downtown blacksburg"], ["24060", "24062", "24063"]),
    "p3": ("Christiansburg, VA", ["christiansburg", "christiansburg,va", "christiansburg,virginia", "christiansburg va"], ["24068", "24073"]),
    "p4": ("Radford, VA", ["radford", "radford,va", "radford,virginia", "radford va"], ["24141", "24143"]),
    "p5": ("Radford University, Radford, VA", ["radford university"], ["24142"]),
}

ids = {}
for row in csv.reader(open(sent)):
    ids[row[0]] = row
addresses, places = {}, []
for row in csv.reader(open(result)):
    if len(row) < 6 or row[2] != "Match":
        continue
    lon, lat = (float(v) for v in row[5].split(","))
    sent_row = ids[row[0]]
    if row[0] in PLACES:
        label, names, zips = PLACES[row[0]]
        places.append({"label": label, "names": names, "zips": zips, "lat": round(lat, 6), "lon": round(lon, 6)})
        continue
    street = ALIASES.get(sent_row[1], sent_row[1])
    addresses[f"{street}|{sent_row[2]}|{sent_row[4]}"] = (lat, lon, "census")
addresses.update(OSM_FIXES)
missing = [p for p in PLACES if not any(pl["label"] == PLACES[p][0] for pl in places)]
if missing:
    sys.exit(f"Places not geocoded: {missing}")
out = HERE / "geocodes_2026-10-25.json"
out.write_text(json.dumps({"addresses": addresses, "places": places}, indent=1) + "\n")
print(f"{len(addresses)} addresses, {len(places)} places -> {out.name}")
