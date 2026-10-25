# Saved rental listings

`nrv_vt_offcampus.json` holds 428 rentals around Blacksburg and Christiansburg, VA (424 within
10 miles of Virginia Tech, ZIP 24061). They were collected on 25 October 2026 from
[Virginia Tech Off-Campus Housing](https://offcampus.vt.edu/housing). That covers 14 of the
site's result pages and the floor-plan pages of 36 apartment communities. It is not the full
list: the site showed about 760 rentals that day.

The Mac service uses this file when `RENTCAST_API_KEY` isn't set, in place of the fictional
samples. A RentCast key still takes priority.

## What each listing has

- **Name, address and link:** the property or post name, the street address, and `listing_url`,
  the listing's page on offcampus.vt.edu, where you apply or contact the lister.
- **Rent:** `rent_monthly` is the lowest price shown for that listing or floor plan.
  - `rent_per: "bedroom"` means one person rents one bedroom: a by-the-bed lease or a room in a
    shared place.
  - `rent_per: "unit"` means the price is for the whole place.
  - Fees aren't included.
- **Rooms and size:** bedrooms, bathrooms, and square feet when the site listed them (132 of the
  428). Bathrooms are estimated for some houses whose posts didn't say.
- **Floor plans:** apartment communities have one listing per floor plan.
- **Location:**
  - `lat` and `lon` come from the US Census geocoder.
  - 17 addresses the Census geocoder couldn't match were looked up on OpenStreetMap. Two of
    those are placed at the middle of the street (`geocoded_by: "openstreetmap street"`).
  - 5 listings that neither could place were left out.
- **Places:** `places` maps "24061", "Virginia Tech", "Blacksburg", "Christiansburg", "Radford"
  and their ZIP codes to a point, so those searches work without geocoding.
- **No contact details:** contacts are left out. Use the listing link.

## Keep in mind

- **Prices change.** Prices and availability are a snapshot from 25 October 2026. The website
  says so and links to each current listing.
- **Personal use only.** The site's terms allow a copy for personal, non-commercial use. Don't
  publish this file or commit it to a public repository.
- **Don't refresh it often.** If you collect the listings again, do it occasionally and by hand,
  not on a schedule.

## Rebuild or switch it off

`source/` holds the collected rows (`vt_offcampus_rows_2026-10-25.json`), the Census batch
geocoder input and output, the merged geocodes, and the two scripts that made the file:

```
cd source
python3 build_geocodes.py census_batch_input_2026-10-25.csv census_batch_result_2026-10-25.csv
python3 make_dataset.py                                          # rewrites ../nrv_vt_offcampus.json
```

The service reloads the file the next time someone searches; no restart is needed. To use
another file, start the service with `--listings-dataset path/to/file.json`. To go back to the
fictional samples, use `--listings-dataset none` or `HOUSING_LISTINGS_DATASET=none`.
