from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
from time import perf_counter

import numpy as np
from PIL import Image

from .format import Capture, column_major, matrix
from .fusion import background_patch, fuse_depth, simplify,reject_floor_fragments
from .geometry import object_local_frame,gravity_frame
from .extent import RefinedExtent
from .glb import write_glb
from .masking import propagate_masks
from .object_capture import run_object_capture
from .texturing import bake_texture
from .validation import validate_phone_glb, estimate_patch_coverage
from .quality import geometry_completeness,height_evidence
from .image_geometry import phone_images,reconstruct_images,texture_cameras,adaptive_depth


@dataclass
class PipelineOptions:
    mask_backend: str = "sam2"
    model: str = "facebook/sam2.1-hiera-tiny"
    device: str = "auto"
    object_capture: bool = True
    object_capture_timeout: float = 900
    geometry_backend: str = "auto"
    inpainting: str = "lama"
    voxel: float = .004

    @classmethod
    def from_env(cls):
        return cls(mask_backend=os.getenv("CAPTURE_MASK_BACKEND", "sam2"),
                   model=os.getenv("CAPTURE_SAM2_MODEL", "facebook/sam2.1-hiera-tiny"),
                   device=os.getenv("CAPTURE_DEVICE", "auto"),
                   object_capture=os.getenv("CAPTURE_OBJECT_CAPTURE", "1") == "1",
                   object_capture_timeout=float(os.getenv("CAPTURE_OBJECT_CAPTURE_TIMEOUT", "900")),
                   inpainting=os.getenv("CAPTURE_INPAINTING", "lama"),
                   geometry_backend=os.getenv("CAPTURE_GEOMETRY_BACKEND", "auto"))


@contextmanager
def inference_guard(lock, check):
    if lock is None:
        yield
        return
    while not lock.acquire(timeout=.2):
        check()
    try:
        check()
        yield
    finally:
        lock.release()


