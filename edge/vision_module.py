"""
Sentinel Vision Module - object detection service.

Keeps only what the GCP architecture uses: the FasterRCNN detector, frame
sources (webcam / mobile WebSocket), /detect, /model-info and /metrics.
Dashboards, fine-tuning, labeling, registry, A/B and mock-JAX routes were
moved to _archive/irrelevant/originals/edge/vision_module.py.
"""
import base64
import os
import time

import cv2
import numpy as np
import torch
from torchvision.models.detection import fasterrcnn_resnet50_fpn, FasterRCNN_ResNet50_FPN_Weights
from torchvision.transforms import functional as F
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

app = FastAPI(title="Sentinel Vision Module")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Prometheus Metrics ---
REQUEST_COUNT = Counter(
    "vision_request_count", "Total number of vision inference requests", ["method", "endpoint", "status"]
)
INFERENCE_LATENCY = Histogram(
    "vision_inference_latency_seconds", "Latency of object detection inference", ["source"]
)

class WebcamStream:
    """Handles webcam frame capture using OpenCV."""
    def __init__(self, src=0):
        self.stream = cv2.VideoCapture(src)
        if not self.stream.isOpened():
            print(f"Warning: Could not open webcam {src}. Using dummy stream.")
            self.stream = None
        
    def get_frame(self):
        if self.stream is None:
            # Return dummy black frame with noise or text to indicate no camera
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(frame, "NO CAMERA", (200, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            return frame

        ret, frame = self.stream.read()
        if not ret:
            return None
        return frame

    def release(self):
        if self.stream:
            self.stream.release()

class MobileFrameSource:
    """Handles frames received via WebSocket from a mobile device."""
    def __init__(self):
        self.last_frame = None
        self.active = False
        self.last_update = 0
    
    def update_frame(self, frame_bytes):
        # Decode image from bytes
        nparr = np.frombuffer(frame_bytes, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if frame is not None:
             self.last_frame = frame
             self.last_update = time.time()
             self.active = True

    def get_frame(self):
        # Timeout if no frame for 5 seconds
        if time.time() - self.last_update > 5:
            self.active = False
            return None
        return self.last_frame

class ObjectDetector:
    """Handles object detection using torchvision's Faster R-CNN."""
    def __init__(self, threshold=0.5):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        # Initialize model with pre-trained weights
        weights = FasterRCNN_ResNet50_FPN_Weights.DEFAULT
        self.model = fasterrcnn_resnet50_fpn(weights=weights, box_score_thresh=threshold)
        self.model.to(self.device)
        self.model.eval()
        self.categories = weights.meta["categories"]

    def detect(self, frame):
        # Convert BGR (OpenCV) to RGB (Torchvision)
        img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        # Convert to tensor and add batch dimension
        img_tensor = F.to_tensor(img_rgb).to(self.device).unsqueeze(0)
        
        with torch.no_grad():
            prediction = self.model(img_tensor)[0]
        
        return prediction

    def get_labels(self, prediction):
        labels = [self.categories[i] for i in prediction['labels']]
        scores = prediction['scores'].tolist()
        boxes = prediction['boxes'].tolist()
        return list(zip(labels, scores, boxes))

# Global instances
stream = None
mobile_source = None
detector = None
camera_enabled = False
camera_permission_granted = False

@app.on_event("startup")
async def startup_event():
    global mobile_source
    # DO NOT auto-initialize camera stream - only on explicit request
    mobile_source = MobileFrameSource()

@app.get("/model-info")
async def get_model_info():
    """Get detailed information about deployed object detection model"""
    weights = FasterRCNN_ResNet50_FPN_Weights.DEFAULT
    categories = weights.meta["categories"]

    return {
        "model": {
            "name": "FasterRCNN ResNet50 FPN",
            "framework": "PyTorch + Torchvision",
            "architecture": "Faster R-CNN with ResNet50 backbone and FPN",
            "weights": "COCO pretrained (FasterRCNN_ResNet50_FPN_Weights.DEFAULT)",
            "input_size": "Variable (min 640x480 recommended)",
            "output": "Bounding boxes, class labels, confidence scores",
            "device": "CUDA" if torch.cuda.is_available() else "CPU",
            "pretrained_on": "COCO dataset (80 object classes)",
            "confidence_threshold": float(os.getenv("DETECTION_THRESHOLD", 0.7))
        },
        "supported_classes": {
            "total": len(categories),
            "classes": sorted(categories)
        },
        "supported_sources": ["webcam", "mobile", "file_upload"],
        "features": [
            "Real-time object detection from webcam",
            "Mobile frame streaming via WebSocket",
            "Image file upload for batch detection",
            "Confidence-based filtering",
            "Bounding box coordinates",
            "Latency tracking"
        ],
        "specs": {
            "model_size": "~137 MB",
            "inference_latency": "50-200ms (GPU: 50-100ms, CPU: 100-200ms)",
            "supported_batch_size": 1,
            "fps_capability": "5-15 FPS (GPU), 1-3 FPS (CPU)"
        }
    }

@app.post("/camera/request-permission")
async def request_camera_permission(user_id: str = "default"):
    """Request permission to use camera streaming"""
    global camera_permission_granted, stream, detector

    camera_permission_granted = True

    # Initialize camera on first permission grant
    if stream is None or detector is None:
        WEBCAM_INDEX = int(os.getenv("WEBCAM_INDEX", 0))
        DETECTION_THRESHOLD = float(os.getenv("DETECTION_THRESHOLD", 0.7))
        stream = WebcamStream(src=WEBCAM_INDEX)
        detector = ObjectDetector(threshold=DETECTION_THRESHOLD)

    return {
        "status": "permission_granted",
        "message": f"Camera access enabled for user: {user_id}",
        "camera_available": stream is not None and stream.stream is not None,
        "detector_ready": detector is not None,
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S')
    }

@app.get("/camera/status")
async def get_camera_status():
    """Check camera permission and availability status"""
    return {
        "permission_granted": camera_permission_granted,
        "camera_initialized": stream is not None,
        "camera_available": stream is not None and stream.stream is not None if stream else False,
        "detector_ready": detector is not None,
        "mobile_source_active": mobile_source.active if mobile_source else False,
        "message": "Camera streaming not enabled. Call POST /camera/request-permission first." if not camera_permission_granted else "Camera ready for streaming"
    }

@app.get("/detect")
async def detect_objects(filter_classes: str = ""):
    """
    Capture frame and return detection results.
    Requires prior permission via POST /camera/request-permission

    Optional: filter_classes - comma-separated list of classes to detect (e.g. "person,car,dog")
    """
    global stream, detector, mobile_source, camera_permission_granted

    # Check permission
    if not camera_permission_granted:
        return {
            "error": "Camera access not permitted",
            "message": "Call POST /camera/request-permission first to enable camera streaming",
            "status_code": 403
        }

    if stream is None or detector is None:
        return {
            "error": "Detection model not initialized",
            "message": "Call POST /camera/request-permission to initialize",
            "status_code": 503
        }

    # Prioritize mobile source if active
    frame = None
    if mobile_source and mobile_source.active:
        frame = mobile_source.get_frame()

    if frame is None:
        frame = stream.get_frame()

    if frame is None:
        REQUEST_COUNT.labels(method="GET", endpoint="/detect", status="400").inc()
        return {"error": "Failed to capture frame"}

    # Detect objects
    start_time = time.time()
    source_type = "mobile" if (mobile_source and mobile_source.active) else "webcam"
    with INFERENCE_LATENCY.labels(source=source_type).time():
        prediction = detector.detect(frame)

    latency = time.time() - start_time
    REQUEST_COUNT.labels(method="GET", endpoint="/detect", status="200").inc()

    results = detector.get_labels(prediction)

    # Filter by classes if specified
    if filter_classes:
        allowed_classes = set(filter_classes.split(","))
        results = [(label, score, box) for label, score, box in results if label.strip() in allowed_classes]

    # Encode frame as base64
    _, buffer = cv2.imencode('.jpg', frame)
    frame_b64 = base64.b64encode(buffer).decode('utf-8')

    return {
        "model": "FasterRCNN ResNet50 FPN (COCO pretrained)",
        "detections": [{"label": label, "score": round(score, 3), "box": box} for label, score, box in results],
        "detection_count": len(results),
        "latency_ms": round(latency * 1000, 2),
        "frame_encoded": frame_b64,
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "source": source_type,
        "filter_applied": filter_classes if filter_classes else "none"
    }

@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.websocket("/mobile-stream")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_bytes()
            mobile_source.update_frame(data)
    except WebSocketDisconnect:
        print("Mobile client disconnected")
        pass

@app.get("/")
@app.get("/api/status")
async def api_status():
    """JSON endpoint listing available endpoints."""
    return {
        "status": "Vision Module Active",
        "model": "FasterRCNN ResNet50 FPN (COCO pretrained)",
        "endpoints": {
            "detection": ["/detect", "/model-info"],
            "camera": ["/camera/request-permission", "/camera/status", "/mobile-stream (WebSocket)"],
            "ops": ["/metrics", "/api/status"],
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
