"""LiveKit local recording helper.

Công cụ này giúp bạn record room và lưu file MP4/OGG/MP3 ra local disk.

Yêu cầu:
- LiveKit server + Egress đang chạy local (docker-compose.yml)
- Room phải join trên LOCAL server (ws://localhost:7880), không phải Cloud
- Egress container phải có quyền SYS_ADMIN + shm_size + seccomp=unconfined

Usage:
  # Tạo room local
  python record_room.py create-room test-room

  # Tạo token để join
  python record_room.py token test-room my-user
  
  # Join room bằng token (https://meet.livekit.io + ws://localhost:7880)
  # Rồi start recording với nhiều chế độ:
  
  # 1. Room composite (ghép tất cả video thành 1 file)
  python record_room.py start test-room --mode room --wait 60
  
  # 2. Track (record từng audio track riêng biệt, không bị lẫn)
  python record_room.py start test-room --mode track --audio-only
  
  # 3. Participant (record tất cả track của 1 người)
  python record_room.py start test-room --mode participant --identity user123
  
  # 4. Web (capture từ URL)
  python record_room.py start test-room --mode web --url https://example.com
  
  # Stop để finalize file
  python record_room.py stop <EGRESS_ID>
  
  # Liệt kê participants trong room
  python record_room.py participants test-room
  
  # Kiểm tra file tại: E:/NCC/egress/recordings/
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

from livekit import api


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# Config - ưu tiên đọc từ biến môi trường
LIVEKIT_URL = _env("LIVEKIT_URL", "http://localhost:7880")
LIVEKIT_API_KEY = _env("LIVEKIT_API_KEY", "APIForNzhv8Shvk")
LIVEKIT_API_SECRET = _env("LIVEKIT_API_SECRET", "mOuEREmPmJqEM4RfupUGrGe6XdxJi3i0LPOdFPoQLkl")

# Local recordings dir inside the egress container (must match docker volume mount)
LOCAL_RECORDINGS_DIR = _env("LOCAL_RECORDINGS_DIR", "/recordings")

# S3 config (chỉ dùng nếu LIVEKIT_URL là Cloud)
S3_ACCESS_KEY = _env("S3_ACCESS_KEY", "")
S3_SECRET = _env("S3_SECRET", "")
S3_BUCKET = _env("S3_BUCKET", "")
S3_REGION = _env("S3_REGION", "")


def _is_cloud(url: str) -> bool:
    return "livekit.cloud" in url.lower()


def _require_auth() -> None:
    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise SystemExit(
            "Missing LIVEKIT_API_KEY / LIVEKIT_API_SECRET.\n"
            "Set them as env vars or edit the script."
        )


def _lk() -> api.LiveKitAPI:
    _require_auth()
    return api.LiveKitAPI(
        url=LIVEKIT_URL,
        api_key=LIVEKIT_API_KEY,
        api_secret=LIVEKIT_API_SECRET
    )


def _status_str(status: int) -> str:
    names = {
        0: "STARTING",
        1: "ACTIVE",
        2: "ENDING",
        3: "COMPLETE",
        4: "FAILED",
        5: "ABORTED",
        6: "LIMIT_REACHED",
    }
    return names.get(int(status), f"UNKNOWN({status})")


# ============================================================================
# Commands
# ============================================================================

async def create_room(room_name: str) -> None:
    """Tạo room trên server local."""
    lk = _lk()
    try:
        await lk.room.create_room(api.CreateRoomRequest(name=room_name))
        print(f"✓ Room created: {room_name}")
    except api.TwirpError as e:
        if e.code == "already_exists":
            print(f"✓ Room already exists: {room_name}")
        else:
            raise
    finally:
        await lk.aclose()


def create_token(room_name: str, identity: str) -> str:
    """Tạo token để join room."""
    _require_auth()
    token = api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    token.with_identity(identity)
    token.with_grants(api.VideoGrants(room_join=True, room=room_name))
    return token.to_jwt()


@dataclass
class StartOpts:
    mode: str = "room"  # room, track, participant, web
    layout: str = "grid"
    wait_sec: int = 0
    identity: str = ""  # for participant mode
    audio_only: bool = False  # for track mode
    url: str = ""  # for web mode


def _build_file_output(room_name: str, suffix: str = "", file_type=None) -> api.EncodedFileOutput:
    """Build file output config for local or cloud."""
    if file_type is None:
        file_type = api.EncodedFileType.MP4
    
    ext_map = {
        api.EncodedFileType.MP4: "mp4",
        api.EncodedFileType.OGG: "ogg",
        getattr(api.EncodedFileType, "MP3", 3): "mp3",
    }
    ext = ext_map.get(file_type, "mp4")
    
    if _is_cloud(LIVEKIT_URL):
        if not (S3_ACCESS_KEY and S3_SECRET and S3_BUCKET):
            raise SystemExit(
                "LiveKit Cloud requires S3 output.\n"
                "Set: S3_ACCESS_KEY, S3_SECRET, S3_BUCKET, S3_REGION"
            )
        return api.EncodedFileOutput(
            file_type=file_type,
            filepath=f"recordings/{room_name}{suffix}-{{time}}.{ext}",
            s3=api.S3Upload(
                access_key=S3_ACCESS_KEY,
                secret=S3_SECRET,
                bucket=S3_BUCKET,
                region=S3_REGION or "us-east-1",
            ),
        )
    else:
        local_dir = LOCAL_RECORDINGS_DIR.rstrip("/") or "/recordings"
        return api.EncodedFileOutput(
            file_type=file_type,
            filepath=f"{local_dir}/{room_name}{suffix}-{{time}}.{ext}",
        )


async def _start_room_composite(room_name: str, opts: StartOpts) -> Optional[str]:
    """Start room composite egress (ghép tất cả video thành 1 file)."""
    lk = _lk()
    file_out = _build_file_output(room_name)
    req = api.RoomCompositeEgressRequest(
        room_name=room_name,
        layout=opts.layout,
        file_outputs=[file_out],
    )

    try:
        result = await lk.egress.start_room_composite_egress(req)
        egress_id = result.egress_id
        print("✓ Room composite recording started")
        print(f"  egress_id: {egress_id}")
        print(f"  room:      {result.room_name}")
        print(f"  status:    {_status_str(result.status)}")
    except api.TwirpError as e:
        if (
            e.code == "not_found"
            and "room" in (e.message or "").lower()
            and not _is_cloud(LIVEKIT_URL)
        ):
            print("Room not found. Creating...")
            await create_room(room_name)
            result = await lk.egress.start_room_composite_egress(req)
            egress_id = result.egress_id
            print("✓ Room composite recording started")
            print(f"  egress_id: {egress_id}")
            print(f"  room:      {result.room_name}")
            print(f"  status:    {_status_str(result.status)}")
        else:
            print(f"✗ Twirp error: {e.message} (code: {e.code})")
            await lk.aclose()
            return None
    finally:
        await lk.aclose()

    if not _is_cloud(LIVEKIT_URL):
        print("\n⚠ File saves to: E:/NCC/egress/recordings/")

    if opts.wait_sec > 0:
        await _wait_active(egress_id, opts.wait_sec)
    return egress_id


async def _start_track_egress(room_name: str, opts: StartOpts) -> Optional[list[str]]:
    """Start individual track egress (record từng track riêng, không bị lẫn)."""
    lk = _lk()
    
    # List participants and their tracks
    try:
        res = await lk.room.list_participants(api.ListParticipantsRequest(room=room_name))
    except api.TwirpError as e:
        if e.code == "not_found" and not _is_cloud(LIVEKIT_URL):
            print("Room not found. Creating...")
            await create_room(room_name)
            print("⚠ Room created but no participants yet. Join the room first.")
            await lk.aclose()
            return None
        else:
            print(f"✗ Error listing participants: {e.message}")
            await lk.aclose()
            return None
    
    if not res.participants:
        print("⚠ No participants in room yet. Join the room first.")
        await lk.aclose()
        return None
    
    print(f"Found {len(res.participants)} participant(s) in room:")
    egress_ids = []
    
    for p in res.participants:
        identity = p.identity
        print(f"\n  Participant: {identity}")
        
        # Filter tracks
        tracks_to_record = []
        for track in p.tracks:
            track_type = int(track.type)
            track_sid = track.sid
            track_name = track.name or "unnamed"
            
            # 0=AUDIO, 1=VIDEO
            if opts.audio_only and track_type != 0:
                continue
            
            type_str = "audio" if track_type == 0 else "video"
            print(f"    - {type_str} track: {track_sid} ({track_name})")
            tracks_to_record.append((track_sid, type_str, track_name))
        
        # Start egress for each track
        for track_sid, type_str, track_name in tracks_to_record:
            # TrackEgressRequest requires DirectFileOutput (raw media, not encoded)
            # Audio → .ogg, Video → .webm (VP8/VP9) or .h264
            ext = "ogg" if type_str == "audio" else "webm"
            suffix = f"-{identity}-{type_str}-{track_sid[:8]}"
            local_dir = LOCAL_RECORDINGS_DIR.rstrip("/") or "/recordings"
            filepath = f"{local_dir}/{room_name}{suffix}-{{time}}.{ext}"
            
            file_out = api.DirectFileOutput(filepath=filepath)
            
            req = api.TrackEgressRequest(
                room_name=room_name,
                track_id=track_sid,
                file=file_out,
            )
            
            try:
                result = await lk.egress.start_track_egress(req)
                egress_ids.append(result.egress_id)
                print(f"      ✓ Started egress {result.egress_id} for {type_str} track")
            except api.TwirpError as e:
                print(f"      ✗ Failed to start egress: {e.message}")
    
    await lk.aclose()
    
    if egress_ids:
        print(f"\n✓ Started {len(egress_ids)} track egress job(s)")
        if not _is_cloud(LIVEKIT_URL):
            print("⚠ Files save to: E:/NCC/egress/recordings/")
    
    return egress_ids if egress_ids else None


async def _start_participant_egress(room_name: str, opts: StartOpts) -> Optional[str]:
    """Start participant egress (record tất cả track của 1 participant)."""
    if not opts.identity:
        print("✗ --identity required for participant mode")
        return None
    
    lk = _lk()
    file_out = _build_file_output(room_name, f"-{opts.identity}")
    
    req = api.ParticipantEgressRequest(
        room_name=room_name,
        identity=opts.identity,
        file_outputs=[file_out],
    )
    
    try:
        result = await lk.egress.start_participant_egress(req)
        egress_id = result.egress_id
        print(f"✓ Participant egress started for: {opts.identity}")
        print(f"  egress_id: {egress_id}")
        print(f"  status:    {_status_str(result.status)}")
    except api.TwirpError as e:
        print(f"✗ Twirp error: {e.message} (code: {e.code})")
        await lk.aclose()
        return None
    finally:
        await lk.aclose()
    
    if not _is_cloud(LIVEKIT_URL):
        print("\n⚠ File saves to: E:/NCC/egress/recordings/")
    
    if opts.wait_sec > 0:
        await _wait_active(egress_id, opts.wait_sec)
    return egress_id


async def _start_web_egress(room_name: str, opts: StartOpts) -> Optional[str]:
    """Start web egress (capture từ URL)."""
    if not opts.url:
        print("✗ --url required for web mode")
        return None
    
    lk = _lk()
    file_out = _build_file_output(room_name, "-web")
    
    req = api.WebEgressRequest(
        url=opts.url,
        file_outputs=[file_out],
    )
    
    try:
        result = await lk.egress.start_web_egress(req)
        egress_id = result.egress_id
        print(f"✓ Web egress started for URL: {opts.url}")
        print(f"  egress_id: {egress_id}")
        print(f"  status:    {_status_str(result.status)}")
    except api.TwirpError as e:
        print(f"✗ Twirp error: {e.message} (code: {e.code})")
        await lk.aclose()
        return None
    finally:
        await lk.aclose()
    
    if not _is_cloud(LIVEKIT_URL):
        print("\n⚠ File saves to: E:/NCC/egress/recordings/")
    
    if opts.wait_sec > 0:
        await _wait_active(egress_id, opts.wait_sec)
    return egress_id


async def start_recording(room_name: str, opts: StartOpts) -> Optional[str | list[str]]:
    """Bắt đầu recording với chế độ được chọn."""
    if opts.mode == "room":
        return await _start_room_composite(room_name, opts)
    elif opts.mode == "track":
        return await _start_track_egress(room_name, opts)
    elif opts.mode == "participant":
        return await _start_participant_egress(room_name, opts)
    elif opts.mode == "web":
        return await _start_web_egress(room_name, opts)
    else:
        print(f"✗ Unknown mode: {opts.mode}")
        print("  Valid modes: room, track, participant, web")
        return None


async def _wait_active(egress_id: str, timeout: int) -> None:
    """Poll egress status cho đến khi ACTIVE hoặc hết timeout."""
    lk = _lk()
    try:
        deadline = time.time() + timeout
        print(f"\nWaiting up to {timeout}s for egress to become ACTIVE...")
        while time.time() < deadline:
            res = await lk.egress.list_egress(api.ListEgressRequest(egress_id=egress_id))
            if not res.items:
                break
            info = res.items[0]
            status = int(info.status)
            status_str = _status_str(status)
            print(f"  status: {status_str}", end="")
            if info.error:
                print(f" (error: {info.error})", end="")
            print()

            # 1=ACTIVE, 3=COMPLETE, 4=FAILED, 5=ABORTED
            if status in {1, 3, 4, 5, 6}:
                break
            await asyncio.sleep(2)
    finally:
        await lk.aclose()


async def stop_recording(egress_id: str) -> None:
    """Dừng recording để finalize file."""
    lk = _lk()
    try:
        result = await lk.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id))
        print("✓ Stop requested")
        print(f"  egress_id: {result.egress_id}")
        print(f"  status:    {_status_str(result.status)}")
    finally:
        await lk.aclose()


async def list_egress(room_name: str = "", egress_id: str = "") -> None:
    """Liệt kê egress jobs."""
    lk = _lk()
    try:
        req = api.ListEgressRequest()
        if room_name:
            req.room_name = room_name
        if egress_id:
            req.egress_id = egress_id

        res = await lk.egress.list_egress(req)
        if not res.items:
            print("No egress found")
            return

        print(f"Found {len(res.items)} egress(es):")
        for item in res.items:
            print(f"  - id: {item.egress_id}")
            print(f"    room: {item.room_name}")
            print(f"    status: {_status_str(item.status)}")
            if item.error:
                print(f"    error: {item.error}")
            print()
    finally:
        await lk.aclose()


async def list_rooms() -> None:
    """Liệt kê rooms đang tồn tại trên server."""
    lk = _lk()
    try:
        res = await lk.room.list_rooms(api.ListRoomsRequest())
        if not res.rooms:
            print("No rooms found")
            return

        print(f"Found {len(res.rooms)} room(s):")
        for r in res.rooms:
            # Some fields may be empty depending on server version
            sid = getattr(r, "sid", "")
            name = getattr(r, "name", "")
            num = getattr(r, "num_participants", None)
            if num is None:
                print(f"  - name: {name}    sid: {sid}")
            else:
                print(f"  - name: {name}    sid: {sid}    participants: {num}")
    finally:
        await lk.aclose()


async def list_participants(room_name: str) -> None:
    """Liệt kê participants và tracks trong room."""
    lk = _lk()
    try:
        res = await lk.room.list_participants(api.ListParticipantsRequest(room=room_name))
        if not res.participants:
            print(f"No participants in room: {room_name}")
            return

        print(f"Found {len(res.participants)} participant(s) in room '{room_name}':")
        for p in res.participants:
            print(f"\n  Identity: {p.identity}")
            print(f"  SID:      {p.sid}")
            print(f"  State:    {p.state}")
            if p.tracks:
                print(f"  Tracks ({len(p.tracks)}):")
                for t in p.tracks:
                    track_type = "audio" if int(t.type) == 0 else "video"
                    muted_str = "(muted)" if t.muted else ""
                    print(f"    - {track_type}: {t.sid} - {t.name or 'unnamed'} {muted_str}")
            else:
                print("  Tracks: none")
    except api.TwirpError as e:
        print(f"✗ Error: {e.message} (code: {e.code})")
    finally:
        await lk.aclose()


async def resolve_room(room_sid: str) -> None:
    """Tìm room name từ room SID (RM_...)."""
    lk = _lk()
    try:
        res = await lk.room.list_rooms(api.ListRoomsRequest())
        for r in res.rooms:
            if getattr(r, "sid", "") == room_sid:
                print(getattr(r, "name", ""))
                return
        raise SystemExit(f"Room SID not found: {room_sid}")
    finally:
        await lk.aclose()


# ============================================================================
# CLI
# ============================================================================

def _usage() -> None:
    print("Commands:")
    print("  create-room <room>")
    print("  token <room> <identity>")
    print("  start <room> [options]")
    print("    Options:")
    print("      --mode room|track|participant|web  (default: room)")
    print("      --layout grid|speaker|single-speaker  (for room mode)")
    print("      --wait <seconds>")
    print("      --audio-only  (for track mode)")
    print("      --identity <name>  (for participant mode)")
    print("      --url <url>  (for web mode)")
    print("  stop <egress_id>")
    print("  list [--room <room>] [--id <egress_id>]")
    print("  rooms")
    print("  participants <room>")
    print("  resolve <room_sid>")
    print()
    print("Examples:")
    print("  # Room composite (1 file cho toàn phòng)")
    print("  python record_room.py start test-room --mode room")
    print()
    print("  # Track mode (mỗi audio track 1 file riêng)")
    print("  python record_room.py start test-room --mode track --audio-only")
    print()
    print("  # Participant (all tracks của 1 người)")
    print("  python record_room.py start test-room --mode participant --identity user123")
    print()
    print("Env vars:")
    print("  LIVEKIT_URL (default: http://localhost:7880)")
    print("  LIVEKIT_API_KEY / LIVEKIT_API_SECRET")


def _parse_start(args: list[str]) -> tuple[str, StartOpts]:
    if not args:
        raise SystemExit("Missing room name")
    room = args[0]
    opts = StartOpts()

    i = 1
    while i < len(args):
        if args[i] == "--mode" and i + 1 < len(args):
            opts.mode = args[i + 1]
            i += 2
        elif args[i] == "--layout" and i + 1 < len(args):
            opts.layout = args[i + 1]
            i += 2
        elif args[i] == "--wait" and i + 1 < len(args):
            opts.wait_sec = int(args[i + 1])
            i += 2
        elif args[i] == "--identity" and i + 1 < len(args):
            opts.identity = args[i + 1]
            i += 2
        elif args[i] == "--url" and i + 1 < len(args):
            opts.url = args[i + 1]
            i += 2
        elif args[i] == "--audio-only":
            opts.audio_only = True
            i += 1
        else:
            raise SystemExit(f"Unknown arg: {args[i]}")
    return room, opts


def _parse_list(args: list[str]) -> tuple[str, str]:
    room = ""
    egress_id = ""
    i = 0
    while i < len(args):
        if args[i] == "--room" and i + 1 < len(args):
            room = args[i + 1]
            i += 2
        elif args[i] == "--id" and i + 1 < len(args):
            egress_id = args[i + 1]
            i += 2
        else:
            raise SystemExit(f"Unknown arg: {args[i]}")
    return room, egress_id


def main() -> None:
    if len(sys.argv) < 2:
        _usage()
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "create-room":
        if len(args) != 1:
            raise SystemExit("Usage: create-room <room>")
        asyncio.run(create_room(args[0]))

    elif cmd == "token":
        if len(args) != 2:
            raise SystemExit("Usage: token <room> <identity>")
        print(create_token(args[0], args[1]))

    elif cmd == "start":
        room, opts = _parse_start(args)
        asyncio.run(start_recording(room, opts))

    elif cmd == "stop":
        if len(args) != 1:
            raise SystemExit("Usage: stop <egress_id>")
        asyncio.run(stop_recording(args[0]))

    elif cmd == "list":
        room, eid = _parse_list(args)
        asyncio.run(list_egress(room_name=room, egress_id=eid))

    elif cmd == "rooms":
        if args:
            raise SystemExit("Usage: rooms")
        asyncio.run(list_rooms())

    elif cmd == "participants":
        if len(args) != 1:
            raise SystemExit("Usage: participants <room>")
        asyncio.run(list_participants(args[0]))

    elif cmd == "resolve":
        if len(args) != 1:
            raise SystemExit("Usage: resolve <room_sid>")
        asyncio.run(resolve_room(args[0]))

    else:
        print(f"Unknown command: {cmd}")
        _usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
