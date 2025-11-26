# Use official Python image
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install system dependencies (required for OpenCV & YOLO)
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy code
COPY . /app

# Install dependencies
RUN pip install --no-cache-dir \
    ultralytics \
    fastapi \
    uvicorn[standard] \
    opencv-python \
    numpy

# Expose API port
EXPOSE 8000

# Run the server
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
