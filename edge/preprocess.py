import cv2
import numpy as np
from typing import Dict, Any

class ImagePreprocessPipeline:
    def __init__(self, target_size=(224, 224), preserve_aspect=True):
        """
        Args:
            target_size: Tuple (height, width) or int for square images
            preserve_aspect: If True, pad image; if False, stretch
        """
        if isinstance(target_size, int):
            self.target_size = (target_size, target_size)
        else:
            self.target_size = target_size
        self.preserve_aspect = preserve_aspect

    def assess_dimensions(self, image: np.ndarray) -> Dict[str, Any]:
        height, width, channels = image.shape
        aspect_ratio = width / height
        return {
            "width": width,
            "height": height,
            "channels": channels,
            "aspect_ratio": aspect_ratio,
            "pixel_count": width * height
        }

    def process(self, image: np.ndarray) -> np.ndarray:
        """Resize and normalize image."""
        h, w = image.shape[:2]
        target_h, target_w = self.target_size

        if self.preserve_aspect:
            # Aspect-ratio-preserving resize with padding (letterbox)
            scale = min(target_w / w, target_h / h)
            new_w, new_h = int(w * scale), int(h * scale)
            resized = cv2.resize(image, (new_w, new_h))

            # Create target-sized image with zero padding
            padded = np.zeros((target_h, target_w, 3), dtype=image.dtype)
            y_offset = (target_h - new_h) // 2
            x_offset = (target_w - new_w) // 2
            padded[y_offset:y_offset+new_h, x_offset:x_offset+new_w] = resized
            processed_img = padded
        else:
            # Direct resize (may distort)
            processed_img = cv2.resize(image, self.target_size)

        processed_img = processed_img.astype(np.float32) / 255.0
        return processed_img

    def run(self, image_bytes: bytes) -> Dict[str, Any]:
        try:
            nparr = np.frombuffer(image_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            
            if img is None:
                return {"status": "error", "message": "Failed to decode image"}
 
            dimensions = self.assess_dimensions(img)
 
            processed_img = self.process(img)
 
            return {
                "status": "success",
                "original_dimensions": dimensions,
                "processed_shape": processed_img.shape,
                "message": "Image preprocessed successfully",
                "placeholder_processing": "Resized and normalized"
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
 
pipeline = ImagePreprocessPipeline(target_size=224, preserve_aspect=True)

def run_pipeline(image_bytes: bytes, target_size=224, preserve_aspect=True):
    """
    Run preprocessing pipeline on image bytes.

    Args:
        image_bytes: Raw image data
        target_size: Target size (int for square or tuple for (h, w))
        preserve_aspect: If True, pad to preserve aspect ratio
    """
    proc_pipeline = ImagePreprocessPipeline(target_size=target_size, preserve_aspect=preserve_aspect)
    return proc_pipeline.run(image_bytes)