FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MODEL_DIR=/models \
    TORCH_HOME=/models/torch \
    HF_HOME=/models/hf \
    YOLO_CONFIG_DIR=/tmp/ultralytics

# libGL / glib are needed by OpenCV (pulled in by ultralytics)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 torchvision==0.20.1
COPY requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --uid 10001 --no-create-home app && mkdir /models && chown app /models
USER app

# Bake weights into the image (no downloads at cold start), smoke-testing each model.
COPY catalog.py tcn.py download_weights.py ./
RUN python download_weights.py

ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
COPY app.py auth.py ./

EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn app:create_app --factory --host 0.0.0.0 --port ${PORT:-8080}"]