def process_capture(input_folder, work, update, check, *, options=None, inference_lock=None):
    import open3d as o3d
    options = options or PipelineOptions.from_env()
    work = Path(work)
    for name in ("masks", "intermediate", "outputs"):
        (work / name).mkdir(parents=True, exist_ok=True)
    timings, warnings = {}, []
    total_started = perf_counter()

    @contextmanager
    def stage(name):
        start = perf_counter()
        try:
            yield
        finally:
            timings[name] = perf_counter() - start

    update("validating", .02, "Validating persisted keyframes and coordinate mappings")
    with stage("validating"):
        capture = Capture.load(input_folder)
        check()
    update("masking", .08, "Propagating approved seed mask and checking depth consistency")
    with stage("masking"), inference_guard(inference_lock, check):
        masks, mask_report = propagate_masks(capture, work / "masks", check, backend=options.mask_backend, model=options.model, device=options.device)
    if options.mask_backend != "sam2":
        warnings.append("Explicit depth-bbox mask fallback/test mode was used; this is not SAM video segmentation quality evidence")
    if mask_report.get('device_fallback_reason'):
        warnings.append(mask_report['device_fallback_reason'])
    if mask_report["recovery_count"]:
        warnings.append(f"Depth-constrained recovery used on {mask_report['recovery_count']} frames; inspect the mask contact sheet")
    extent_report = mask_report['extent_refinement']
    extent = RefinedExtent.from_report(extent_report)
    silhouette_masks=[np.asarray(Image.open(work/'masks/raw_masks'/f'{f.index:04d}.png'))>0 for f in capture.manifest.frames] if extent.floor_visibility_exclusions else None
    if extent_report['seed_was_truncating']:
        warnings.append('Seed box was truncating multi-view object evidence; replaced with the refined object extent')
    if options.geometry_backend not in ('auto','photo','depth'):raise ValueError('Unknown geometry backend')
    image_mode=options.geometry_backend=='photo' or (options.geometry_backend=='auto' and options.object_capture and phone_images(capture))
    depth_mode=phone_images(capture) and not image_mode
    image_report={}
    if depth_mode:
        from ..scans.tapped import MaskFiles
        raw_masks=MaskFiles(work/'masks/raw_masks',capture.manifest.frames)
        update('reconstructing_object',.30,'Building the requested coarse depth fallback')
        with stage('adaptive_depth_fallback'):
            mesh,object_fusion=adaptive_depth(capture,raw_masks,check,extent=extent,silhouette_masks=raw_masks)
        method='adaptive_depth_fallback';registration={'status':'not_attempted'}
        depth_dimensions=extent.high-extent.low
        warnings.append('Explicit low-resolution depth fallback; inspect missing thin parts')
    elif image_mode:
        from ..scans.tapped import MaskFiles
        raw_dir=work/'masks/raw_masks';raw_masks=MaskFiles(raw_dir,capture.manifest.frames)
        update('reconstructing_object',.30,'Reconstructing masked high-resolution room photos')
        with stage('image_reconstruction'):
            try:
                with inference_guard(inference_lock,check):
                    mesh,image_report=reconstruct_images(capture,raw_dir,work/'intermediate/object-capture',check,timeout=options.object_capture_timeout)
                method=image_report['method'];registration=dict(image_report['registration'],status='registered')
            except Exception as exc:
                check();warnings.append(f'Image reconstruction failed; explicit coarse depth fallback: {exc}')
                image_report={'error':str(exc)}
                mesh,depth_report=adaptive_depth(capture,raw_masks,check,extent=extent,silhouette_masks=raw_masks)
                method='adaptive_depth_fallback';registration={'status':'failed','error':str(exc)}
                warnings.append('Low-resolution depth fallback needs review')
        depth_dimensions=extent.high-extent.low
        object_fusion={'frames_used':len(capture.manifest.frames),'outside_seed_fraction':None,'outside_refined_fraction':None,
                       'implementation':'not_run_for_image_primary','raw_depth_extent_dimensions_m':depth_dimensions.tolist()}
        if method=='adaptive_depth_fallback':object_fusion.update(depth_report)
    else:
        update("reconstructing_object", .30, "Fusing masked metric depth and trying masked Object Capture")
        with stage("object_depth_fusion"):
            depth_mesh, object_fusion, _ = fuse_depth(capture, masks, check, object_only=True, voxel=options.voxel, output=work / "intermediate/object-depth.ply", extent=extent,diagnostic_height=0.,silhouette_masks=silhouette_masks)
        mesh = depth_mesh
        method = "depth_fusion"
        registration = {"status": "not_attempted", "rms_m": None, "scale": None}
        depth_dimensions = object_local_frame(depth_mesh.vertices, capture.manifest.anchor_to_world)[2]
        if object_fusion["triangles_removed_by_extent"]:
            warnings.append("TSDF edge spill was clipped to the multi-view refined extent (including its recorded margin)")
        if options.object_capture and options.geometry_backend!='depth':
            with stage("object_capture"):
                try:
                    with inference_guard(inference_lock, check):
                        primary, registered = run_object_capture(capture, work / "masks", work / "intermediate/object-capture", check, timeout=options.object_capture_timeout)
                    primary_dimensions = object_local_frame(primary.vertices, capture.manifest.anchor_to_world)[2]
                    difference = np.abs(primary_dimensions - depth_dimensions)
                    if np.any(difference > np.maximum(.05, depth_dimensions * .20)):
                        raise ValueError(f"Registered Object Capture dimensions disagree with metric TSDF: {difference.tolist()} m")
                    mesh, method = primary, "object_capture"
                    registration = dict(registered, status="registered", dimensions_m=primary_dimensions.tolist())
                except Exception as exc:
                    check()  # Cancellation is never converted into a reconstruction fallback.
                    registration = {"status": "failed", "rms_m": None, "scale": None, "error": str(exc)}
                    warnings.append(f"Object Capture failed; explicit metric TSDF fallback: {exc}")
        else:
            warnings.append("Object Capture was explicitly disabled for this run")
    up=gravity_frame(capture.manifest.anchor_to_world)[:,1]
    simplification_before=height_evidence(mesh.vertices,up)
    if not (image_mode or depth_mode):mesh = simplify(mesh)
    simplified_floor_audit={}
    simplified_floor_removed=0 if image_mode or depth_mode else reject_floor_fragments(mesh,extent,capture=capture,silhouette_masks=silhouette_masks,check=check,audit=simplified_floor_audit)
    simplification_after=height_evidence(mesh.vertices,up)
    o3d.io.write_triangle_mesh(str(work / "intermediate/object-selected.ply"), mesh)
    local, object_to_anchor, dimensions = object_local_frame(mesh.vertices, capture.manifest.anchor_to_world)
    object_geometry=image_report['geometry_completeness'] if method=='apple_masked_photogrammetry' else geometry_completeness(mesh.vertices,extent,object_fusion.get('component_validation') if method=='depth_fusion' else None)
    if method=='adaptive_depth_fallback':object_geometry['low_quality']=True
    warnings.extend(object_geometry['warnings'])
    update("reconstructing_background", .48, "Fusing non-object depth and completing nearby supported 3D surfaces")
    with stage("background_depth_fusion"):
        background, background_fusion, cloud = fuse_depth(capture, masks, check, object_only=False, voxel=options.voxel, output=work / "intermediate/background-depth.ply", extent=extent)
    with stage("background_completion"):
        patch, background_report = background_patch(capture, background, cloud, check, extent=extent)
        o3d.io.write_triangle_mesh(str(work / "intermediate/background-patch.ply"), patch)
    update("texturing", .66, "Baking visible keyframe texels and completing unseen atlas texels")
    with stage("object_texturing"), inference_guard(inference_lock, check):
        object_arrays, object_texture_report = bake_texture(texture_cameras(capture,image_report) if image_mode else capture, raw_masks if image_mode or depth_mode else masks, mesh, check, object_only=True, inpainting=options.inpainting, device=options.device,visibility="mesh" if image_mode or depth_mode else "depth")
    with stage("background_texturing"), inference_guard(inference_lock, check):
        background_arrays, background_texture_report = bake_texture(capture, masks, patch, check, object_only=False, inpainting=options.inpainting, device=options.device)
    for name, texture_report in (("object", object_texture_report), ("background", background_texture_report)):
        warnings.extend(f"{name}: {message}" for message in texture_report["inpainting"]["warnings"])
        if texture_report["low_quality"]:
            warnings.append(f"{name} has over 40% inpainted surface texels; low quality")
    if min(dimensions) < .03:
        warnings.append("Thin object geometry may be unreliable at this depth resolution")
    if min(frame.sharpness for frame in capture.manifest.frames) < 30:
        warnings.append("Low-sharpness/textureless capture frames detected")
    angles = np.sort(np.array([frame.orbit_deg % 360 for frame in capture.manifest.frames]))
    gap = float(np.max(np.diff(np.r_[angles, angles[0] + 360])))
    if gap > 45:
        warnings.append(f"Orbit coverage has a {gap:.1f} degree gap; hidden geometry may be incomplete")
    if len(capture.manifest.frames) < 30:
        warnings.append("Fewer than 30 views; capture additional heights/sides for reliable geometry")
    pose_disagreement = max(np.linalg.norm((matrix(capture.manifest.anchor_to_world) @ matrix(frame.camera_to_anchor) - matrix(frame.camera_to_world))[:3, 3]) for frame in capture.manifest.frames)
    if pose_disagreement > .02:
        warnings.append("World/anchor pose composition differs by more than 2 cm; ARCore tracking may have refined")
    warnings.append("Reflective, transparent or unobserved surfaces may remain approximate; review source-view comparisons")
    warnings.append("Background geometry is valid only near observed support/scan viewpoints; unseen shape is not guaranteed")
    update("exporting", .94, "Writing the phone-compatible metric GLBs and reconstruction report")
    with stage("exporting"):
        check()
        positions, normals, uv, faces, texture = object_arrays
        basis, origin = object_to_anchor[:3, :3], object_to_anchor[:3, 3]
        write_glb(work / "outputs/object.glb", (positions - origin) @ basis, normals @ basis, uv, faces, texture)
        write_glb(work / "outputs/background.glb", *background_arrays)
        object_contract = validate_phone_glb(work / "outputs/object.glb",require_closed=image_mode or depth_mode)
        background_contract = validate_phone_glb(work / "outputs/background.glb")
    with stage("background_view_validation"):
        check()
        view_coverage = estimate_patch_coverage(capture, work / "outputs/object.glb", work / "outputs/background.glb", column_major(object_to_anchor))
    insufficient_coverage = view_coverage["minimum_fraction"] < .98
    if insufficient_coverage:
        warnings.append("3D background patch covers less than 98% of the reconstructed object from a nearby scan view; capture more support surfaces")
    report = {
        "schema": "housingar.capture.report.v1", "capture_id": capture.manifest.capture_id,
        "session_id": capture.manifest.session_id, "frames_received": len(capture.manifest.frames),
        "frames_used": object_fusion["frames_used"], "masking": mask_report,
        "method": method, "registration": registration,"image_reconstruction":image_report,
        "registration_rms_m": registration.get("rms_m"), "registration_scale": registration.get("scale"),
        "object_dimensions_m": dimensions.tolist(), "object_to_anchor": column_major(object_to_anchor),
        "extent_refinement": extent_report,
        "fused_object_points_outside_seed_fraction": object_fusion['outside_seed_fraction'],
        "fused_object_points_outside_refined_fraction": object_fusion['outside_refined_fraction'],
        "object_frame": "+Y gravity up, bottom-centre origin, metres", "background_frame": "anchor, metres",
        "object_triangles": len(object_arrays[3]), "background_triangles": len(background_arrays[3]),
        "glb_validation": {"object": object_contract, "background": background_contract},
        "object_depth_comparison": {"dimensions_m": depth_dimensions.tolist(), **object_fusion},
        "object_geometry_completeness":object_geometry,
        "object_simplification":{'reference_height_m':0.,'reference':'gravity plane through capture anchor origin',
            'before':simplification_before,'after':simplification_after,'floor_triangles_removed':simplified_floor_removed,'floor_mesh_validation':simplified_floor_audit},
        "background_depth_fusion": background_fusion, "background_geometry": background_report,
        "background_view_coverage": view_coverage,
        "object_texture": object_texture_report, "background_texture": background_texture_report,
        "inpainted_fraction": background_texture_report["inpainted_texel_fraction"],
        "object_low_quality": object_geometry['low_quality'] or object_texture_report['low_quality'],
        "background_low_quality": background_texture_report["low_quality"] or insufficient_coverage,
        "quality_scope": "object" if image_mode or depth_mode else "object_and_background",
        "low_quality": object_geometry['low_quality'] or object_texture_report['low_quality'] or ((background_texture_report["low_quality"] or insufficient_coverage) and not (image_mode or depth_mode)),
        "max_orbit_gap_deg": gap, "world_anchor_translation_disagreement_m": float(pose_disagreement),
        "timings_seconds": timings, "total_seconds": perf_counter() - total_started,
        "warnings": warnings,
    }
    check()
    (work / "outputs/report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return report
