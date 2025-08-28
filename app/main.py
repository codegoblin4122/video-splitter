# app/main.py
import os
import json
import math
import uuid
import shutil
import subprocess
from datetime import datetime, timedelta
from threading import Lock
from typing import Optional, List

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Body, Header
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from jose import jwt, JWTError

# Config

APP_NAME = "Video Splitter API"
APP_VERSION = "1.0.0"
DATA_DIR = os.environ.get("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)

# Auth to be updated
SECRET = os.environ.get("JWT_SECRET", "dev-only-change-me")
ALGO = "HS256"

# Hard-coded user and admin user for A1
USERS = {
    "admin": {"password": "admin123", "role": "admin"},
    "user": {"password": "user123", "role": "user"},
}

# In-memory job registry 
JOBS = {}
JLOCK = Lock()

# App

app = FastAPI(title=APP_NAME, version=APP_VERSION)

# CORS 
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve optional static web assets at /web
if os.path.isdir("static"):
    app.mount("/web", StaticFiles(directory="static"), name="static")

# Auth helpers

def make_jwt(username: str, role: str) -> str:
    payload = {
        "sub": username,
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=8),
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, SECRET, algorithm=ALGO)

def decode_bearer(authorization: str) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1]
    try:
        return jwt.decode(token, SECRET, algorithms=[ALGO])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

def current_user(authorization: str) -> str:
    claims = decode_bearer(authorization)
    return claims.get("sub")

def require_role(authorization: str, role: str):
    claims = decode_bearer(authorization)
    if claims.get("role") != role:
        raise HTTPException(status_code=403, detail="Forbidden")

# ffmpeg helpers

