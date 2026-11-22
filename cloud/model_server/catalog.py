"""Model catalogue and loaders.

Heavy libraries (torch, transformers, ultralytics) are imported inside the
loaders so the API layer can be tested without them.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

MODEL_DIR = os.environ.get("MODEL_DIR", "/models")
TCN_WEIGHTS_DIR = os.environ.get("TCN_WEIGHTS_DIR", "/mnt/tcn-weights")
MAX_SERIES_LENGTH = 10_000


class ModelUnavailable(Exception):
    """A model that is expected to be missing until someone supplies weights."""


class ModelNotServedHere(Exception):
    def __init__(self, name: str, group: str):
        super().__init__(f"model '{name}' is served by the '{group}' service")


# --------------------------------------------------------------------------- #
# Model wrappers: each exposes predict(...) returning JSON-serialisable data.
# --------------------------------------------------------------------------- #


class ResNetClassifier:
    def __init__(self, arch: str):
        import torch
        import torchvision.models as tvm

        weights = {
            "resnet18": tvm.ResNet18_Weights.IMAGENET1K_V1,
            "resnet50": tvm.ResNet50_Weights.IMAGENET1K_V2,
        }[arch]
        self._torch = torch
        self.model = getattr(tvm, arch)(weights=weights).eval()
        self.transform = weights.transforms()
        self.labels = weights.meta["categories"]

    def predict(self, image, top_k: int = 5) -> list[dict]:
        with self._torch.inference_mode():
            probs = self.model(self.transform(image).unsqueeze(0)).softmax(-1)[0]
        scores, idx = probs.topk(top_k)
        return [
            {"label": self.labels[i], "score": round(s, 5)}
            for s, i in zip(scores.tolist(), idx.tolist())
        ]


class YoloDetector:
    def __init__(self, path: str):
        from ultralytics import YOLO

        self.model = YOLO(path)

    def predict(self, image, conf: float = 0.25) -> list[dict]:
        result = self.model.predict(image, conf=conf, verbose=False)[0]
        boxes = result.boxes
        return [
            {
                "label": result.names[int(c)],
                "score": round(s, 5),
                "box_xyxy": [round(v, 1) for v in b],
            }
            for b, s, c in zip(boxes.xyxy.tolist(), boxes.conf.tolist(), boxes.cls.tolist())
        ]


class SiglipMatcher:
    """Zero-shot image/text matching. Scores are independent sigmoids, not a softmax."""

    def __init__(self, model_id: str):
        import torch
        from transformers import AutoModel, AutoProcessor

        self._torch = torch
        self.model = AutoModel.from_pretrained(model_id).eval()
        self.processor = AutoProcessor.from_pretrained(model_id)

    def predict(self, image, labels: list[str], template: str = "This is a photo of {}.") -> list[dict]:
        texts = [template.format(label) for label in labels]
        inputs = self.processor(
            text=texts, images=image, padding="max_length", max_length=64, return_tensors="pt"
        )
        with self._torch.inference_mode():
            logits = self.model(**inputs).logits_per_image[0]
        scores = self._torch.sigmoid(logits).tolist()
        ranked = sorted(zip(labels, scores), key=lambda p: p[1], reverse=True)
        return [{"label": label, "score": round(s, 5)} for label, s in ranked]


class BertFillMask:
    def __init__(self, model_id: str):
        from transformers import pipeline

        self.pipe = pipeline("fill-mask", model=model_id, device=-1)
        self.mask_token = self.pipe.tokenizer.mask_token

    def predict(self, text: str, top_k: int = 5) -> list[dict]:
        if text.count(self.mask_token) != 1:
            raise ValueError(f"text must contain exactly one {self.mask_token} token")
        return [
            {"token": r["token_str"], "score": round(float(r["score"]), 5), "sequence": r["sequence"]}
            for r in self.pipe(text, top_k=top_k)
        ]


class Gpt2Generator:
    def __init__(self, model_id: str):
        from transformers import pipeline

        self.pipe = pipeline("text-generation", model=model_id, device=-1)

    def predict(self, prompt: str, max_new_tokens: int = 50, temperature: float = 0.8) -> str:
        kwargs: dict[str, Any] = {"do_sample": temperature > 0}
        if temperature > 0:
            kwargs["temperature"] = temperature
        out = self.pipe(
            prompt,
            max_new_tokens=max_new_tokens,
            pad_token_id=self.pipe.tokenizer.eos_token_id,
            return_full_text=False,
            **kwargs,
        )
        return out[0]["generated_text"]


class TcnModel:
    def __init__(self, name: str):
        path = os.path.join(TCN_WEIGHTS_DIR, f"{name}.pt")
        if not os.path.isfile(path):
            raise ModelUnavailable(f"no trained weights at {path}; upload {name}.pt to the TCN weights bucket")
        import torch

        from tcn import load_tcn_checkpoint

        self._torch = torch
        self.model, self.labels = load_tcn_checkpoint(path)

    def predict(self, series: list[list[float]]) -> dict:
        x = self._torch.tensor(series, dtype=self._torch.float32)
        if x.ndim != 2 or x.shape[1] != self.model.in_channels:
            raise ValueError(f"series must be a list of time steps, each with {self.model.in_channels} values")
        with self._torch.inference_mode():
            probs = self.model(x.T.unsqueeze(0))[0].softmax(0)
        if self.model.mode == "segmenter":  # probs: K x T
            conf, idx = probs.max(0)
            return {
                "labels": [self.labels[i] for i in idx.tolist()],
                "scores": [round(c, 5) for c in conf.tolist()],
            }
        ranked = sorted(zip(self.labels, probs.tolist()), key=lambda p: p[1], reverse=True)
        return {"predictions": [{"label": label, "score": round(s, 5)} for label, s in ranked]}


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelSpec:
    name: str
    group: str  # which Cloud Run service hosts it
    task: str
    source: str
    load: Callable[[], Any]


SPECS: list[ModelSpec] = [
    ModelSpec("resnet18", "vision", "image-classification", "torchvision ResNet18 (ImageNet-1k)",
              lambda: ResNetClassifier("resnet18")),
    ModelSpec("resnet50", "vision", "image-classification", "torchvision ResNet50 (ImageNet-1k V2)",
              lambda: ResNetClassifier("resnet50")),
    ModelSpec("yolov8", "vision", "object-detection", "Ultralytics YOLOv8n (COCO)",
              lambda: YoloDetector(os.path.join(MODEL_DIR, "yolov8n.pt"))),
    ModelSpec("siglip2", "nlp", "image-text-matching", "google/siglip2-base-patch16-224",
              lambda: SiglipMatcher("google/siglip2-base-patch16-224")),
    ModelSpec("bert", "nlp", "fill-mask", "google-bert/bert-base-uncased",
              lambda: BertFillMask("google-bert/bert-base-uncased")),
    ModelSpec("gpt2", "nlp", "text-generation", "openai-community/gpt2",
              lambda: Gpt2Generator("openai-community/gpt2")),
    ModelSpec("tcn-segmenter", "timeseries", "sequence-segmentation", "your checkpoint: tcn-segmenter.pt",
              lambda: TcnModel("tcn-segmenter")),
    ModelSpec("tcn-sequence", "timeseries", "sequence-classification", "your checkpoint: tcn-sequence.pt",
              lambda: TcnModel("tcn-sequence")),
]

GROUPS = sorted({s.group for s in SPECS}) + ["all"]


class ModelCatalog:
    def __init__(self, specs: list[ModelSpec], all_specs: list[ModelSpec] | None = None):
        self.specs = {s.name: s for s in specs}
        self.all_specs = {s.name: s for s in (all_specs if all_specs is not None else specs)}
        self.models: dict[str, Any] = {}
        self.unavailable: dict[str, str] = {}

    @classmethod
    def for_group(cls, group: str) -> "ModelCatalog":
        if group not in GROUPS:
            raise ValueError(f"MODEL_GROUP must be one of {GROUPS}, got {group!r}")
        specs = SPECS if group == "all" else [s for s in SPECS if s.group == group]
        return cls(specs, all_specs=SPECS)

    def load_all(self) -> None:
        for spec in self.specs.values():
            try:
                self.models[spec.name] = spec.load()
                log.info("loaded %s", spec.name)
            except ModelUnavailable as exc:  # anything else should fail the deploy
                self.unavailable[spec.name] = str(exc)
                log.warning("%s unavailable: %s", spec.name, exc)

    def get(self, name: str):
        """Return the loaded model, or raise ModelNotServedHere / ModelUnavailable / KeyError."""
        if name in self.models:
            return self.models[name]
        if name in self.unavailable:
            raise ModelUnavailable(self.unavailable[name])
        if name in self.all_specs:
            raise ModelNotServedHere(name, self.all_specs[name].group)
        raise KeyError(name)

    def describe(self) -> list[dict]:
        out = []
        for spec in self.all_specs.values():
            if spec.name in self.models:
                status, detail = "ready", None
            elif spec.name in self.unavailable:
                status, detail = "unavailable", self.unavailable[spec.name]
            else:
                status, detail = "other-service", f"served by the '{spec.group}' service"
            out.append({"name": spec.name, "task": spec.task, "group": spec.group,
                        "source": spec.source, "status": status, "detail": detail})
        return out
