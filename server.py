import cv2
import base64
import numpy as np
from ultralytics import YOLO
from fastapi import FastAPI, WebSocket
import uvicorn

app = FastAPI()
model = YOLO("yolov8n.pt", verbose=False) # or yolov11n, yolov8s, etc.

@app.websocket("/ws")
async def inference_socket(ws: WebSocket):
    await ws.accept()
    print("Client connected")

    while True:
        try:
            data = await ws.receive_text()

            # Decode Base64 → OpenCV image
            img_bytes = base64.b64decode(data)
            npimg = np.frombuffer(img_bytes, np.uint8)
            frame = cv2.imdecode(npimg, cv2.IMREAD_COLOR)

            # Run YOLO
            results = model(frame, verbose=False)[0]
            detections = []

            for box in results.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                cls = int(box.cls[0])
                conf = float(box.conf[0])
                detections.append([x1, y1, x2, y2, model.names[cls], conf])

            # Send result back as text
            await ws.send_json({"detections": detections})

        except Exception as e:
            print("Client disconnected:", e)
            break

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000)