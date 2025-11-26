FROM python:3.10-slim

WORKDIR /app

# Install only needed system libs (for OpenCV & YOLO)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy only necessary files first (to leverage Docker layer caching)
COPY requirements.txt ./

# Install dependencies
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

# Copy all source code
COPY . /app

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
