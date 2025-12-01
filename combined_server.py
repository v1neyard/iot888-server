import asyncio
import base64
import threading
import time
from datetime import datetime
from typing import List

import cv2
import numpy as np
import firebase_admin
from firebase_admin import credentials, firestore, storage, db as rtdb
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from ultralytics import YOLO

# ----------------------- CONFIG -----------------------
# Firebase
SERVICE_ACCOUNT_PATH = "/app/firebase_service_account.json" 
FIREBASE_DB_URL = "https://iot888-24430-default-rtdb.asia-southeast1.firebasedatabase.app"

# YOLO
YOLO_WEIGHTS = "yolov8n.pt"
MIN_INTERVAL = 1.0  # Process 1 frame per second
MIN_PUBLISH_DELAY = 5 # Don't switch lights too fast

app = FastAPI()

# Global state
model = YOLO(YOLO_WEIGHTS) # Add .to('cuda') if GPU available
last_frame = None
last_frame_lock = threading.Lock()

# Firebase Init
cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
firebase_admin.initialize_app(cred, {
    "databaseURL": FIREBASE_DB_URL
})
db = firestore.client()

# ---------------- WebSocket Manager ----------------
# This allows the API to send messages to the connected Raspi
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def send_command(self, message: dict):
        # Broadcast command to all connected cameras (usually just one)
        for connection in self.active_connections:
            await connection.send_json(message)

manager = ConnectionManager()

# ---------------- Utilities ----------------
def log_traffic_decision(zone_id):
    """Log the decision to Firebase"""
    try:
        ts = datetime.utcnow().isoformat() + "Z"
        payload = str(zone_id)
        # Firestore
        db.collection("telemetry").add({
            "topic": "iot/backend/traffic",
            "payload": payload,
            "ts": firestore.SERVER_TIMESTAMP,
            "ts_local": ts,
        })
        # Realtime DB
        rtdb.reference("telemetry/iot_backend_traffic").push({
            "payload": payload,
            "timestamp": ts,
        })
    except Exception as e:
        print("Firebase Error:", e)

# ---------------- API Endpoints ----------------

@app.post("/traffic/{cmd}")
async def publish_traffic(cmd: str):
    """
    Manually force a traffic command via API.
    This sends the command to Raspi via WebSocket.
    """
    if cmd not in ["1", "2", "3"]:
        raise HTTPException(status_code=400, detail="cmd must be '1', '2', or '3'")

    # Send command to Raspi via WebSocket
    await manager.send_command({"type": "FORCE_COMMAND", "val": cmd})
    
    # Log to Firebase
    log_traffic_decision(cmd)
    
    return {"status": "sent_to_raspi", "cmd": cmd}

@app.websocket("/ws")
async def inference_socket(ws: WebSocket):
    await manager.connect(ws)
    print("Raspi connected via WebSocket")

    last_time = 0.0
    last_publish_time = 0

    try:
        while True:
            # 1. Receive Image
            data = await ws.receive_text()
            
            # Check if Raspi sent a sensor update (instead of image)
            # You can expand this protocol later if needed
            if data.startswith("{"):
                # Handle JSON sensor data if you send it here
                continue

            img_bytes = base64.b64decode(data)
            npimg = np.frombuffer(img_bytes, np.uint8)
            frame = cv2.imdecode(npimg, cv2.IMREAD_COLOR)

            if frame is None:
                continue

            # Update global frame for preview if needed
            with last_frame_lock:
                global last_frame
                last_frame = frame.copy()

            # Rate Limiting
            current_time = time.time()
            if current_time - last_time < MIN_INTERVAL:
                # Just acknowledge without heavy processing
                await ws.send_json({"status": "skipped"})
                continue

            last_time = current_time

            # 2. YOLO Logic
            H, W, _ = frame.shape
            zone_width = W // 3
            zones = {
                1: (0, zone_width),
                2: (zone_width, 2 * zone_width),
                3: (2 * zone_width, W)
            }
            zone_counts = {1: 0, 2: 0, 3: 0}

            results = model(frame, verbose=False)[0]
            detections = []

            for box in results.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                cls = int(box.cls[0])
                label = model.names[cls]

                if label in ["car", "truck", "bus", "motorbike"]:
                    center_x = int((x1 + x2) / 2)
                    for z, (zx1, zx2) in zones.items():
                        if zx1 <= center_x < zx2:
                            zone_counts[z] += 1
                            break
                    
                    detections.append([x1, y1, x2, y2, label])

            # 3. Decision Logic
            response_payload = {
                "detections": detections,
                "zone_counts": zone_counts,
                "command": None # Default no command
            }

            now = time.time()
            if now - last_publish_time >= MIN_PUBLISH_DELAY:
                # Determine busiest zone
                max_zone = max(zone_counts, key=zone_counts.get)
                
                # PREPARE COMMAND FOR RASPI
                response_payload["command"] = str(max_zone)
                
                print(f"🚦 Decision: GREEN to Zone {max_zone}")
                log_traffic_decision(max_zone)
                last_publish_time = now

            # 4. Send Results & Command back to Raspi
            await ws.send_json(response_payload)

    except WebSocketDisconnect:
        manager.disconnect(ws)
        print("Raspi disconnected")
    except Exception as e:
        print("Error:", e)
        manager.disconnect(ws)
        

frontend_clients = []
@app.websocket("/ws/stream")
async def video_stream(ws: WebSocket):
    await ws.accept()
    frontend_clients.append(ws)
    print("Frontend connected for video stream")

    try:
        while True:
            await asyncio.sleep(1)  # keep alive
    except:
        frontend_clients.remove(ws)
        print("Frontend disconnected")
        
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("combined_server:app", host="0.0.0.0", port=8000, reload=True)