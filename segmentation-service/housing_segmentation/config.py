from dataclasses import dataclass
import os
from pathlib import Path


MODEL_IDS = {
    "tiny": "facebook/sam2.1-hiera-tiny",
    "small": "facebook/sam2.1-hiera-small",
}
SERVICE_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    backend: str = "floodfill"
    model: str = "tiny"
    device: str = "auto"
    data_dir: Path = SERVICE_ROOT / "data"
    cache_frames: int = 2
    max_image_bytes: int = 10 * 1024 * 1024
    max_image_pixels: int = 8_000_000

    def __post_init__(self):
        if self.backend not in {"sam2", "floodfill"}:
            raise ValueError("backend must be sam2 or floodfill")
        if self.model not in MODEL_IDS and self.model not in MODEL_IDS.values():
            raise ValueError("model must be tiny, small, or their facebook/sam2.1-hiera-* ID")
        if self.device not in {"auto", "mps", "cpu"}:
            raise ValueError("device must be auto, mps, or cpu")
        if self.cache_frames < 1:
            raise ValueError("cache_frames must be at least 1")

    @classmethod
    def from_env(cls):
        return cls(
            backend=os.getenv("SEGMENTATION_BACKEND", "floodfill"),
            model=os.getenv("SAM2_MODEL", "tiny"),
            device=os.getenv("SEGMENTATION_DEVICE", "auto"),
            data_dir=Path(os.getenv("SEGMENTATION_DATA_DIR", str(SERVICE_ROOT / "data"))),
            cache_frames=int(os.getenv("SEGMENTATION_CACHE_FRAMES", "2")),
        )

    @property
    def model_id(self):
        return MODEL_IDS.get(self.model, self.model)
