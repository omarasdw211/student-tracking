import asyncio
import glob
import json
import os
import re
import shutil
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).parent
UPLOADS_DIR = BASE_DIR / "uploads"
DOWNLOADS_DIR = BASE_DIR / "downloads"
SEPARATED_DIR = BASE_DIR / "separated"

for d in [UPLOADS_DIR, DOWNLOADS_DIR, SEPARATED_DIR]:
    d.mkdir(exist_ok=True)

# Multiple Piped instances as fallback
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.tokhmi.xyz",
    "https://api.piped.yt",
    "https://piped-api.garudalinux.org",
]

QUALITY_OPTIONS = [
    {"format_id": "max",  "label": "أعلى جودة"},
    {"format_id": "1080", "label": "1080p"},
    {"format_id": "720",  "label": "720p"},
    {"format_id": "480",  "label": "480p"},
    {"format_id": "360",  "label": "360p"},
]

_FMT_MAP = {
    "max":  "bestvideo+bestaudio/best",
    "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720":  "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480":  "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "360":  "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
}


class VideoURL(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: str


def _is_youtube(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url))


def _extract_yt_id(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def _cleanup(*paths: str):
    for p in paths:
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


# ─── Piped API helpers ─────────────────────────────────────────────────────────

async def _piped_get(video_id: str) -> dict:
    """Try each Piped instance until one works."""
    for base in PIPED_INSTANCES:
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                r = await client.get(f"{base}/streams/{video_id}")
            if r.status_code == 200:
                return r.json()
        except Exception:
            continue
    return {}


def _piped_pick_url(data: dict, quality: str) -> tuple[str | None, bool]:
    """
    Returns (url, has_audio).
    Tries to find a combined (video+audio) stream first.
    Falls back to video-only if nothing else found.
    """
    target_h = {"max": 9999, "1080": 1080, "720": 720, "480": 480, "360": 360}.get(quality, 9999)

    video_streams = data.get("videoStreams", [])

    def parse_height(s):
        try:
            return int(s.get("quality", "0p").replace("p", "").split(" ")[0])
        except Exception:
            return 0

    # Filter to mp4 only, within target height
    mp4 = [s for s in video_streams if "mp4" in s.get("mimeType", "").lower()
           and parse_height(s) <= target_h]

    # Prefer non-video-only (has audio embedded)
    combined = [s for s in mp4 if not s.get("videoOnly", True)]
    video_only = [s for s in mp4 if s.get("videoOnly", True)]

    for pool, has_audio in [(combined, True), (video_only, False)]:
        if pool:
            best = max(pool, key=parse_height)
            return best.get("url"), has_audio

    return None, False


async def _piped_audio_url(data: dict) -> str | None:
    """Get best audio stream URL from Piped data."""
    audio_streams = data.get("audioStreams", [])
    if not audio_streams:
        return None
    # Pick highest bitrate
    best = max(audio_streams, key=lambda s: s.get("bitrate", 0))
    return best.get("url")


# ─── Video Info ────────────────────────────────────────────────────────────────

@app.post("/api/video-info")
async def video_info(body: VideoURL):
    url = body.url.strip()

    if _is_youtube(url):
        # Try oEmbed for title/thumbnail
        title, thumb = "فيديو YouTube", ""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    "https://www.youtube.com/oembed",
                    params={"url": url, "format": "json"},
                )
            if r.status_code == 200:
                d = r.json()
                title = d.get("title", title)
                thumb = d.get("thumbnail_url", "")
        except Exception:
            pass
        return {"title": title, "thumbnail": thumb, "duration": 0, "formats": QUALITY_OPTIONS}

    # Non-YouTube: use yt-dlp
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "--dump-json", "--no-playlist",
            "--js-runtimes", "node",
            "--socket-timeout", "15", "--retries", "2",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode == 0:
            info = json.loads(stdout.decode())
            return {
                "title": info.get("title", "فيديو"),
                "thumbnail": info.get("thumbnail", ""),
                "duration": info.get("duration", 0),
                "formats": QUALITY_OPTIONS,
            }
    except Exception:
        pass

    return {"title": "فيديو", "thumbnail": "", "duration": 0, "formats": QUALITY_OPTIONS}


# ─── Download ──────────────────────────────────────────────────────────────────

