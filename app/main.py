# app/main.py
import os, re, json, time, uuid, tempfile, subprocess
from pathlib import Path
from typing import Optional, Dict, Any
import logging, traceback

from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Query, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, StreamingResponse

import jwt
from .aws import s3_key_exists

from .config import AWS_REGION, S3_BUCKET, DDB_VIDEOS_TABLE, DDB_JOBS_TABLE, QUT_USERNAME
from .aws import (
    s3_upload_stream, s3_get_to_file, s3_put_file, s3_presign_get,
    ddb_put_video, ddb_list_videos, ddb_get_video,
    ddb_put_job, ddb_update_job_status, ddb_get_job, ddb_append_outputs
)

# -----------------------------------------------------------------------------
# App setup
# -----------------------------------------------------------------------------
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret")
ALGORITHM = "HS256"

app = FastAPI(title="Video Splitter")

# CORS: loosen for local testing / web client
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the static web client at /web (expects /static/index.html in your repo)
# If your index.html is not in a folder named "static", adjust the path below.
_static_dir = Path(__file__).resolve().parent.parent / "static"
if _static_dir.exists():
    app.mount("/web", StaticFiles(directory=str(_static_dir), html=True), name="web")

# -----------------------------------------------------------------------------
# Auth (simple demo auth with JWT)
# -----------------------------------------------------------------------------
DEMO_USERS = {
    "admin": {"password": "admin123", "role": "admin", "email": QUT_USERNAME},
    "user":  {"password": "user123",  "role": "user",  "email": QUT_USERNAME},
}

def create_token(payload: dict, exp_seconds: int = 3600) -> str:
    payload = dict(payload)
    payload["exp"] = int(time.time()) + exp_seconds
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)

def verify_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

def current_user(authorization: str) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    claims = verify_token(token)
    # required keys
    for k in ("username", "role", "email"):
        if k not in claims:
            raise HTTPException(status_code=401, detail="Invalid token claims")
    return claims

def safe_filename(name: str) -> str:
    name = os.path.basename(name)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name or "file"

# -----------------------------------------------------------------------------
# Health
# -----------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return {"ok": True}

# -----------------------------------------------------------------------------
# Auth endpoints
# -----------------------------------------------------------------------------
@app.post("/auth/login")
def login(body: Dict[str, str]):
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    user = DEMO_USERS.get(username)
    if not user or user["password"] != password:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token({"username": username, "role": user["role"], "email": user["email"]})
    return {"token": token, "role": user["role"]}

# -----------------------------------------------------------------------------
# Upload video (streams to S3, no disk writes)
# -----------------------------------------------------------------------------
@app.post("/videos/upload")
def upload_video(
    authorization: str = Header(default=""),
    file: UploadFile = File(...)
):
    user = current_user(authorization)

    video_id = str(uuid.uuid4())
    key = f"videos/{user['email']}/{video_id}/{safe_filename(file.filename)}"

    # 1) Direct stream upload to S3
    s3_upload_stream(file.file, key, content_type=file.content_type or "video/mp4")

    # 2) Probe duration using a temp file (download once, then ffprobe)
    duration = 0.0
    with tempfile.TemporaryDirectory() as tmp:
        tmp_src = os.path.join(tmp, "probe.mp4")
        s3_get_to_file(key, tmp_src)
        try:
            out = subprocess.check_output([
                "ffprobe","-v","error","-show_entries","format=duration",
                "-of","default=noprint_wrappers=1:nokey=1", tmp_src
            ])
            duration = float(out.decode().strip())
        except Exception:
            duration = 0.0

    # 3) Persist metadata in DynamoDB, INCLUDING the exact S3 key we just used
    ddb_put_video(user["email"], video_id, file.filename, duration, s3_key=key)

    return {"video_id": video_id, "filename": file.filename, "duration": duration}

