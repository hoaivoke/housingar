#!/usr/bin/env python3
"""Exercise real HTTP, restart persistence and optional single RentCast request.

The demo server uses an isolated data folder, copies one existing library item
for furnishing evidence, and is always stopped. Neither source library data nor
web/ is changed. Credentials are never written to the report or command line.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter, sleep

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from housing_segmentation.listings.service import ListingQuery, Listings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18001)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error("Use a new output directory")
    # Never send test writes to another worker's server on a busy port.
    try:
        with socket.socket() as probe:
            probe.bind(("0.0.0.0", args.port))
    except OSError as exc:
        parser.error(
            f"Cannot bind port {args.port}; choose --port with a free port: {exc}"
        )
    output.mkdir(parents=True)
    data = output / "server-data"
    (data / "library").mkdir(parents=True)
    originals = sorted((ROOT / "data/library").glob("*/item.json"))
    # Prefer the actual user-scanned black chair over synthetic library items.
    source = next(
        (
            p
            for p in originals
            if json.loads(p.read_text()).get("source", {}).get("scan_id")
            == "6fb50237_scan_93413134"
        ),
        None,
    )
    item = None
    if source:
        item = json.loads(source.read_text())
        shutil.copytree(source.parent, data / "library" / item["item_id"])
    result = {
        "measured_utc": datetime.now(timezone.utc).isoformat(),
        "port": args.port,
        "bind_host": "0.0.0.0",
        "web_dist_present": (ROOT.parent / "web/dist/index.html").is_file(),
        "rentcast_key_set": bool(os.getenv("RENTCAST_API_KEY", "").strip()),
        "live_rentcast_calls": 0,
        "library_item": None,
    }
    env = dict(
        os.environ,
        SEGMENTATION_DATA_DIR=str(data),
        RENTCAST_API_KEY="",
        PYTHONUNBUFFERED="1",
    )
    base = f"http://127.0.0.1:{args.port}"
    with httpx.Client(base_url=base, trust_env=False, timeout=15) as client:

        def start():
            log = (output / "server.log").open("a")
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "housing_segmentation",
                    "--backend",
                    "floodfill",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    str(args.port),
                ],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            log.close()
            for _ in range(150):
                if process.poll() is not None:
                    raise RuntimeError("Demo server exited; see server.log")
                try:
                    if client.get("/health").status_code == 200:
                        return process
                except httpx.HTTPError:
                    pass
                sleep(0.1)
            process.terminate()
            process.wait(timeout=10)
            raise RuntimeError("Demo server did not start")

        def stop(process):
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

        process = start()
        try:
            started = perf_counter()
            first = client.get("/v1/listings", params={"lat": 30.2672, "lon": -97.7431})
            first.raise_for_status()
            first = first.json()
            result["sample_first_seconds"] = perf_counter() - started
            second = client.get(
                "/v1/listings", params={"lat": 30.2672, "lon": -97.7431}
            ).json()
            assert (
                first["source"] == "sample"
                and len(first["listings"]) == 15
                and not first["cached"]
                and second["cached"]
            )
            identifier = first["listings"][2]["listing_id"]
            prefix = "/v1/listings/" + identifier
            plan = client.get(prefix + "/layout").json()
            plan.update(estimated=False, source="user_edited")
            plan["rooms"][0]["name"] = "Saved bedroom"
            saved_plan = client.put(prefix + "/layout", json=plan)
            saved_plan.raise_for_status()
            saved_plan = saved_plan.json()
            result.update(
                sample_count=15,
                cached_second=second["cached"],
                room_count=len(plan["rooms"]),
                area_error_fraction=abs(
                    plan["total_area_m2"]
                    - first["listings"][2]["square_feet"] * 0.09290304
                )
                / plan["total_area_m2"],
            )
            saved_furniture = None
            if item:
                furniture = {
                    "schema": "housingar.room_furniture.v1",
                    "listing_id": identifier,
                    "placements": [
                        {
                            "placement_id": "chair_01",
                            "item_id": item["item_id"],
                            "name": item["name"],
                            "position": [1, 0, 2],
                            "yaw_deg": 45,
                        }
                    ],
                }
                saved_furniture = client.put(prefix + "/furniture", json=furniture)
                saved_furniture.raise_for_status()
                saved_furniture = saved_furniture.json()
                mesh = client.get(item["mesh_url"])
                mesh.raise_for_status()
                assert (
                    mesh.content
                    == (data / "library" / item["item_id"] / "object.glb").read_bytes()
                )
                assert client.get(item["thumbnail_url"]).status_code == 200
                result["library_item"] = {
                    "item_id": item["item_id"],
                    "name": item["name"],
                    "method": item["method"],
                    "mesh_bytes": len(mesh.content),
                    "placement_round_trip": client.get(prefix + "/furniture").json()
                    == saved_furniture,
                }
            geocoded = client.get(
                "/v1/listings",
                params={"near": "4600 Silver Hill Rd, Washington, DC 20233"},
            )
            geocoded.raise_for_status()
            result["census"] = {
                "center": geocoded.json()["center"],
                "matched": geocoded.json()["center"]["lat"] is not None,
            }
            cors = client.options(
                "/v1/listings",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "GET",
                },
            )
            result["cors_origin"] = cors.headers.get("access-control-allow-origin")
            result["root_status"] = client.get("/").status_code
            ip = subprocess.run(
                ["/usr/sbin/ipconfig", "getifaddr", "en0"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            result["lan_ip"] = ip or None
            if ip:
                with httpx.Client(trust_env=False, timeout=5) as lan:
                    result["lan_health_status"] = lan.get(
                        f"http://{ip}:{args.port}/health"
                    ).status_code
        finally:
            stop(process)
        process = start()
        try:
            result["restart_layout_persisted"] = (
                client.get(prefix + "/layout").json() == saved_plan
            )
            result["restart_cache_hit"] = client.get(
                "/v1/listings", params={"lat": 30.2672, "lon": -97.7431}
            ).json()["cached"]
            result["restart_furniture_persisted"] = (
                client.get(prefix + "/furniture").json() == saved_furniture
                if saved_furniture
                else None
            )
            assert result["restart_layout_persisted"] and result["restart_cache_hit"]
            if saved_furniture:
                assert result["restart_furniture_persisted"]
        finally:
            stop(process)
    if result["rentcast_key_set"]:

        def record_request(request):
            if request.url.host == "api.rentcast.io":
                result["live_rentcast_calls"] += 1
                result["live_rentcast_request_url"] = str(request.url)

        def record_response(response):
            if response.request.url.host == "api.rentcast.io":
                result["live_rentcast_status"] = response.status_code
                response.read()
                (output / "rentcast-response.json").write_bytes(response.content)

        with httpx.Client(
            timeout=15,
            event_hooks={"request": [record_request], "response": [record_response]},
        ) as provider:
            service = Listings(output / "live-provider", None, client=provider)
            try:
                live = service.search(ListingQuery(lat=30.2672, lon=-97.7431, limit=3))
                result["live_rentcast"] = {
                    "source": live["source"],
                    "count": len(live["listings"]),
                    "cached": live["cached"],
                }
            except Exception as exc:
                result["live_rentcast_error"] = str(exc)
        assert result["live_rentcast_calls"] == 1
    else:
        result["live_rentcast_skipped"] = (
            "RENTCAST_API_KEY is not set; no paid API request made"
        )
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
