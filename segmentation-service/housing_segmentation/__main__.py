import argparse
import os
from dataclasses import replace

import uvicorn

from .app import create_app
from .config import SERVICE_ROOT, Settings

DEFAULT_DATASET = SERVICE_ROOT / "listings_data" / "nrv_vt_offcampus.json"


def parse_args(argv=None):
    defaults = Settings.from_env()
    parser = argparse.ArgumentParser(description="Housing AR Mac segmentation service")
    parser.add_argument("--backend", choices=("sam2", "floodfill"), default=defaults.backend)
    parser.add_argument("--model", choices=("tiny", "small"), default=defaults.model)
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default=defaults.device)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--listings-dataset",
        default=os.getenv("HOUSING_LISTINGS_DATASET", str(DEFAULT_DATASET)),
        help="Local listings file used when RENTCAST_API_KEY is not set ('none' to use samples)",
    )
    return parser.parse_args(argv)


def main():
    defaults = Settings.from_env()
    args = parse_args()
    settings = replace(defaults, backend=args.backend, model=args.model, device=args.device)
    dataset = None if args.listings_dataset.strip().lower() in ("", "none") else args.listings_dataset
    uvicorn.run(create_app(settings, listings_dataset=dataset), host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
