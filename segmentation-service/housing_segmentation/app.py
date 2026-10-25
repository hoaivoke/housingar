from contextlib import asynccontextmanager
import json
import logging
import math
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictInt, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from .backends import make_backend
from .config import SERVICE_ROOT, Settings
from .engine import InputError, SegmentationEngine
from .storage import Storage, validate_session_id

logger = logging.getLogger(__name__)


class Intrinsics(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    fx: Annotated[FiniteFloat, Field(gt=0)]
    fy: Annotated[FiniteFloat, Field(gt=0)]
    cx: FiniteFloat
    cy: FiniteFloat


class FrameMetadata(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    session_id: str
    timestamp_ns: Annotated[StrictInt, Field(ge=0)]
    intrinsics: Intrinsics
    camera_pose_world: Annotated[list[FiniteFloat], Field(min_length=7, max_length=7)]
    sensor_to_display_rotation_deg: StrictInt
    depth_timestamp_ns: Annotated[StrictInt, Field(ge=0)]

    @field_validator("session_id")
    @classmethod
    def session_is_safe(cls, value):
        return validate_session_id(value)

    @field_validator("sensor_to_display_rotation_deg")
    @classmethod
    def rotation_is_valid(cls, value):
        if value not in (0, 90, 180, 270):
            raise ValueError("rotation must be 0, 90, 180 or 270")
        return value


def parse_json(text, field):
    def reject_constant(value):
        raise ValueError(f"non-finite JSON value {value}")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("numeric value is outside the finite float range")
        return number
    try:
        return json.loads(text, parse_constant=reject_constant, parse_float=finite_float)
    except (json.JSONDecodeError, ValueError) as exc:
        raise InputError(f"{field} is not valid JSON: {exc}", 400) from exc


def create_app(settings: Settings | None = None, *, capture_runner=None, capture_options=None, scan_runner=None, scan_options=None,
               listings_client=None, rentcast_api_key=None, web_dist=None, listings_dataset=None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application):
        # Model loading and all image/inference/storage work stay off the event loop.
        backend = await run_in_threadpool(make_backend, settings)
        application.state.engine = SegmentationEngine(settings, backend)
        application.state.storage = await run_in_threadpool(Storage, settings.data_dir)
        from .captures.jobs import JobManager
        application.state.jobs = await run_in_threadpool(JobManager, settings.data_dir / "captures",
            runner=capture_runner, options=capture_options, inference_lock=application.state.engine._lock,scan_runner=scan_runner,scan_options=scan_options)
        from .assets import Assets
        application.state.assets=await run_in_threadpool(Assets,settings.data_dir,application.state.jobs)
        from .listings.service import Listings
        application.state.listings=await run_in_threadpool(Listings,settings.data_dir,application.state.assets,
                                                         client=listings_client,api_key=rentcast_api_key,
                                                         dataset=listings_dataset)
        logger.info("Active backend=%s model=%s device=%s", backend.name, backend.model, backend.device)
        try:
            yield
        finally:
            await run_in_threadpool(application.state.listings.close)
            await run_in_threadpool(application.state.jobs.close)
            application.state.engine.clear_cache()

    application = FastAPI(title="Housing AR segmentation", version="0.1.0", lifespan=lifespan)
    from .site import ApiCORS
    application.add_middleware(ApiCORS)

    @application.exception_handler(InputError)
    async def input_error_handler(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @application.get("/health")
    async def health():
        backend = application.state.engine.backend
        return {"status": "ok", "backend": backend.name, "model": backend.model, "device": backend.device}

    @application.post("/v1/segment")
    async def segment(
        frame_id: Annotated[str, Form(min_length=1, max_length=256)],
        points: Annotated[str, Form()],
        metadata: Annotated[str, Form()],
        image: Annotated[UploadFile, File()],
    ):
        parsed_points = parse_json(points, "points")
        parsed_metadata = parse_json(metadata, "metadata")
        try:
            validated = FrameMetadata.model_validate(parsed_metadata)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors(include_context=False)) from exc
        try:
            content = await image.read(settings.max_image_bytes + 1)
        finally:
            await image.close()
        result = await run_in_threadpool(
            application.state.engine.segment, content, frame_id, validated.session_id, parsed_points,
        )
        await run_in_threadpool(
            application.state.storage.save_frame,
            content, validated.model_dump(mode="json"), parsed_points, result,
        )
        return result

    @application.post("/v1/diagnostics")
    async def diagnostics(payload: dict[str, Any]):
        return await run_in_threadpool(application.state.storage.save_diagnostics, payload)

    @application.post("/v1/logs/drift")
    async def drift(payload: dict[str, Any]):
        return await run_in_threadpool(application.state.storage.append_drift, payload)

    @application.post("/v1/geometry")
    async def geometry(payload: dict[str, Any]):
        return await run_in_threadpool(application.state.storage.save_geometry, payload)

    @application.get("/v1/logs")
    async def logs():
        return await run_in_threadpool(application.state.storage.list_logs)

    @application.post("/v1/captures", status_code=202)
    async def upload_capture(manifest: Annotated[str, Form()], archive: Annotated[UploadFile, File()]):
        payload = parse_json(manifest, "manifest")
        try:
            return await run_in_threadpool(application.state.jobs.ingest, payload, archive.file)
        finally:
            await archive.close()

    @application.get("/v1/jobs/{job_id}")
    async def job_status(job_id: str):
        return await run_in_threadpool(application.state.jobs.get, job_id)

    @application.post("/v1/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        return await run_in_threadpool(application.state.jobs.cancel, job_id)

    @application.get("/v1/jobs/{job_id}/files/{name}")
    async def job_file(job_id: str, name: str):
        path = await run_in_threadpool(application.state.jobs.output_file, job_id, name)
        return FileResponse(path, media_type="application/json" if name.endswith(".json") else "model/gltf-binary", filename=name)

    @application.post('/v1/scans',status_code=202)
    async def upload_scan(manifest:Annotated[str,Form()],archive:Annotated[UploadFile,File()]):
        payload=parse_json(manifest,'manifest')
        try:return await run_in_threadpool(application.state.jobs.ingest_scan,payload,archive.file)
        finally:await archive.close()

    @application.get('/v1/scans/{scan_id}/proposals')
    async def scan_proposals(scan_id:str):
        return await run_in_threadpool(application.state.assets.proposals,scan_id)

    @application.get('/v1/scans/{scan_id}/proposals/{proposal_id}/{name}')
    async def proposal_file(scan_id:str,proposal_id:str,name:str):
        path=await run_in_threadpool(application.state.assets.proposal_file,scan_id,proposal_id,name)
        return FileResponse(path,media_type='image/jpeg' if name.endswith('.jpg') else 'model/gltf-binary')

    @application.post('/v1/library',status_code=201)
    async def add_item(payload:dict[str,Any]):
        return await run_in_threadpool(application.state.assets.add,payload)

    @application.get('/v1/library')
    async def library():return await run_in_threadpool(application.state.assets.list_items)

    @application.patch('/v1/library/{item_id}')
    async def rename_item(item_id:str,payload:dict[str,Any]):
        return await run_in_threadpool(application.state.assets.rename,item_id,payload)

    @application.delete('/v1/library/{item_id}')
    async def delete_item(item_id:str):return await run_in_threadpool(application.state.assets.delete,item_id)

    @application.get('/v1/library/{item_id}/{name}')
    async def library_file(item_id:str,name:str):
        path=await run_in_threadpool(application.state.assets.item_file,item_id,name)
        return FileResponse(path,media_type='image/jpeg' if name.endswith('.jpg') else 'model/gltf-binary')

    @application.post('/v1/layouts')
    async def save_layout(payload:dict[str,Any]):return await run_in_threadpool(application.state.assets.save_layout,payload)

    @application.get('/v1/layouts')
    async def layouts():return await run_in_threadpool(application.state.assets.layouts)

    from .listings.routes import router
    from .site import mount_site
    application.include_router(router)
    mount_site(application,web_dist if web_dist is not None else SERVICE_ROOT.parent/'web'/'dist')
    return application


app = create_app()