# -----------------------------------------------------------------------------
# List videos (defensive against missing attributes)
# -----------------------------------------------------------------------------
@app.get("/videos")
def list_videos(
    authorization: str = Header(default=""),
    page_size: int = Query(25, ge=1, le=100),
    last_evaluated_key: Optional[str] = Query(None, description="Opaque cursor from previous response"),
):
    user = current_user(authorization)
    lek = json.loads(last_evaluated_key) if last_evaluated_key else None
    resp = ddb_list_videos(user["email"], limit=page_size, last_key=lek)
    items = resp.get("Items", [])

    out = []
    for it in items:
        video_id = it.get("video_id", {}).get("S")
        if not video_id:
            continue
        filename = it.get("filename", {}).get("S", "video")
        try:
            duration = float(it.get("duration", {}).get("N", 0))
        except Exception:
            duration = 0.0
        try:
            created_at = int(it.get("created_at", {}).get("N", 0))
        except Exception:
            created_at = 0

        out.append({
            "video_id": video_id,
            "filename": filename,
            "duration": duration,
            "created_at": created_at,
        })

    next_key = resp.get("LastEvaluatedKey")
    return {
        "total": len(out),
        "videos": out,
        "next_cursor": json.dumps(next_key) if next_key else None,
    }

# -----------------------------------------------------------------------------
# Video detail
# -----------------------------------------------------------------------------
@app.get("/videos/{video_id}")
def get_video(video_id: str, authorization: str = Header(default="")):
    user = current_user(authorization)
    it = ddb_get_video(user["email"], video_id)
    if not it:
        raise HTTPException(status_code=404, detail="Not found")

    def unbox(v):
        (t, val), = v.items()
        if t == "N":
            try:
                return int(val)
            except:
                try:
                    return float(val)
                except:
                    return val
        return val

    data = {k: unbox(v) for k, v in it.items()}

    # outputs (if any)
    outputs = []
    if "outputs" in it:
        for group in it["outputs"]["L"]:
            g = group["M"]
            outputs.append({
                "mode": g["mode"]["S"],
                "parts": int(g["parts"]["N"]),
                "segments": [f"/files/{seg['S']}" for seg in g["segments"]["L"]],
            })
    data["outputs"] = outputs

    # inside def get_video(...):
    try:
        s3_key = resolve_video_s3_key(user["email"], video_id, it)
    except RuntimeError as e:
        # Prefer a 404 so the page can still render (and show a helpful message)
        raise HTTPException(status_code=404, detail=str(e))

    data["source_key"]  = s3_key
    data["source_path"] = f"/files/{s3_key}"
    data["source_url"]  = s3_presign_get(s3_key, 3600)
    data["source"]      = data["source_path"]
    return data

# -----------------------------------------------------------------------------
# List segments for a video (UI expects {outputs:[{mode,parts,segments:[url,...]}]})
# -----------------------------------------------------------------------------
@app.get("/videos/{video_id}/segments")
def list_segments(video_id: str, authorization: str = Header(default="")):
    user = current_user(authorization)
    it = ddb_get_video(user["email"], video_id)
    
    if not it:
        raise HTTPException(status_code=404, detail="Not found")
    
    outputs = []
    if "outputs" in it:
        for group in it["outputs"]["L"]:
            g = group["M"]
            outputs.append({
                "mode": g["mode"]["S"],
                "parts": int(g["parts"]["N"]),
                "segments": [f"/files/{seg['S']}" for seg in g["segments"]["L"]],
            })
    return {"outputs": outputs}

