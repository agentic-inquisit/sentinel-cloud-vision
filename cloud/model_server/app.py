"""Sentinel model server: hosts the dashboard's models behind API-key auth.

Run:  uvicorn app:create_app --factory --port 8080
Env:  MODEL_GROUP     vision | nlp | timeseries | all   (which models this instance loads)
      API_KEY_HASHES  comma-separated SHA-256 hashes of valid keys (see keys.py)
"""

from __future__ import annotations

import io
import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from auth import ApiKeyAuth, parse_key_hashes
from catalog import MAX_SERIES_LENGTH, ModelNotServedHere, ModelUnavailable, ModelCatalog

log = logging.getLogger("model_server")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 10 * 1024 * 1024))


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


class FillMaskIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000, examples=["Paris is the [MASK] of France."])
    top_k: int = Field(5, ge=1, le=20)


class GenerateIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    max_new_tokens: int = Field(50, ge=1, le=200)
    temperature: float = Field(0.8, ge=0.0, le=2.0, description="0 = greedy decoding")


class SeriesIn(BaseModel):
    series: list[list[float]] = Field(
        min_length=1, max_length=MAX_SERIES_LENGTH, description="time steps x channels"
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def read_image(upload: UploadFile) -> Image.Image:
    data = upload.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"image larger than {MAX_UPLOAD_BYTES} bytes")
    try:
        image = Image.open(io.BytesIO(data))
        return image.convert("RGB")
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError):
        raise HTTPException(400, "file is not a readable image")


def run(catalog: ModelCatalog, name: str, *args, **kwargs):
    try:
        model = catalog.get(name)
    except ModelNotServedHere as exc:
        raise HTTPException(404, str(exc))
    except ModelUnavailable as exc:
        raise HTTPException(503, str(exc))
    except KeyError:
        raise HTTPException(404, f"unknown model '{name}'")
    try:
        return model.predict(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


def create_app(catalog: ModelCatalog | None = None, key_hashes: frozenset[str] | None = None) -> FastAPI:
    group = os.environ.get("MODEL_GROUP", "all")
    if key_hashes is None:
        key_hashes = parse_key_hashes(os.environ.get("API_KEY_HASHES"))
    if catalog is None:
        catalog = ModelCatalog.for_group(group)
    auth = ApiKeyAuth(key_hashes)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO)
        catalog.load_all()
        yield

    app = FastAPI(title="Sentinel Model Server", version="1.0.0", lifespan=lifespan)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok", "group": group, "ready": sorted(catalog.models)}

    api = APIRouter(prefix="/v1", dependencies=[Depends(auth)])

    @api.get("/models")
    def list_models():
        return {"group": group, "models": catalog.describe()}

    @api.post("/vision/classify")
    def classify(
        file: Annotated[UploadFile, File()],
        model: Annotated[Literal["resnet18", "resnet50"], Form()] = "resnet50",
        top_k: Annotated[int, Form(ge=1, le=20)] = 5,
    ):
        return {"model": model, "predictions": run(catalog, model, read_image(file), top_k=top_k)}

    @api.post("/vision/detect")
    def detect(
        file: Annotated[UploadFile, File()],
        conf: Annotated[float, Form(ge=0.01, le=1.0)] = 0.25,
    ):
        return {"model": "yolov8", "detections": run(catalog, "yolov8", read_image(file), conf=conf)}

    @api.post("/vision-text/match")
    def match(
        file: Annotated[UploadFile, File()],
        labels: Annotated[list[str], Form(description="candidate labels; repeat the field per label")],
    ):
        labels = [label.strip() for label in labels if label.strip()]
        if not 1 <= len(labels) <= 32 or any(len(label) > 100 for label in labels):
            raise HTTPException(422, "send 1-32 labels of at most 100 characters each")
        return {"model": "siglip2", "predictions": run(catalog, "siglip2", read_image(file), labels)}

    @api.post("/text/fill-mask")
    def fill_mask(body: FillMaskIn):
        return {"model": "bert", "predictions": run(catalog, "bert", body.text, top_k=body.top_k)}

    @api.post("/text/generate")
    def generate(body: GenerateIn):
        text = run(catalog, "gpt2", body.prompt, max_new_tokens=body.max_new_tokens,
                   temperature=body.temperature)
        return {"model": "gpt2", "generated_text": text}

    @api.post("/timeseries/{model}")
    def timeseries(model: Literal["tcn-segmenter", "tcn-sequence"], body: SeriesIn):
        return {"model": model, **run(catalog, model, body.series)}

    app.include_router(api)
    return app
