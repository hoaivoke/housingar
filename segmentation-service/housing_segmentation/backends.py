"""All model state is accessed under the engine's inference lock."""
import os

# This must be set before importing torch, including in the benchmark process.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import cv2
import numpy as np

from .config import SERVICE_ROOT, Settings


class FloodfillBackend:
    name = "floodfill"
    model = "opencv-floodfill"
    device = "cpu"

    def synchronize(self):
        pass

    def embed(self, rgb):
        # No learned embedding: cache the prepared image for repeat prompts.
        return np.ascontiguousarray(rgb)

    def predict(self, prepared, points):
        height, width = prepared.shape[:2]
        masks, scores = [], []
        for tolerance, score in ((8, 0.6), (16, 0.5), (32, 0.4)):
            positive = np.zeros((height, width), dtype=bool)
            negative = np.zeros_like(positive)
            has_positive = False
            for x, y, label in points:
                filled = np.zeros((height + 2, width + 2), np.uint8)
                # Fixed range compares to the prompt color, preventing color drift.
                cv2.floodFill(
                    prepared, filled, (int(x), int(y)), (0, 0, 0),
                    (tolerance,) * 3, (tolerance,) * 3,
                    4 | cv2.FLOODFILL_MASK_ONLY | cv2.FLOODFILL_FIXED_RANGE | (255 << 8),
                )
                region = filled[1:-1, 1:-1] != 0
                if label == 1:
                    positive |= region
                    has_positive = True
                else:
                    negative |= region
            if not has_positive:
                positive[:] = True
            masks.append(positive & ~negative)
            scores.append(score)
        return np.stack(masks), np.asarray(scores)


class Sam2Backend:
    name = "sam2"

    def __init__(self, settings: Settings):
        os.environ.setdefault("HF_HOME", str(SERVICE_ROOT / ".cache" / "huggingface"))
        try:
            import torch
            from sam2.sam2_image_predictor import SAM2ImagePredictor
        except ImportError as exc:
            raise RuntimeError(
                "SAM 2 is unavailable. Install requirements-sam2.txt with SAM2_BUILD_CUDA=0 "
                "from the official GitHub repository, or start --backend floodfill."
            ) from exc
        self.torch = torch
        mps_available = torch.backends.mps.is_available()
        self.device = ("mps" if mps_available else "cpu") if settings.device == "auto" else settings.device
        if self.device == "mps" and not mps_available:
            raise RuntimeError("MPS was requested but torch.backends.mps.is_available() is false")
        self.model = settings.model_id
        # One model, with separate lightweight predictors holding each frame's state.
        self._predictor_type = SAM2ImagePredictor
        self._base = SAM2ImagePredictor.from_pretrained(self.model, device=self.device)
        self._base.model.eval()

    def synchronize(self):
        if self.device == "mps":
            self.torch.mps.synchronize()

    def embed(self, rgb):
        predictor = self._predictor_type(self._base.model)
        with self.torch.inference_mode():
            predictor.set_image(rgb)
        return predictor

    def predict(self, prepared, points):
        coords = np.asarray([[p[0], p[1]] for p in points], dtype=np.float32)
        labels = np.asarray([p[2] for p in points], dtype=np.int32)
        with self.torch.inference_mode():
            masks, scores, _ = prepared.predict(
                point_coords=coords, point_labels=labels, multimask_output=True,
            )
        return masks, scores


def make_backend(settings: Settings):
    return FloodfillBackend() if settings.backend == "floodfill" else Sam2Backend(settings)