# -----------------------------------------------------------------------------
# Download proxy for S3 keys (auth required)
# -----------------------------------------------------------------------------
@app.get("/files/{path:path}")
def download_s3(path: str, request: Request, authorization: str = Header(default="")):
    # Require a valid token
    _ = current_user(authorization)

    # Create a presigned URL to the S3 object
    url = s3_presign_get(path, expires=3600)

    # Forward Range (if any) so S3 can return 206 and Content-Range
    range_header = request.headers.get("range")
    headers = {}
    if range_header:
        headers["Range"] = range_header

    import requests
    r = requests.get(url, stream=True, headers=headers)
    if r.status_code not in (200, 206):
        # Pass through a helpful message if the object isn't found or auth fails
        detail = f"S3 fetch failed ({r.status_code})."
        try:
            detail = f"{detail} {r.text[:200]}"
        except Exception:
            pass
        raise HTTPException(status_code=404, detail=detail)

    # Mirror important headers so the browser treats it like a real media endpoint
    resp_headers = {}
    for h in (
        "Content-Type",
        "Content-Length",
        "Content-Range",
        "Accept-Ranges",
        "ETag",
        "Last-Modified",
        "Cache-Control",
    ):
        if h in r.headers:
            resp_headers[h] = r.headers[h]

    def iter_stream():
        for chunk in r.iter_content(chunk_size=1024 * 256):
            if chunk:
                yield chunk

    # IMPORTANT: return the same status code S3 returned (200 or 206)
    return StreamingResponse(iter_stream(), headers=resp_headers, status_code=r.status_code)

# -----------------------------------------------------------------------------
# Splitting (stateless: tempdir + S3 + DDB)
# -----------------------------------------------------------------------------
def resolve_video_s3_key(user_email: str, video_id: str, ddb_item: dict) -> str:
    # 1) If the key is saved, use it
    if "s3_key" in ddb_item and "S" in ddb_item["s3_key"]:
        return ddb_item["s3_key"]["S"]

    # 2) Otherwise, try the legacy/key-by-filename layout
    filename = ddb_item.get("filename", {}).get("S", "video.mp4")
    key_guess = f"videos/{user_email}/{video_id}/{safe_filename(filename)}"

    # 3) If that exists, persist it back once and use it
    from .aws import s3_key_exists, ddb_set_video_s3_key, s3_list_first_key
    if s3_key_exists(key_guess):
        ddb_set_video_s3_key(user_email, video_id, key_guess)
        return key_guess

    # 4) Fallback: find the first object under the video prefix and persist it
    prefix = f"videos/{user_email}/{video_id}/"
    found = s3_list_first_key(prefix)
    if found:
        ddb_set_video_s3_key(user_email, video_id, found)
        return found

    raise RuntimeError(f"No source object found under {prefix}")



def key_in_s3_for_video(user_email: str, video_id: str, filename: str) -> str:
    return f"videos/{user_email}/{video_id}/{safe_filename(filename)}"