def _check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        subprocess.run(["ffprobe", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except Exception:
        raise HTTPException(500, "ffmpeg/ffprobe not found on PATH")

def ffprobe_duration(path: str) -> float:
    _check_ffmpeg()
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    try:
        return float(out)
    except ValueError:
        raise HTTPException(400, "Could not determine video duration")

def split_video(in_path: str, out_dir: str, parts: int, mode: str) -> List[str]:
    """
    mode=fast  -> stream copy (I/O-bound)
    mode=heavy -> re-encode + filters (CPU-bound)
    """
    _check_ffmpeg()
    os.makedirs(out_dir, exist_ok=True)
    duration = ffprobe_duration(in_path)
    seg_time = max(duration / max(parts, 1), 0.1)

    if mode not in ("fast", "heavy"):
        mode = "heavy"

    if mode == "fast":
        cmd = [
            "ffmpeg", "-y", "-i", in_path,
            "-c", "copy",
            "-f", "segment",
            "-segment_time", f"{seg_time}",
            "-reset_timestamps", "1",
            os.path.join(out_dir, "part_%02d.mp4"),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", in_path,
            "-vf", "scale=1280:-2,unsharp=5:5:1.0",
            "-c:v", "libx264", "-preset", "slower", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-f", "segment",
            "-segment_time", f"{seg_time}",
            "-reset_timestamps", "1",
            os.path.join(out_dir, "part_%02d.mp4"),
        ]

    # Execute
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"ffmpeg failed: {e}")

    files = sorted(
        [os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.lower().endswith(".mp4")]
    )
    return files

# Models (lightweight – use Body())

def json_meta_path(video_dir: str) -> str:
    return os.path.join(video_dir, "meta.json")

def read_meta(video_dir: str) -> Optional[dict]:
    p = json_meta_path(video_dir)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def write_meta(video_dir: str, meta: dict):
    with open(json_meta_path(video_dir), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

# API: health/auth

@app.get("/healthz")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat() + "Z", "version": APP_VERSION}

@app.post("/auth/login")
def login(username: str = Body(...), password: str = Body(...)):
    u = USERS.get(username)
    if not u or u["password"] != password:
        raise HTTPException(status_code=401, detail="Bad credentials")
    return {"token": make_jwt(username, u["role"]), "role": u["role"]}

# API: videos

@app.post("/videos/upload")
async def upload_video(
    file: UploadFile = File(...),
    authorization: str = Header(default=""),
):
    user = current_user(authorization)
    vid_id = str(uuid.uuid4())
    vdir = os.path.join(DATA_DIR, vid_id)
    os.makedirs(vdir, exist_ok=True)

    in_path = os.path.join(vdir, "input.mp4")
    try:
        with open(in_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    duration = ffprobe_duration(in_path)
    meta = {
        "owner": user,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "duration": duration,
        "filename": file.filename,
        "video_id": vid_id,
    }
    write_meta(vdir, meta)
    return {"video_id": vid_id, "duration": duration, "filename": file.filename}

@app.get("/videos")
def list_videos(
    page: int = 1,
    page_size: int = 25,
    authorization: str = Header(default=""),
):
    user = current_user(authorization)
    items = []
    for vid in os.listdir(DATA_DIR):
        vdir = os.path.join(DATA_DIR, vid)
        if not os.path.isdir(vdir):
            continue
        meta = read_meta(vdir)
        if not meta:
            continue
        # owner filter – basic multi-user separation
        if meta.get("owner") == user or USERS.get(user, {}).get("role") == "admin":
            items.append({
                "video_id": vid,
                "duration": meta.get("duration"),
                "created_at": meta.get("created_at"),
                "filename": meta.get("filename"),
            })
    #  pagination?
    items.sort(key=lambda x: x["created_at"], reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    return {"total": total, "page": page, "page_size": page_size, "videos": items[start:end]}

@app.get("/videos/{video_id}")
def get_video(video_id: str, authorization: str = Header(default="")):
    user = current_user(authorization)
    vdir = os.path.join(DATA_DIR, video_id)
    meta = read_meta(vdir)
    if not meta:
        raise HTTPException(404, "Video not found")
    owner = meta.get("owner")
    role = USERS.get(user, {}).get("role")
    if role != "admin" and owner != user:
        raise HTTPException(403, "Forbidden")
    return {
        "video_id": video_id,
        "filename": meta.get("filename"),
        "duration": meta.get("duration"),
        "created_at": meta.get("created_at"),
        "source_url": f"/segments/{video_id}/source/input.mp4",
    }

@app.get("/segments/{video_id}/source/input.mp4")
def get_source(video_id: str, authorization: str = Header(default="")):
    user = current_user(authorization)
    vdir = os.path.join(DATA_DIR, video_id)
    meta = read_meta(vdir)
    if not meta:
        raise HTTPException(404, "Video not found")
    owner = meta.get("owner")
    role = USERS.get(user, {}).get("role")
    if role != "admin" and owner != user:
        raise HTTPException(403, "Forbidden")

    path = os.path.join(vdir, "input.mp4")
    if not os.path.exists(path):
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="video/mp4", filename="input.mp4")

# API: splitting

def _authorize_video_access(video_id: str, user: str):
    vdir = os.path.join(DATA_DIR, video_id)
    meta = read_meta(vdir)
    if not meta:
        raise HTTPException(404, "Video not found")
    role = USERS.get(user, {}).get("role")
    if role != "admin" and meta.get("owner") != user:
        raise HTTPException(403, "Forbidden")
    return vdir, meta

@app.post("/videos/{video_id}/split")
def split_sync(
    video_id: str,
    parts: int = 10,
    mode: str = "heavy",
    authorization: str = Header(default=""),
):
    user = current_user(authorization)
    vdir, _ = _authorize_video_access(video_id, user)
    in_path = os.path.join(vdir, "input.mp4")
    if not os.path.exists(in_path):
        raise HTTPException(404, "Video not found")

    out_dir = os.path.join(vdir, f"segments_{mode}")
    files = split_video(in_path, out_dir, parts, mode)
    segments = [f"/segments/{video_id}/{mode}/{os.path.basename(x)}" for x in files]
    return {"video_id": video_id, "mode": mode, "parts": len(files), "segments": segments}

def _run_split_job(video_id: str, parts: int, mode: str):
    try:
        vdir = os.path.join(DATA_DIR, video_id)
        in_path = os.path.join(vdir, "input.mp4")
        out_dir = os.path.join(vdir, f"segments_{mode}")
        with JLOCK:
            JOBS[video_id] = {"status": "processing", "mode": mode, "started_at": datetime.utcnow().isoformat() + "Z"}
        files = split_video(in_path, out_dir, parts, mode)
        with JLOCK:
            JOBS[video_id] = {
                "status": "done",
                "mode": mode,
                "parts": len(files),
                "finished_at": datetime.utcnow().isoformat() + "Z",
            }
    except Exception as e:
        with JLOCK:
            JOBS[video_id] = {"status": "error", "error": str(e), "finished_at": datetime.utcnow().isoformat() + "Z"}

@app.post("/videos/{video_id}/split_async")
def split_async(
    video_id: str,
    parts: int = 10,
    mode: str = "heavy",
    background_tasks: BackgroundTasks = None,
    authorization: str = Header(default=""),
):
    user = current_user(authorization)
    vdir, _ = _authorize_video_access(video_id, user)
    if not os.path.exists(os.path.join(vdir, "input.mp4")):
        raise HTTPException(404, "Video not found")

    with JLOCK:
        JOBS[video_id] = {"status": "queued", "mode": mode, "queued_at": datetime.utcnow().isoformat() + "Z"}
    background_tasks.add_task(_run_split_job, video_id, parts, mode)
    return {"job": video_id, "status": "queued"}

@app.get("/jobs/{video_id}")
def job_status(video_id: str, authorization: str = Header(default="")):
    user = current_user(authorization)
    vdir, _ = _authorize_video_access(video_id, user)
    with JLOCK:
        job = JOBS.get(video_id)
    if not job:
        return {"status": "unknown"}
    return job

# API: listing & downloading segments

@app.get("/videos/{video_id}/segments")
def list_segments(
    video_id: str,
    mode: Optional[str] = None,
    authorization: str = Header(default=""),
):
    user = current_user(authorization)
    vdir, _ = _authorize_video_access(video_id, user)

    modes = []
    if mode:
        modes = [f"segments_{mode}"]
    else:
        modes = [d for d in os.listdir(vdir) if d.startswith("segments_") and os.path.isdir(os.path.join(vdir, d))]

    outputs = []
    for m in modes:
        mdir = os.path.join(vdir, m)
        if not os.path.isdir(mdir):
            continue
        files = sorted([f for f in os.listdir(mdir) if f.lower().endswith(".mp4")])
        outputs.append({
            "mode": m.replace("segments_", ""),
            "parts": len(files),
            "segments": [f"/segments/{video_id}/{m.replace('segments_','')}/{name}" for name in files],
        })

    return {"video_id": video_id, "outputs": outputs}

@app.get("/segments/{video_id}/{mode}/{filename}")
def get_segment(
    video_id: str,
    mode: str,
    filename: str,
    authorization: str = Header(default=""),
):
    user = current_user(authorization)
    vdir, _ = _authorize_video_access(video_id, user)
    path = os.path.join(vdir, f"segments_{mode}", filename)
    if not os.path.exists(path):
        raise HTTPException(404, "Not found")

    resp = FileResponse(path, media_type="video/mp4", filename=filename)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp