###########################################
# 1️⃣ Build Stage (install dependencies)
###########################################
FROM python:3.10-slim AS builder

WORKDIR /app

# Install system dependencies for YOLO + OpenCV
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    gcc \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies early for caching
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt


###########################################
# 2️⃣ Runtime Stage (lightweight)
###########################################
FROM python:3.10-slim

WORKDIR /app

# Install only required runtime libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed dependencies from builder stage
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Copy project files
COPY . .

# Optional: Firebase service account (or use Docker secret)
COPY firebase_service_account.json /app/

EXPOSE 8000

# Healthcheck for Docker/Kubernetes
HEALTHCHECK --interval=30s --timeout=3s \
  CMD curl -f http://localhost:8000/docs || exit 1

# Run API (change 'combined_server' if filename is different)
CMD ["uvicorn", "combined_server:app", "--host", "0.0.0.0", "--port", "8000"]
