from faster_whisper import WhisperModel
from minio import Minio
import subprocess
import numpy as np
from datetime import timedelta

# MinIO Configuration
MINIO_ENDPOINT = "localhost:9000"
MINIO_ACCESS_KEY = "minioadmin"
MINIO_SECRET_KEY = "minioadmin123"
MINIO_BUCKET = "livekit-recordings"
MINIO_FILE_PATH = "fa_test.mp3"

# Audio settings for Whisper
SAMPLE_RATE = 16000

def stream_audio_from_minio():
    """Stream audio from MinIO through FFmpeg, return numpy array"""
    
    print(f"Connecting to MinIO at {MINIO_ENDPOINT}...")
    minio_client = Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=False
    )
    
    # Generate presigned URL for streaming
    presigned_url = minio_client.presigned_get_object(
        MINIO_BUCKET, 
        MINIO_FILE_PATH,
        expires=timedelta(hours=1)
    )
    print(f"Generated presigned URL for streaming...")
    
    # FFmpeg command: stream from URL -> convert to 16kHz mono PCM
    ffmpeg_cmd = [
        "ffmpeg",
        "-i", presigned_url,           # Input from MinIO URL
        "-f", "s16le",                 # Output format: signed 16-bit little-endian
        "-acodec", "pcm_s16le",        # Audio codec
        "-ar", str(SAMPLE_RATE),       # Sample rate 16kHz (Whisper requirement)
        "-ac", "1",                    # Mono channel
        "-loglevel", "error",          # Suppress ffmpeg output
        "-"                            # Output to stdout (pipe)
    ]
    
    print(f"Streaming {MINIO_FILE_PATH} through FFmpeg...")
    
    # Run FFmpeg and capture output
    process = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    # Read all audio data
    audio_data, stderr = process.communicate()
    
    if process.returncode != 0:
        raise RuntimeError(f"FFmpeg error: {stderr.decode()}")
    
    # Convert bytes to numpy array (float32 normalized to [-1, 1])
    audio_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
    
    duration = len(audio_np) / SAMPLE_RATE
    print(f"Streamed {duration:.2f}s of audio ({len(audio_data) / 1024:.1f} KB)")
    
    return audio_np

def main():
    # Stream audio from MinIO
    audio = stream_audio_from_minio()
    
    # Load Whisper model
    print("Loading Whisper model...")
    model = WhisperModel(
        "large-v3",
        device="cuda",
        compute_type="float16"
    )

    # Transcribe with auto detect language
    print("Transcribing audio...")
    segments, info = model.transcribe(
        audio,  # Pass numpy array directly
        beam_size=5,
        vad_filter=True
    )

    print("\n" + "=" * 50)
    print(f"Detected language: {info.language}")
    print(f"Language probability: {info.language_probability:.2%}")
    print("=" * 50 + "\n")

    # Print results
    all_segments = list(segments)
    for seg in all_segments:
        print(f"[{seg.start:.2f}s -> {seg.end:.2f}s] {seg.text}")

if __name__ == "__main__":
    main()
