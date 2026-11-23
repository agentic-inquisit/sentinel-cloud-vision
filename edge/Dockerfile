# Use Python 3.9 slim image
FROM python:3.9-slim

# # Install system dependencies for OpenCV and Torch
# RUN apt-get update && apt-get install -y \
#     libgl1-mesa-glx \
#     libglib2.0-0 \
#     && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements from root (build context must be root)
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy edge directory contents
COPY edge/ .

# Expose port
EXPOSE 8001

# Run the application
CMD ["uvicorn", "vision_module:app", "--host", "0.0.0.0", "--port", "8001"]