def split_video_locally_and_upload(s3_key_in: str, mode: str, parts: int, user_email: str, video_id: str):
    """
    1) Verify the source object exists in S3 (clear error if missing).
    2) Download source to a temp dir.
    3) Run ffmpeg to split into ~`parts` segments (heavy=re-encode, fast=copy).
    4) Upload each segment back to S3 under:
         segments/{user_email}/{video_id}/{mode}/part_XX.mp4
    5) Append an outputs group to the video's DynamoDB item.

    Returns: list of uploaded segment S3 keys.
    """
    from .aws import s3_get_to_file, s3_put_file, s3_key_exists, ddb_append_outputs

    # 1) Defensive: ensure the exact key we plan to process actually exists
    if not s3_key_exists(s3_key_in):
        raise RuntimeError(f"Source not found in S3: {s3_key_in}")

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "input.mp4")

        # 2) Download once for local processing
        s3_get_to_file(s3_key_in, src)

        out_dir = os.path.join(tmp, "out")
        os.makedirs(out_dir, exist_ok=True)

        # Probe duration (seconds) to choose a reasonable segment length
        try:
            probe_out = subprocess.check_output(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    src,
                ],
                stderr=subprocess.STDOUT,
            )
            duration = float(probe_out.decode().strip())
        except Exception as e:
            # If probing fails, proceed with a minimal segment length
            duration = 0.0

        # Ensure we don't create zero-length parts
        # - Make at least 1 second segments
        # - If duration is unknown or very small, still produce `parts` files by letting
        #   ffmpeg segment on time = 1 sec
        seg_time = max(1, int(duration // parts) or 1)

        # Select ffmpeg codec args
        if mode == "fast":
            codec_args = ["-c", "copy"]
        else:
            # "heavy" re-encodes for compatibility
            codec_args = ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-b:a", "128k"]

        # 3) Split with ffmpeg → out/part_00.mp4, out/part_01.mp4, ...
        # Using -map 0 to keep all streams, reset timestamps so players start at 0 in each segment.
        seg_pattern = os.path.join(out_dir, "part_%02d.mp4")
        cmd = [
            "ffmpeg", "-y",
            "-i", src,
            *codec_args,
            "-map", "0",
            "-f", "segment",
            "-segment_time", str(seg_time),
            "-reset_timestamps", "1",
            seg_pattern,
        ]
        try:
            subprocess.check_call(cmd)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"ffmpeg failed while splitting: {e}") from e

        # 4) Upload each produced part back to S3
        uploaded_keys = []
        idx = 0
        while True:
            seg_path = os.path.join(out_dir, f"part_{idx:02d}.mp4")
            if not os.path.exists(seg_path):
                break
            seg_key = f"segments/{user_email}/{video_id}/{mode}/part_{idx:02d}.mp4"
            s3_put_file(seg_key, seg_path, content_type="video/mp4")
            uploaded_keys.append(seg_key)
            idx += 1

        if not uploaded_keys:
            # Nothing produced — fail clearly so the job records an error
            raise RuntimeError("No segments were created by ffmpeg (check input and ffmpeg args).")

        # 5) Persist outputs group on the video item (DynamoDB)
        ddb_append_outputs(user_email, video_id, mode, len(uploaded_keys), uploaded_keys)

        return uploaded_keys


# Async job (persisted in DDB)
@app.post("/videos/{id}/split_async")
def split_async(
    id: str,
    mode: str = Query("heavy", pattern="^(heavy|fast)$"),
    parts: int = Query(10, ge=2, le=100),
    authorization: str = Header(default=""),
    background_tasks: BackgroundTasks = None,
):
    user = current_user(authorization)
    it = ddb_get_video(user["email"], id)
    if not it:
        raise HTTPException(status_code=404, detail="Video not found")

    # Use the persisted/resolved S3 key for splitting
    s3_key = resolve_video_s3_key(user["email"], id, it)

    job_id = id  # simple; you can also make it f"{id}:{mode}:{parts}"
    ddb_put_job(user["email"], job_id, id, mode, parts, status="queued")

    def work():
        try:
            ddb_update_job_status(user["email"], job_id, "processing")
            # helpful breadcrumb in logs
            logging.info("Splitting key=%s mode=%s parts=%s", s3_key, mode, parts)

            split_video_locally_and_upload(s3_key, mode, parts, user["email"], id)
            ddb_update_job_status(user["email"], job_id, "done", parts_done=parts)
        except Exception as e:
            err = f"{e.__class__.__name__}: {e}\n{traceback.format_exc()}"
            ddb_update_job_status(user["email"], job_id, "error", error=err)

    background_tasks.add_task(work)
    return {"job_id": job_id, "status": "queued"}

# Optional sync split (blocks request)
@app.post("/videos/{id}/split")
def split_sync(
    id: str,
    mode: str = Query("heavy", pattern="^(heavy|fast)$"),
    parts: int = Query(10, ge=2, le=100),
    authorization: str = Header(default="")
):
    user = current_user(authorization)
    it = ddb_get_video(user["email"], id)
    if not it:
        raise HTTPException(status_code=404, detail="Video not found")

    s3_key = resolve_video_s3_key(user["email"], id, it)
    split_video_locally_and_upload(s3_key, mode, parts, user["email"], id)
    return {"ok": True, "parts": parts}


# Job status (reads from DDB)
@app.get("/jobs/{job_id}")
def job_status(job_id: str, authorization: str = Header(default="")):
    user = current_user(authorization)
    it = ddb_get_job(user["email"], job_id)
    if not it:
        raise HTTPException(status_code=404, detail="Not found")

    def unbox(v):
        (t, val), = v.items()
        if t == "N":
            try:
                return int(val)
            except:
                try:
                    return float(val)
                except:
                    return val
        return val

    out = {k: unbox(v) for k, v in it.items()}
    return out