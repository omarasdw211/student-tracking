import asyncio
import glob
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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


class VideoURL(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: str


def _cleanup(*paths: str):
    for p in paths:
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def _pick_formats(raw_formats: list) -> list:
    seen_heights = set()
    result = []
    priority = ["mp4", "webm"]

    for fmt in reversed(raw_formats):
        height = fmt.get("height")
        ext = fmt.get("ext", "")
        vcodec = fmt.get("vcodec", "none")
        acodec = fmt.get("acodec", "none")

        if vcodec == "none" or height is None:
            continue
        if ext not in priority:
            continue
        if height in seen_heights:
            continue

        seen_heights.add(height)
        label = f"{height}p {ext.upper()}"
        # prefer format that has audio merged
        if acodec != "none":
            fmt_id = fmt["format_id"]
        else:
            fmt_id = f"{fmt['format_id']}+bestaudio[ext=m4a]/bestaudio"

        result.append({"format_id": fmt_id, "label": label, "ext": ext})

    result.sort(key=lambda x: int(x["label"].split("p")[0]), reverse=True)
    return result[:5]


@app.post("/api/video-info")
async def video_info(body: VideoURL):
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "--dump-json", "--no-playlist", body.url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail="انتهى وقت الاستجابة، تحقق من الرابط")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="yt-dlp غير مثبت")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        raise HTTPException(status_code=400, detail=f"تعذّر جلب معلومات الفيديو: {err[:300]}")

    try:
        info = json.loads(stdout.decode())
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="استجابة غير متوقعة من yt-dlp")

    formats = _pick_formats(info.get("formats", []))
    if not formats:
        formats = [{"format_id": "best", "label": "أفضل جودة متاحة", "ext": "mp4"}]

    return {
        "title": info.get("title", "بدون عنوان"),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration", 0),
        "formats": formats,
    }


@app.post("/api/download")
async def download_video(body: DownloadRequest):
    job_id = str(uuid.uuid4())
    output_template = str(DOWNLOADS_DIR / f"{job_id}.%(ext)s")

    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "-f", body.format_id,
            "--merge-output-format", "mp4",
            "-o", output_template,
            "--no-playlist",
            body.url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail="انتهى وقت التنزيل")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="yt-dlp غير مثبت")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        raise HTTPException(status_code=400, detail=f"فشل التنزيل: {err[:300]}")

    matches = glob.glob(str(DOWNLOADS_DIR / f"{job_id}.*"))
    if not matches:
        raise HTTPException(status_code=500, detail="لم يُعثر على الملف المحمَّل")

    file_path = matches[0]
    return FileResponse(
        file_path,
        media_type="video/mp4",
        filename="video.mp4",
        background=BackgroundTask(_cleanup, file_path),
    )


@app.post("/api/separate-audio")
async def separate_audio(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())
    upload_path = UPLOADS_DIR / f"{job_id}_{file.filename}"
    sep_output_dir = SEPARATED_DIR / job_id
    result_path = DOWNLOADS_DIR / f"{job_id}_vocals.mp4"

    # Save uploaded file
    content = await file.read()
    upload_path.write_bytes(content)

    try:
        # Run Spleeter to separate vocals from accompaniment
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

        # Find the vocals file produced by Spleeter
        stem_name = Path(upload_path.name).stem
        vocals_path = sep_output_dir / stem_name / "vocals.wav"

        if not vocals_path.exists():
            # Try without job_id prefix
            alt_name = "_".join(upload_path.stem.split("_")[1:]) if "_" in upload_path.stem else upload_path.stem
            vocals_path = sep_output_dir / alt_name / "vocals.wav"

        if not vocals_path.exists():
            # Fallback: find any vocals.wav in the output directory
            found = list(sep_output_dir.rglob("vocals.wav"))
            if not found:
                raise HTTPException(status_code=500, detail="لم يُنتج Spleeter ملف الصوت")
            vocals_path = found[0]

        # Merge vocals audio back with original video using FFmpeg
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


# Serve static files from the project root (one level above backend/)
# Works both locally and in Docker (where WORKDIR is /app/backend)
_static_dir = BASE_DIR.parent
if not (_static_dir / "video-tools.html").exists():
    _static_dir = Path("/app")  # Docker fallback
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
