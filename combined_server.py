import base64
import io
import threading
import time
from datetime import datetime

import cv2
import numpy as np
import paho.mqtt.client as mqtt
import firebase_admin
from firebase_admin import credentials, firestore, storage
from fastapi import FastAPI, WebSocket, HTTPException
from ultralytics import YOLO

# ----------------------- CONFIG -----------------------
# MQTT
MQTT_HOST = "172.20.10.4"
MQTT_PORT = 1883
MQTT_USER = "admin"
MQTT_PASS = "admin"
TOPIC_DHT = "iot/esp32/dht11"
TOPIC_SOUND = "iot/esp32/lm393"
# TOPIC_TRAFFIC_IN = "iot/raspi/traffic"  # messages coming from devices (we still listen if used)
TOPIC_TRAFFIC_OUT = "iot/backend/traffic"  # changed as requested

# Firebase (replace with your values)
SERVICE_ACCOUNT_PATH = "/app/firebase_service_account.json"  # <-- REPLACE
FIREBASE_BUCKET = "your-project-id.appspot.com"        # <-- REPLACE

# YOLO / inference configuration
YOLO_WEIGHTS = "yolov8n.pt"
TARGET_FPS = 2  # 2 images per second
MIN_INTERVAL = 1.0 / TARGET_FPS

# Sound threshold (tune to your payload units). If payload is numeric amplitude, set threshold here.
SOUND_THRESHOLD = 300  # example threshold; adapt to your payload

# ------------------------------------------------------

app = FastAPI()

# Global state
model = YOLO(YOLO_WEIGHTS)

# If you have CUDA and want GPU, uncomment:
# model = model.to('cuda')

mqtt_client = mqtt.Client()
last_frame = None
last_frame_lock = threading.Lock()

# Firestore client will be initialized after firebase_admin
db = None
bucket = None

# Keep single shared mqtt connected flag
mqtt_connected = threading.Event()

# ---------------- Firebase init ----------------
def init_firebase():
    global db, bucket
    try:
        cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
        firebase_admin.initialize_app(cred, {"storageBucket": FIREBASE_BUCKET})
    except Exception as e:
        # Fall back to default app if already initialized elsewhere
        try:
            firebase_admin.get_app()
        except Exception:
            raise RuntimeError("Failed to initialize Firebase. Check SERVICE_ACCOUNT_PATH and bucket name.")

    db = firestore.client()
    bucket = storage.bucket()
    print("Firebase initialized. Bucket:", FIREBASE_BUCKET)

# ---------------- MQTT callbacks ----------------

def on_connect(client, userdata, flags, rc):
    print("MQTT connected with result code", rc)
    # Subscribe to topics
    client.subscribe(TOPIC_DHT)
    client.subscribe(TOPIC_SOUND)
    # client.subscribe(TOPIC_TRAFFIC_IN)
    mqtt_connected.set()


def store_message_to_firestore(topic, payload):
    try:
        doc = {
            "topic": topic,
            "payload": payload,
            "ts": firestore.SERVER_TIMESTAMP,
            "ts_local": datetime.utcnow().isoformat() + "Z",
        }
        # Use a collection 'telemetry' and let Firestore generate doc id
        db.collection("telemetry").add(doc)
    except Exception as e:
        print("Failed to write to Firestore:", e)


def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode()
    except Exception:
        payload = str(msg.payload)

    print(f"MQTT Received => {msg.topic}: {payload}")
    # store to Firestore as time-series
    try:
        store_message_to_firestore(msg.topic, payload)
    except Exception as e:
        print("Firestore store error:", e)

    # Special handling for sound topic
    if msg.topic == TOPIC_SOUND:
        # try parse numeric value
        try:
            val = float(payload)
        except Exception:
            # payload may be JSON or other format. Try to extract digits
            try:
                import json

                js = json.loads(payload)
                val = float(js.get("value", 0))
            except Exception:
                val = None

        if val is not None:
            try:
                if val >= SOUND_THRESHOLD:
                    print("Loud sound detected (value=", val, ") — saving image to Firebase")
                    # grab last frame snapshot and upload
                    # save_last_frame_to_local(val)
            except Exception as e:
                print("Error handling loud sound:", e)


