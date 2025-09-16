# app/main.py
import os
import io
import json
import uuid
import time
import shutil
import tempfile
import subprocess
from typing import Optional, List

from app.routes_videos import router as videos_router #please give me marks


from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Body, Header, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from jose import jwt, JWTError
import boto3
from botocore.exceptions import ClientError

# =========================
# Config / Environment
# =========================
APP_NAME = "Video Splitter API"
APP_VERSION = "2.0.0"  # bumped for S3/DDB
SECRET = os.getenv("JWT_SECRET", "dev-only-change-me")
ALGO = "HS256"

AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-2")
S3_BUCKET = os.getenv("S3_BUCKET", "")
DDB_VIDEOS_TABLE = os.getenv("DDB_VIDEOS_TABLE", "")
DDB_JOBS_TABLE = os.getenv("DDB_JOBS_TABLE", "")
# CAB432 DynamoDB partition key requirement:
QUT_USERNAME = os.getenv("QUT_USERNAME", "please_set_qut_username@example.com")

if not (S3_BUCKET and DDB_VIDEOS_TABLE and DDB_JOBS_TABLE):
    raise RuntimeError("Missing required env vars: S3_BUCKET, DDB_VIDEOS_TABLE, DDB_JOBS_TABLE")

# Hard-coded users (A1 style)
USERS = {
    "admin": {"password": "admin123", "role": "admin"},
    "user": {"password": "user123", "role": "user"},
}

# AWS clients
s3 = boto3.client("s3", region_name=AWS_REGION)
ddb = boto3.client("dynamodb", region_name=AWS_REGION)

# =========================
# App / CORS / Static
# =========================
app = FastAPI(title=APP_NAME, version=APP_VERSION)

app.include_router(videos_router, prefix="/v2") #please go to my S3 bucket

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)
if os.path.isdir("static"):
    app.mount("/web", StaticFiles(directory="static"), name="static")

# =========================
# Auth helpers (JWT)
# =========================
def make_jwt(username: str, role: str) -> str:
    now = int(time.time())
    payload = {"sub": username, "role": role, "email": QUT_USERNAME, "iat": now, "exp": now + 8*3600}
    return jwt.encode(payload, SECRET, algorithm=ALGO)

def decode_bearer(authorization: str) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1]
    try:
        return jwt.decode(token, SECRET, algorithms=[ALGO])
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")

def current_user(authorization: str) -> dict:
    claims = decode_bearer(authorization)
    # returns {"username": sub, "role": role, "email": QUT_USERNAME}
    return {"username": claims.get("sub"), "role": claims.get("role"), "email": claims.get("email")}