@app.post("/api/download")
async def download_video(body: DownloadRequest):
    url = body.url.strip()
    quality = body.format_id

    # ── YouTube: use Piped API (no IP blocking, no auth needed) ──
    if _is_youtube(url):
        video_id = _extract_yt_id(url)
        if not video_id:
            raise HTTPException(status_code=400, detail="رابط YouTube غير صحيح")

        data = await _piped_get(video_id)
        if not data:
            raise HTTPException(status_code=502, detail="تعذّر الوصول لخوادم Piped، حاول مرة أخرى")

        video_url, has_audio = _piped_pick_url(data, quality)
        if not video_url:
            raise HTTPException(status_code=404, detail="لم تُوجد جودة مناسبة لهذا الفيديو")

        # If combined stream (has audio): redirect browser directly to it
        if has_audio:
            return JSONResponse({"download_url": video_url, "filename": "video.mp4", "direct": True})

        # Video-only: merge with audio on server using FFmpeg
        audio_url = await _piped_audio_url(data)
        if not audio_url:
            return JSONResponse({"download_url": video_url, "filename": "video_no_audio.mp4", "direct": True})

        job_id = str(uuid.uuid4())
        out_path = str(DOWNLOADS_DIR / f"{job_id}.mp4")

        ffmpeg_proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-i", video_url,
            "-i", audio_url,
            "-c:v", "copy", "-c:a", "aac",
            "-map", "0:v:0", "-map", "1:a:0",
            out_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, ff_err = await asyncio.wait_for(ffmpeg_proc.communicate(), timeout=300)
        if ffmpeg_proc.returncode != 0:
            # Fallback: return video-only URL
            return JSONResponse({"download_url": video_url, "filename": "video.mp4", "direct": True})

        return FileResponse(
            out_path,
            media_type="video/mp4",
            filename="video.mp4",
            background=BackgroundTask(_cleanup, out_path),
        )

    # ── Non-YouTube: use yt-dlp ──
    job_id = str(uuid.uuid4())
    output_template = str(DOWNLOADS_DIR / f"{job_id}.%(ext)s")
    fmt = _FMT_MAP.get(quality, _FMT_MAP["max"])

    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "-f", fmt,
            "--merge-output-format", "mp4",
            "-o", output_template,
            "--no-playlist",
            "--js-runtimes", "node",
            "--socket-timeout", "30",
            "--retries", "3",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=408, detail="انتهى وقت التنزيل، جرب جودة أقل")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        raise HTTPException(status_code=400, detail=f"فشل التنزيل: {err[:300]}")

    matches = glob.glob(str(DOWNLOADS_DIR / f"{job_id}.*"))
    if not matches:
        raise HTTPException(status_code=500, detail="لم يُعثر على الملف بعد التنزيل")

    file_path = matches[0]
    return FileResponse(
        file_path,
        media_type="video/mp4",
        filename="video.mp4",
        background=BackgroundTask(_cleanup, file_path),
    )


# ─── Audio Separation ──────────────────────────────────────────────────────────

@app.post("/api/separate-audio")
async def separate_audio(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())
    upload_path = UPLOADS_DIR / f"{job_id}_{file.filename}"
    sep_output_dir = SEPARATED_DIR / job_id
    result_path = DOWNLOADS_DIR / f"{job_id}_vocals.mp4"

    content = await file.read()
    upload_path.write_bytes(content)

    try:
        proc = await asyncio.create_subprocess_exec(
            "spleeter", "separate",
            "-i", str(upload_path),
            "-p", "spleeter:2stems",
            "-o", str(sep_output_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)

        if proc.returncode != 0:
            err = stderr.decode(errors="replace")
            raise HTTPException(status_code=500, detail=f"فشل فصل الصوت: {err[:300]}")

        found = list(sep_output_dir.rglob("vocals.wav"))
        if not found:
            raise HTTPException(status_code=500, detail="لم يُنتج Spleeter ملف الصوت")
        vocals_path = found[0]

        ffmpeg_proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-i", str(upload_path),
            "-i", str(vocals_path),
            "-c:v", "copy", "-c:a", "aac",
            "-map", "0:v:0", "-map", "1:a:0",
            str(result_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, ff_stderr = await asyncio.wait_for(ffmpeg_proc.communicate(), timeout=120)

        if ffmpeg_proc.returncode != 0:
            err = ff_stderr.decode(errors="replace")
            raise HTTPException(status_code=500, detail=f"فشل دمج الفيديو: {err[:300]}")

    except HTTPException:
        _cleanup(str(upload_path), str(sep_output_dir))
        raise
    except Exception as e:
        _cleanup(str(upload_path), str(sep_output_dir))
        raise HTTPException(status_code=500, detail=str(e))

    return FileResponse(
        str(result_path),
        media_type="video/mp4",
        filename="video_vocals_only.mp4",
        background=BackgroundTask(_cleanup, str(upload_path), str(sep_output_dir), str(result_path)),
    )


# ─── Static Files ─────────────────────────────────────────────────────────────

_static_dir = BASE_DIR.parent
if not (_static_dir / "video-tools.html").exists():
    _static_dir = Path("/app")
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