# ---------------- Utilities ----------------

# def save_last_frame_to_local(sound_value=None):
#     """Save latest frame to local storage instead of Firebase."""
#     global last_frame
#     with last_frame_lock:
#         frame = None if last_frame is None else last_frame.copy()
#     if frame is None:
#         print("No frame available to save")
#         return None
#     try:
#         ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
#         filename = f"captures_local/capture_{ts}.jpg"
#         # ensure directory
#         import os
#         os.makedirs(os.path.dirname(filename), exist_ok=True)
#         cv2.imwrite(filename, frame)
#         # Optionally store metadata locally
#         meta_path = f"captures_local/capture_{ts}.json"
#         with open(meta_path, 'w') as f:
#             import json
#             json.dump({"path": filename, "sound_value": sound_value, "timestamp": ts}, f)
#         print("Saved image locally:", filename)
#         return filename
#     except Exception as e:
#         print("Error saving locally:", e)
#         return None

# ---------------- MQTT thread ----------------

def start_mqtt_loop():
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
    mqtt_client.loop_forever()


# ---------------- FastAPI endpoints ----------------

@app.post("/traffic/{cmd}")
def publish_traffic(cmd: str):
    """Publish traffic command (1,2,3) to iot/backend/traffic and record to Firestore."""
    if cmd not in ["1", "2", "3"]:
        raise HTTPException(status_code=400, detail="cmd must be '1', '2', or '3'")

    # ensure mqtt is connected
    if not mqtt_connected.is_set():
        raise HTTPException(status_code=503, detail="MQTT not connected yet")

    mqtt_client.publish(TOPIC_TRAFFIC_OUT, cmd)
    store_message_to_firestore(TOPIC_TRAFFIC_OUT, cmd)
    return {"status": "published", "topic": TOPIC_TRAFFIC_OUT, "cmd": cmd}


@app.websocket("/ws")
async def inference_socket(ws: WebSocket):
    await ws.accept()
    print("Client connected via WebSocket")

    last_time = 0.0

    while True:
        try:
            data = await ws.receive_text()

            # Always update last_frame (so we have the freshest frame if sound event occurs),
            # but only run YOLO at TARGET_FPS.
            img_bytes = base64.b64decode(data)
            npimg = np.frombuffer(img_bytes, np.uint8)
            frame = cv2.imdecode(npimg, cv2.IMREAD_COLOR)

            if frame is None:
                await ws.send_json({"error": "invalid_image"})
                continue

            # update shared last_frame
            with last_frame_lock:
                last_frame = frame.copy()

            current_time = time.time()
            if current_time - last_time < MIN_INTERVAL:
                # Skip inference, but send a quick acknowledge so client knows server is alive.
                await ws.send_json({"detections": [], "skipped": True})
                continue

            last_time = current_time

            # Optionally resize to speed inference
            # frame_in = cv2.resize(frame, (640, 640))
            frame_in = frame

            # Run YOLO
            results = model(frame_in, verbose=False)[0]
            detections = []

            for box in results.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                cls = int(box.cls[0])
                conf = float(box.conf[0])
                detections.append([x1, y1, x2, y2, model.names[cls], conf])

            await ws.send_json({"detections": detections, "skipped": False})

        except Exception as e:
            print("WebSocket closed or error:", e)
            break


# ---------------- Startup ----------------

def start_background_services():
    # Initialize Firebase
    init_firebase()

    # Start MQTT loop in separate thread
    t = threading.Thread(target=start_mqtt_loop, daemon=True)
    t.start()
    print("Started MQTT thread")


# Call startup when module is imported by uvicorn
start_background_services()

# If you want to run directly with python combined_server.py
if __name__ == '__main__':
    import uvicorn

    uvicorn.run("combined_server:app", host="0.0.0.0", port=8000, reload=False)
