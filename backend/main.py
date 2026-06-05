import asyncio
import glob
import json
import os
import re
import shutil
import uuid
from pathlib import Path

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

QUALITY_OPTIONS = [
    {"format_id": "max",  "label": "أعلى جودة"},
    {"format_id": "1080", "label": "1080p"},
    {"format_id": "720",  "label": "720p"},
    {"format_id": "480",  "label": "480p"},
    {"format_id": "360",  "label": "360p"},
]


class VideoURL(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: str


def _is_youtube(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url))


def _cleanup(*paths: str):
    for p in paths:
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


# ─── Video Info ────────────────────────────────────────────────────────────────

@app.post("/api/video-info")
async def video_info(body: VideoURL):
    url = body.url.strip()

    # YouTube → use oEmbed (no auth required, works from any IP)
    if _is_youtube(url):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    "https://www.youtube.com/oembed",
                    params={"url": url, "format": "json"},
                )
            if r.status_code == 200:
                data = r.json()
                return {
                    "title": data.get("title", "فيديو YouTube"),
                    "thumbnail": data.get("thumbnail_url", ""),
                    "duration": 0,
                    "formats": QUALITY_OPTIONS,
                }
        except Exception:
            pass
        # fallback if oEmbed fails
        return {"title": "فيديو YouTube", "thumbnail": "", "duration": 0, "formats": QUALITY_OPTIONS}

    # Other platforms → use yt-dlp (no IP blocking issues on non-YouTube)
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "--dump-json", "--no-playlist",
            "--socket-timeout", "15",
            "--retries", "2",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return {"title": "فيديو", "thumbnail": "", "duration": 0, "formats": QUALITY_OPTIONS}
    except Exception:
        return {"title": "فيديو", "thumbnail": "", "duration": 0, "formats": QUALITY_OPTIONS}

    if proc.returncode == 0:
        try:
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


# ─── Download via yt-dlp ──────────────────────────────────────────────────────

# Map UI quality label → yt-dlp format selector
_FMT_MAP = {
    "max":  "bestvideo+bestaudio/best",
    "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720":  "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480":  "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "360":  "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
}


@app.post("/api/download")
async def download_video(body: DownloadRequest):
    job_id = str(uuid.uuid4())
    output_template = str(DOWNLOADS_DIR / f"{job_id}.%(ext)s")
    fmt = _FMT_MAP.get(body.format_id, _FMT_MAP["max"])

    cmd = [
        "yt-dlp",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-o", output_template,
        "--no-playlist",
        "--socket-timeout", "30",
        "--retries", "3",
    ]

    # tv_embedded bypasses YouTube's IP-based blocking on datacenter servers
    if _is_youtube(body.url):
        cmd += ["--extractor-args", "youtube:player_client=tv_embedded"]

    cmd.append(body.url.strip())

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=408, detail="انتهى وقت التنزيل، جرب جودة أقل")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="yt-dlp غير مثبت")

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


# ─── Audio Separation via Spleeter ────────────────────────────────────────────

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

        stem_name = Path(upload_path.name).stem
        vocals_path = sep_output_dir / stem_name / "vocals.wav"

        if not vocals_path.exists():
            found = list(sep_output_dir.rglob("vocals.wav"))
            if not found:
                raise HTTPException(status_code=500, detail="لم يُنتج Spleeter ملف الصوت")
            vocals_path = found[0]

        ffmpeg_proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-i", str(upload_path),
            "-i", str(vocals_path),
            "-c:v", "copy",
            "-c:a", "aac",
            "-map", "0:v:0",
            "-map", "1:a:0",
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
