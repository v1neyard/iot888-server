FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

COPY . .

# Download YOLO model at runtime, NOT build time!
ENV YOLO_DOWNLOAD=1

CMD ["bash", "-c", " \
    if [ \"$YOLO_DOWNLOAD\" = \"1\" ]; then \
        python3 -c \"from ultralytics import YOLO; YOLO('yolov8n.pt')\"; \
    fi && \
    uvicorn server:app --host 0.0.0.0 --port 8000"]