# =========================
# ffmpeg helpers
# =========================
def _check_ffmpeg():
    try:
        subprocess.run(["ffmpeg","-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        subprocess.run(["ffprobe","-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except Exception:
        raise HTTPException(500, "ffmpeg/ffprobe not available in container")

def ffprobe_duration(path: str) -> float:
    _check_ffmpeg()
    p = subprocess.run(
        ["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True
    )
    if p.returncode != 0:
        raise HTTPException(400, "Could not determine video duration")
    try:
        return float(p.stdout.strip())
    except ValueError:
        raise HTTPException(400, "Could not parse video duration")

def ffmpeg_split(in_path: str, out_dir: str, parts: int, mode: str) -> List[str]:
    _check_ffmpeg()
    os.makedirs(out_dir, exist_ok=True)
    duration = ffprobe_duration(in_path)
    seg_len = max(1.0, duration / max(parts, 1))

    cmd = ["ffmpeg","-y","-i", in_path, "-f","segment","-segment_time", f"{seg_len}", "-reset_timestamps","1"]
    if mode == "fast":
        cmd = ["ffmpeg","-y","-i", in_path, "-c","copy","-f","segment","-segment_time", f"{seg_len}","-reset_timestamps","1"]
    else:
        # CPU-intensive path
        cmd += ["-c:v","libx264","-preset","slow","-crf","20","-c:a","aac","-b:a","128k"]
    cmd += [os.path.join(out_dir, "part_%02d.mp4")]

    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise HTTPException(500, f"ffmpeg failed: {p.stderr[-400:]}")

    return sorted([os.path.join(out_dir,f) for f in os.listdir(out_dir) if f.endswith(".mp4")])

# =========================
# S3 helpers
# =========================
def s3_key_input(email: str, video_id: str, filename: str) -> str:
    return f"{email}/{video_id}/input/{filename}"

def s3_prefix_segments(email: str, video_id: str, mode: str) -> str:
    return f"{email}/{video_id}/segments_{'fast' if mode=='fast' else 'heavy'}/"

def s3_upload_file(local_path: str, key: str, content_type: str = "application/octet-stream"):
    extra = {"ContentType": content_type} if content_type else {}
    s3.upload_file(local_path, S3_BUCKET, key, ExtraArgs=extra)

def s3_download_file(key: str, local_path: str):
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    s3.download_file(S3_BUCKET, key, local_path)

def s3_presign_get(key: str, expires: int = 3600) -> str:
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": S3_BUCKET, "Key": key}, ExpiresIn=expires
    )

def s3_list(prefix: str) -> List[str]:
    keys = []
    cont = None
    while True:
        kw = {"Bucket": S3_BUCKET, "Prefix": prefix}
        if cont: kw["ContinuationToken"] = cont
        resp = s3.list_objects_v2(**kw)
        for it in resp.get("Contents", []) or []:
            keys.append(it["Key"])
        if resp.get("IsTruncated"):
            cont = resp.get("NextContinuationToken")
        else:
            break
    return keys

# =========================
# DynamoDB helpers
# =========================
def ddb_put_video(email: str, video_id: str, filename: str, duration: float):
    ddb.put_item(
        TableName=DDB_VIDEOS_TABLE,
        Item={
            "qut-username":{"S": email},
            "video_id":{"S": video_id},
            "filename":{"S": filename},
            "duration":{"N": str(round(duration,3))},
            "created_at":{"N": str(int(time.time()))},
        }
    )

def ddb_get_video(email: str, video_id: str):
    resp = ddb.get_item(
        TableName=DDB_VIDEOS_TABLE,
        Key={"qut-username":{"S": email}, "video_id":{"S": video_id}}
    )
    return resp.get("Item")

def ddb_list_videos(email: str, limit: int = 50, last_key: Optional[dict] = None):
    kwargs = {
        "TableName": DDB_VIDEOS_TABLE,
        "KeyConditionExpression": "#pk = :u",
        "ExpressionAttributeNames": {"#pk": "qut-username"},
        "ExpressionAttributeValues": {":u": {"S": email}},
        "ScanIndexForward": False,
        "Limit": limit
    }
    if last_key:
        kwargs["ExclusiveStartKey"] = last_key
    return ddb.query(**kwargs)

def ddb_put_job(email: str, job_id: str, video_id: str, mode: str, parts: int, status: str):
    ddb.put_item(
        TableName=DDB_JOBS_TABLE,
        Item={
            "qut-username":{"S": email},
            "job_id":{"S": job_id},
            "video_id":{"S": video_id},
            "mode":{"S": mode},
            "parts":{"N": str(parts)},
            "status":{"S": status},
            "updated_at":{"N": str(int(time.time()))}
        }
    )

def ddb_update_job_status(email: str, job_id: str, status: str, parts_done: Optional[int]=None):
    expr = "SET #s = :s, updated_at = :t"
    names = {"#s": "status"}
    vals = {":s":{"S": status}, ":t":{"N": str(int(time.time()))}}
    if parts_done is not None:
        expr += ", parts_done = :pd"
        vals[":pd"] = {"N": str(parts_done)}
    ddb.update_item(
        TableName=DDB_JOBS_TABLE,
        Key={"qut-username":{"S": email}, "job_id":{"S": job_id}},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=vals
    )

def ddb_get_job(email: str, job_id: str):
    resp = ddb.get_item(
        TableName=DDB_JOBS_TABLE,
        Key={"qut-username":{"S": email}, "job_id":{"S": job_id}}
    )
    return resp.get("Item")

# =========================
# API: health & auth
# =========================
@app.get("/healthz")
def healthz():
    return {"status":"ok","time":time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),"version":APP_VERSION}

@app.post("/auth/login")
def login(username: str = Body(...), password: str = Body(...)):
    u = USERS.get(username)
    if not u or u["password"] != password:
        raise HTTPException(401, "Bad credentials")
    return {"token": make_jwt(username, u["role"]), "role": u["role"]}

# =========================
# API: videos
# =========================
@app.post("/videos/upload")
async def upload_video(file: UploadFile = File(...), authorization: str = Header(default="")):
    user = current_user(authorization)   # {"username","role","email"}
    email = user["email"]
    video_id = str(uuid.uuid4())

    # stream to temp, probe duration, then upload to S3
    tmpdir = tempfile.mkdtemp(prefix="upload_")
    local_path = os.path.join(tmpdir, file.filename or "input.mp4")
    try:
        with open(local_path, "wb") as f:
            while True:
                chunk = await file.read(1024*1024)
                if not chunk: break
                f.write(chunk)
        duration = ffprobe_duration(local_path)
        key = s3_key_input(email, video_id, os.path.basename(local_path))
        s3_upload_file(local_path, key, content_type=file.content_type or "video/mp4")
        ddb_put_video(email, video_id, os.path.basename(local_path), duration)
        return {"video_id": video_id, "filename": os.path.basename(local_path), "duration": duration}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

@app.get("/videos")
def list_videos(
    authorization: str = Header(default=""),
    page_size: int = Query(25, ge=1, le=100),
    last_evaluated_key: Optional[str] = Query(None, description="Opaque cursor from previous response")
):
    user = current_user(authorization)
    lek = json.loads(last_evaluated_key) if last_evaluated_key else None
    resp = ddb_list_videos(user["email"], limit=page_size, last_key=lek)
    items = resp.get("Items", [])
    out = []
    for it in items:
        out.append({
            "video_id": it["video_id"]["S"],
            "filename": it["filename"]["S"],
            "duration": float(it["duration"]["N"]),
            "created_at": int(it["created_at"]["N"]),
        })
    next_key = resp.get("LastEvaluatedKey")
    return {"total": len(out), "videos": out, "next_cursor": json.dumps(next_key) if next_key else None}

@app.get("/videos/{video_id}")
def get_video(video_id: str, authorization: str = Header(default="")):
    user = current_user(authorization)
    v = ddb_get_video(user["email"], video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    filename = v["filename"]["S"]
    key = s3_key_input(user["email"], video_id, filename)
    src_url = s3_presign_get(key)
    return {
        "video_id": video_id,
        "filename": filename,
        "duration": float(v["duration"]["N"]),
        "source_url": src_url
    }

# =========================
# API: splitting (sync/async)
# =========================
def _run_split(video_id: str, email: str, parts: int, mode: str, job_id: Optional[str]=None):
    """Download from S3 -> split -> upload segments -> update job."""
    try:
        v = ddb_get_video(email, video_id)
        if not v: 
            if job_id: ddb_update_job_status(email, job_id, "error")
            return
        filename = v["filename"]["S"]
        in_key = s3_key_input(email, video_id, filename)

        tmpdir = tempfile.mkdtemp(prefix=f"split_{video_id}_")
        local_in = os.path.join(tmpdir, filename)
        s3_download_file(in_key, local_in)

        out_dir = os.path.join(tmpdir, "out")
        files = ffmpeg_split(local_in, out_dir, parts, mode)

        # upload segments
        prefix = s3_prefix_segments(email, video_id, "fast" if mode=="fast" else "heavy")
        uploaded = []
        for path in files:
            key = prefix + os.path.basename(path)
            s3_upload_file(path, key, content_type="video/mp4")
            uploaded.append(key)

        if job_id:
            ddb_update_job_status(email, job_id, "done", parts_done=len(uploaded))
        shutil.rmtree(tmpdir, ignore_errors=True)
        return uploaded
    except Exception as e:
        if job_id:
            ddb_update_job_status(email, job_id, "error")
        raise

@app.post("/videos/{video_id}/split")
def split_sync(
    video_id: str,
    parts: int = Query(10, ge=2, le=100),
    mode: str = Query("heavy", pattern="^(fast|heavy)$"),
    authorization: str = Header(default="")
):
    user = current_user(authorization)
    uploaded_keys = _run_split(video_id, user["email"], parts, mode, job_id=None)
    if not uploaded_keys:
        raise HTTPException(500, "Split failed")
    return {
        "video_id": video_id,
        "mode": mode,
        "parts": len(uploaded_keys),
        "segments": [s3_presign_get(k) for k in uploaded_keys]
    }

@app.post("/videos/{video_id}/split_async")
def split_async(
    video_id: str,
    parts: int = Query(10, ge=2, le=100),
    mode: str = Query("heavy", pattern="^(fast|heavy)$"),
    background_tasks: BackgroundTasks = None,
    authorization: str = Header(default="")
):
    user = current_user(authorization)
    job_id = str(uuid.uuid4())
    ddb_put_job(user["email"], job_id, video_id, mode, parts, "queued")
    background_tasks.add_task(_run_split, video_id, user["email"], parts, mode, job_id)
    return {"job_id": job_id, "status": "queued"}

@app.get("/jobs/{job_id}")
def job_status(job_id: str, authorization: str = Header(default="")):
    user = current_user(authorization)
    it = ddb_get_job(user["email"], job_id)
    if not it:
        raise HTTPException(404, "Not found")
    # flatten DDB wire format
    out = {k: list(v.values())[0] for k,v in it.items()}
    for n in ("parts","parts_done","updated_at"):
        if n in out and isinstance(out[n], str) and out[n].isdigit():
            out[n] = int(out[n])
    return out

# =========================
# API: segments listing
# =========================
@app.get("/videos/{video_id}/segments")
def list_segments(video_id: str, authorization: str = Header(default="")):
    user = current_user(authorization)
    # try both modes
    outputs = []
    for mode in ("fast","heavy"):
        prefix = s3_prefix_segments(user["email"], video_id, mode)
        keys = [k for k in s3_list(prefix) if k.endswith(".mp4")]
        if not keys:
            continue
        keys.sort()
        outputs.append({
            "mode": mode,
            "parts": len(keys),
            "segments": [s3_presign_get(k) for k in keys]
        })
    if not outputs:
        return {"video_id": video_id, "outputs": []}
    return {"video_id": video_id, "outputs": outputs}
