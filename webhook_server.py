"""
Simple Webhook Server for LiveKit track_published events.
Khi có track mới được publish, server sẽ tự động start track egress để record.
"""

import asyncio
import json
from datetime import datetime
from fastapi import FastAPI, Request
from livekit import api

# Config - match với livekit.yaml và egress.yaml
LIVEKIT_URL = "http://livekit-allinone:7880"  # Docker internal hostname
LIVEKIT_API_KEY = "APIForNzhv8Shvk"
LIVEKIT_API_SECRET = "mOuEREmPmJqEM4RfupUGrGe6XdxJi3i0LPOdFPoQLkl"

# MinIO/S3 config
MINIO_ENDPOINT = "http://minio:9000"
MINIO_ACCESS_KEY = "minioadmin"
MINIO_SECRET = "minioadmin123"
MINIO_BUCKET = "livekit-recordings"
MINIO_REGION = "us-east-1"

app = FastAPI(title="LiveKit Webhook Handler")

# Track các egress đã start để tránh duplicate
active_egresses = {}


def log(msg: str):
    """Simple logging với timestamp."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_lk() -> api.LiveKitAPI:
    """Tạo LiveKit API client."""
    return api.LiveKitAPI(
        url=LIVEKIT_URL,
        api_key=LIVEKIT_API_KEY,
        api_secret=LIVEKIT_API_SECRET
    )


async def start_track_recording(room_name: str, track_sid: str, track_type: str, identity: str):
    """Start egress để record 1 track và upload lên MinIO."""
    # Check nếu đã có egress cho track này
    if track_sid in active_egresses:
        log(f"  ⏭ Track {track_sid} already being recorded, skipping")
        return
    
    lk = get_lk()
    try:
        # Tạo filename
        ext = "ogg" if track_type == "AUDIO" else "webm"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{room_name}-{identity}-{track_type.lower()}-{timestamp}.{ext}"
        
        # Cấu hình S3Upload cho MinIO
        s3_upload = api.S3Upload(
            access_key=MINIO_ACCESS_KEY,
            secret=MINIO_SECRET,
            bucket=MINIO_BUCKET,
            region=MINIO_REGION,
            endpoint=MINIO_ENDPOINT,
            force_path_style=True,  # Bắt buộc cho MinIO
        )
        
        # File output với S3
        file_out = api.DirectFileOutput(
            filepath=filename,
            output=s3_upload,
        )
        
        req = api.TrackEgressRequest(
            room_name=room_name,
            track_id=track_sid,
            file=file_out,
        )
        
        result = await lk.egress.start_track_egress(req)
        active_egresses[track_sid] = result.egress_id
        
        log(f"  ✓ Started egress {result.egress_id}")
        log(f"    Upload to MinIO: s3://{MINIO_BUCKET}/{filename}")
        
    except Exception as e:
        log(f"  ✗ Failed to start egress: {e}")
    finally:
        await lk.aclose()


@app.get("/")
async def root():
    """Health check."""
    return {"status": "ok", "service": "livekit-webhook-handler"}


@app.post("/webhook")
async def handle_webhook(request: Request):
    """
    Nhận webhook từ LiveKit.
    Không validate JWT (đơn giản hóa cho testing).
    """
    try:
        body = await request.body()
        event = json.loads(body)
        print("event:\n", json.dumps(event, indent=2, ensure_ascii=False))
        event_type = event.get("event", "unknown")
        log(f"📥 Received webhook: {event_type}")
        
        # Chỉ xử lý track_published
        if event_type == "track_published":
            room = event.get("room", {})
            participant = event.get("participant", {})
            track = event.get("track", {})
            
            room_name = room.get("name", "unknown")
            identity = participant.get("identity", "unknown")
            track_sid = track.get("sid", "")
            mime_type = track.get("mimeType", "")  # e.g. "audio/red", "video/VP8"
            track_source = track.get("source", "UNKNOWN")  # MICROPHONE, CAMERA, SCREEN_SHARE
            
            # Xác định loại track từ mimeType
            is_audio = mime_type.startswith("audio")
            track_type = "AUDIO" if is_audio else "VIDEO"
            
            log(f"  Room: {room_name}")
            log(f"  Participant: {identity}")
            log(f"  Track: {track_sid} (mime: {mime_type}, source: {track_source})")
            
            # Chỉ record AUDIO tracks
            if not is_audio:
                log(f"  ⏭ Skipping non-audio track")
                return {"received": True, "action": "skipped_video"}
            
            # Start recording cho track này
            asyncio.create_task(
                start_track_recording(room_name, track_sid, track_type, identity)
            )
            
        elif event_type == "track_unpublished":
            track = event.get("track", {})
            track_sid = track.get("sid", "")
            if track_sid in active_egresses:
                log(f"  Track {track_sid} unpublished, egress should auto-stop")
                del active_egresses[track_sid]
                
        else:
            # Log các event khác
            log(f"  (ignored)")
        
        return {"received": True}
        
    except Exception as e:
        log(f"✗ Error processing webhook: {e}")
        return {"error": str(e)}


if __name__ == "__main__":
    import uvicorn
    log("🚀 Starting webhook server on port 8000...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
