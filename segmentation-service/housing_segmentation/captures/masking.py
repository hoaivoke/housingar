import gc
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from .geometry import anchor_depth_points, box_corners, depth_bbox_mask, depth_to_image, projected_box
from .extent import refine_extent


def contact_sheet(capture, masks, output):
    columns = 6
    tile_w, tile_h = 240, 200
    sheet = Image.new("RGB", (columns * tile_w, int(np.ceil(len(masks) / columns)) * tile_h), "#20262d")
    draw = ImageDraw.Draw(sheet)
    for i, (frame, mask) in enumerate(zip(capture.manifest.frames, masks)):
        rgb = capture.image(frame)
        overlay = rgb.copy()
        overlay[mask] = (.55 * overlay[mask] + .45 * np.array([30, 235, 125])).astype(np.uint8)
        image = Image.fromarray(overlay)
        image.thumbnail((tile_w, tile_h - 25))
        x, y = (i % columns) * tile_w, (i // columns) * tile_h
        sheet.paste(image, (x, y))
        draw.text((x + 5, y + tile_h - 22), f"frame {frame.index} | {mask.mean():.1%}", fill="white")
    sheet.save(output, quality=90)


def propagate_masks(capture, work, check, *, backend="sam2", model="facebook/sam2.1-hiera-tiny", device="auto", predictor=None):
    """Seeded official video predictor, with depth-constrained geometric recovery."""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    frames = capture.manifest.frames
    seed_position = next(i for i, frame in enumerate(frames) if frame.index == capture.manifest.seed.frame_index)
    with Image.open(capture.root / capture.manifest.seed.mask) as image:
        seed = np.array(image) != 0
    owned_predictor = predictor is None
    state = None
    raw = {}
    actual_device = "cpu"
    normalized_size = (frames[seed_position].width, frames[seed_position].height)
    normalization_scale = min(1., 960 / max(normalized_size))
    normalized_size = tuple(max(1, round(v * normalization_scale)) for v in normalized_size)
    if backend == "sam2":
        import os
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        from ..config import SERVICE_ROOT
        os.environ.setdefault("HF_HOME", str(SERVICE_ROOT / ".cache/huggingface"))
        import torch
        actual_device = ("mps" if torch.backends.mps.is_available() else "cpu") if device == "auto" else device
        if predictor is None:
            if actual_device == 'cpu':
                torch.set_num_threads(min(4, os.cpu_count() or 1))
            from sam2.sam2_video_predictor import SAM2VideoPredictor
            predictor = SAM2VideoPredictor.from_pretrained(model, device=actual_device, apply_postprocessing=False)
        video_dir = work / "sam_video"
        video_dir.mkdir(exist_ok=True)
        for i, frame in enumerate(frames):
            check()
            Image.fromarray(capture.image(frame)).resize(normalized_size).save(video_dir / f"{i:05d}.jpg", quality=95)
        state = predictor.init_state(video_path=str(video_dir), offload_video_to_cpu=True, offload_state_to_cpu=True)
        normalized_seed = cv2.resize(seed.astype(np.uint8), normalized_size, interpolation=cv2.INTER_NEAREST) > 0
        predictor.add_new_mask(state, frame_idx=seed_position, obj_id=1, mask=normalized_seed)
        for reverse in (False, True):
            for i, ids, logits in predictor.propagate_in_video(state, start_frame_idx=seed_position, reverse=reverse):
                check()
                object_index = list(ids).index(1)
                mask = (logits[object_index, 0] > 0).detach().cpu().numpy()
                raw[i] = cv2.resize(mask.astype(np.uint8), (frames[i].width, frames[i].height), interpolation=cv2.INTER_NEAREST) > 0
        empty_count = sum(mask.sum() < 20 for i, mask in raw.items() if i != seed_position)
        if actual_device == 'mps' and owned_predictor and empty_count > (len(frames) - 1) / 2:
            # Finite -1024 logits can still be a device-specific collapsed
            # video track. Repeat the official model on CPU before deriving
            # geometry; empty masks must not make the seed box authoritative.
            del predictor, state
            gc.collect()
            torch.mps.empty_cache()
            masks, report = propagate_masks(capture, work, check, backend=backend, model=model, device='cpu')
            report['requested_device'] = device
            report['device_fallback_reason'] = f'MPS video returned empty masks on {empty_count}/{len(frames)-1} non-seed frames; repeated official SAM video on CPU'
            return masks, report
    elif backend != "depth_bbox":
        raise ValueError("Mask backend must be sam2 or explicit depth_bbox test/fallback mode")
    masks, reports = [], []
    try:
        recoveries = set()
        # Recover empty video tracks before collecting 3D evidence. The seed
        # bbox prompts SAM, but never adds depth or clips observations here.
        if backend == "sam2":
            corners = box_corners(capture.manifest.seed.bbox_anchor)
            for i, frame in enumerate(frames):
                check()
                if i == seed_position or raw.get(i, np.zeros((1, 1))).sum() >= 20:
                    continue
                box = projected_box(corners, frame)
                if box is not None:
                    scale = np.array([normalized_size[0] / frame.width, normalized_size[1] / frame.height] * 2)
                    predictor.add_new_points_or_box(state, frame_idx=i, obj_id=1, box=box * scale)
                    for predicted_index, ids, logits in predictor.propagate_in_video(state, start_frame_idx=i, max_frame_num_to_track=1):
                        if predicted_index == i:
                            proposal = (logits[list(ids).index(1), 0] > 0).detach().cpu().numpy()
                            raw[i] = cv2.resize(proposal.astype(np.uint8), (frame.width, frame.height), interpolation=cv2.INTER_NEAREST) > 0
                            recoveries.add(i)
                            break
        evidence = [seed.copy() if i == seed_position else raw.get(i, np.zeros((f.height, f.width), bool)) for i, f in enumerate(frames)]
        raw_dir = work / 'raw_masks'
        raw_dir.mkdir(exist_ok=True)
        for frame, mask in zip(frames, evidence):
            Image.fromarray(mask.astype(np.uint8) * 255).save(raw_dir / f'{frame.index:04d}.png')
        extent, extent_report = refine_extent(capture, evidence, check, geometric_fallback=backend == 'depth_bbox')
        for i, frame in enumerate(frames):
            check()
            geometric, valid_depth = depth_bbox_mask(capture, frame, extent=extent)
            candidate = seed.copy() if i == seed_position else raw.get(i, np.zeros_like(geometric))
            candidate &= ~valid_depth | geometric
            recovery = i in recoveries
            if backend == "depth_bbox" and i != seed_position:
                candidate = geometric.copy()
                recovery = True
            if i == seed_position:
                # The accepted seed is authoritative; depth can only remove inconsistent pixels.
                candidate = seed & (~valid_depth | geometric)
            else:
                # Fill only the multi-view refined extent, never support planes.
                candidate |= geometric
            candidate = np.ascontiguousarray(candidate)
            masks.append(candidate)
            Image.fromarray(candidate.astype(np.uint8) * 255).save(work / f"{frame.index:04d}.png")
            reports.append({"frame_index": frame.index, "coverage": float(candidate.mean()),
                            "depth_recovery": recovery, "mask_backend": backend})
    finally:
        if backend == "sam2" and owned_predictor:
            del predictor, state
            gc.collect()
            if actual_device == "mps":
                import torch
                torch.mps.empty_cache()
    contact_sheet(capture, masks, work / "contact_sheet.jpg")
    return masks, {"backend": backend, "model": model if backend == "sam2" else None,
                   "device": actual_device, "frames": reports, "recovery_count": sum(r["depth_recovery"] for r in reports),
                   "extent_refinement": extent_report}
