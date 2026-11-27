"""Routing contract shared by ingest, the detectors and the Gateway manifests.

The internal Gateway's GRPCRoute (deploy/k8s/streaming/inference-route.yaml)
matches these gRPC metadata headers. Only the ingest service sets them: it drops
any routing header a client sends and rebuilds them from validated values.
"""

from __future__ import annotations

MODEL = "detector"

HEADER_MODEL = "x-sentinel-model"
HEADER_VERSION = "x-sentinel-model-version"
HEADER_DEVICE_CLASS = "x-sentinel-device-class"
ROUTING_HEADERS = (HEADER_MODEL, HEADER_VERSION, HEADER_DEVICE_CLASS)

# version -> backend model. Both are models already used elsewhere in the repo.
VERSIONS = {
    "v1": "fasterrcnn_resnet50_fpn",  # edge/vision_module.py ObjectDetector
    "v2": "yolov8n",                  # cloud/model_server/catalog.py YoloDetector
}
DEVICE_CLASSES = ("camera", "mobile", "edge-agent")


def routing_metadata(device_class: str, pinned_version: str | None = None) -> tuple[tuple[str, str], ...]:
    """Metadata ingest attaches to every Detect call."""
    if device_class not in DEVICE_CLASSES:
        raise ValueError(f"unknown device class {device_class!r}")
    md = [(HEADER_MODEL, MODEL), (HEADER_DEVICE_CLASS, device_class)]
    if pinned_version is not None:
        if pinned_version not in VERSIONS:
            raise ValueError(f"unknown model version {pinned_version!r}; expected one of {sorted(VERSIONS)}")
        md.append((HEADER_VERSION, pinned_version))
    return tuple(md)
