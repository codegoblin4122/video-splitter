import os, uuid, math, subprocess, tempfile, shutil, json
import boto3
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from .config import validate_cloud_env, AWS_REGION
from .aws import (
    s3_put_file, s3_get_to_file, s3_presign_get,
    ddb_put_video, ddb_list_videos, ddb_get_video,
    ddb_put_job, ddb_update_job_status, ddb_get_job
)
from .auth import require_user  # your existing JWT dependency

router = APIRouter()

# ---------- SQS config ----------
SQS_QUEUE_URL = os.getenv("https://sqs.ap-southeast-2.amazonaws.com/901444280953/n10254854-video-splitter")  # e.g. https://sqs.ap-southeast-2.amazonaws.com/123456789012/video-jobs
_sqs = boto3.client("sqs", region_name=AWS_REGION)

@router.on_event("startup")
def _assert_env():
    validate_cloud_env()
    # Fail fast if async splitting is enabled but queue URL missing
    if not SQS_QUEUE_URL:
        # Not raising here so you can still run sync flows without SQS
        # But we log a warning so it’s obvious in logs.
        try:
            import logging
            logging.getLogger("video-splitter").warning("SQS_QUEUE_URL not set; /split_async will 500")
        except Exception:
            pass

# /videos/upload -> store file in S3, write metadata in DDB
@router.post("/videos/upload")
async def upload_video(file: UploadFile = File(...), user=Depends(require_user)):
    # Save stream to tmp, probe duration, then upload to S3
    tmpdir = tempfile.mkdtemp(prefix="upload_")
    try:
        local_path = os.path.join(tmpdir, file.filename)
        with open(local_path, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)

        # probe duration with ffprobe
        dur_sec = probe_duration(local_path)

        video_id = str(uuid.uuid4())
        s3_key_input = f"{user['email']}/{video_id}/input/{file.filename}"
        s3_put_file(s3_key_input, local_path, content_type=file.content_type or "video/mp4")

        # write metadata to DDB
        ddb_put_video(user["email"], video_id, file.filename, dur_sec)

        return {"video_id": video_id, "filename": file.filename, "duration": dur_sec}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

def probe_duration(path: str) -> float:
    # requires ffprobe in image
    p = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True,
        text=True,
    )
    if p.returncode != 0:
        raise HTTPException(400, "ffprobe failed")
    try:
        return round(float(p.stdout.strip()), 3)
    except:
        return 0.0

@router.get("/videos")
def list_videos(user=Depends(require_user)):
    resp = ddb_list_videos(user["email"])
    # Allow both: a list of items OR a DynamoDB Query response with "Items"
    items = resp if isinstance(resp, list) else resp.get("Items", [])
    out = []
    for it in items:
        try:
            out.append({
                "video_id": it["video_id"]["S"],
                "filename": it["filename"]["S"],
                "duration": float(it["duration"]["N"]),
                "created_at": int(it["created_at"]["N"]),
            })
        except Exception:
            # Be forgiving if any field is missing/malformed
            pass
    return {"total": len(out), "videos": out}

@router.get("/videos/{video_id}")
def get_video(video_id: str, user=Depends(require_user)):
    it = ddb_get_video(user["email"], video_id)
    if not it:
        raise HTTPException(404, "Not found")
    return {
        "video_id": it["video_id"]["S"],
        "filename": it["filename"]["S"],
        "duration": float(it["duration"]["N"]),
    }

# ------------------ Split endpoints ------------------

@router.post("/videos/{video_id}/split")
def split_sync(
    video_id: str,
    mode: str = Query("heavy"),
    parts: int = Query(10, ge=2, le=100),
    user=Depends(require_user),
):
    return _do_split(video_id, mode, parts, user["email"], async_=False)

@router.post("/videos/{video_id}/split_async")
def split_async(
    video_id: str,
    mode: str = Query("heavy"),
    parts: int = Query(10, ge=2, le=100),
    user=Depends(require_user),
):
    """
    NEW: Enqueue a job to SQS instead of running a background thread here.
    An ECS/EC2 'worker' service will poll the queue and perform the split.
    """
    if not SQS_QUEUE_URL:
        raise HTTPException(500, "SQS_QUEUE_URL is not configured")

    # 1) Record a job in DDB for UI polling
    job_id = str(uuid.uuid4())
    ddb_put_job(user["email"], job_id, video_id, mode, parts, "queued")

    # 2) Send message to SQS for a worker to process
    payload = {
        "job_id": job_id,
        "video_id": video_id,
        "mode": mode,
        "parts": parts,
        "email": user["email"],
    }
    try:
        _sqs.send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps(payload),
            MessageAttributes={
                "job_id": {"DataType": "String", "StringValue": job_id},
                "video_id": {"DataType": "String", "StringValue": video_id},
                "mode": {"DataType": "String", "StringValue": mode},
                "parts": {"DataType": "Number", "StringValue": str(parts)},
            },
        )
    except Exception as e:
        # If enqueue fails, mark job error for visibility
        ddb_update_job_status(user["email"], job_id, "error")
        raise HTTPException(500, f"Failed to enqueue job: {e}")

    return {"job_id": job_id, "status": "queued"}

@router.get("/jobs/{job_id}")
def job_status(job_id: str, user=Depends(require_user)):
    it = ddb_get_job(user["email"], job_id)
    if not it:
        raise HTTPException(404, "Not found")
    out = {k: list(v.values())[0] for k, v in it.items()}
    # coerce numeric
    if "parts" in out:
        out["parts"] = int(out["parts"])
    if "parts_done" in out:
        out["parts_done"] = int(out["parts_done"])
    if "updated_at" in out:
        out["updated_at"] = int(out["updated_at"])
    return out

def _do_split(
    video_id: str,
    mode: str,
    parts: int,
    user_email: str,
    async_: bool,
    job_id: str | None = None,
):
    if job_id:
        ddb_update_job_status(user_email, job_id, "running")

    # get metadata to reconstruct input key/filename
    v = ddb_get_video(user_email, video_id)
    if not v:
        if job_id:
            ddb_update_job_status(user_email, job_id, "error")
        raise HTTPException(404, "Video not found")
    filename = v["filename"]["S"]
    s3_key_input = f"{user_email}/{video_id}/input/{filename}"

    tmpdir = tempfile.mkdtemp(prefix=f"split_{video_id}_")
    try:
        local_in = os.path.join(tmpdir, filename)
        s3_get_to_file(s3_key_input, local_in)

        # compute segment time
        duration = float(v["duration"]["N"])
        seg_len = max(1.0, duration / parts)

        # output pattern
        out_dir = os.path.join(tmpdir, "out")
        os.makedirs(out_dir, exist_ok=True)
        pattern = os.path.join(out_dir, "part_%02d.mp4")

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            local_in,
            "-f",
            "segment",
            "-segment_time",
            str(seg_len),
            "-reset_timestamps",
            "1",
        ]
        if mode == "fast":
            cmd += ["-c", "copy"]
            s3_prefix = f"{user_email}/{video_id}/segments_fast/"
        else:
            # HEAVY: re-encode (CPU-intensive)
            cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", "20", "-c:a", "aac", "-b:a", "128k"]
            s3_prefix = f"{user_email}/{video_id}/segments_heavy/"

        cmd += [pattern]

        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            if job_id:
                ddb_update_job_status(user_email, job_id, "error")
            raise HTTPException(500, f"ffmpeg failed: {p.stderr[-400:]}")

        # upload segments back to S3
        uploaded = []
        for name in sorted(os.listdir(out_dir)):
            if not name.endswith(".mp4"):
                continue
            key = s3_prefix + name
            s3_put_file(key, os.path.join(out_dir, name), content_type="video/mp4")
            uploaded.append(key)

        if job_id:
            ddb_update_job_status(user_email, job_id, "done", parts_done=len(uploaded))

        # return listing (sync) or nothing (async)
        groups = [
            {
                "mode": "heavy" if mode != "fast" else "fast",
                "parts": len(uploaded),
                "segments": [s3_presign_get(k) for k in uploaded],
            }
        ]
        return {"video_id": video_id, "outputs": groups}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

@router.get("/videos/{video_id}/segments")
def list_segments(video_id: str, user=Depends(require_user)):
    # We don’t list S3 by prefix here (kept simple). Instead we derive expected names
    v = ddb_get_video(user["email"], video_id)
    if not v:
        raise HTTPException(404, "Video not found")

    # Try both fast & heavy prefixes; generate pre-signed URLs for common names
    out = []
    for mode, prefix in [
        ("fast", f"{user['email']}/{video_id}/segments_fast/"),
        ("heavy", f"{user['email']}/{video_id}/segments_heavy/"),
    ]:
        # naive probe: try up to 100 parts and let client 403 if missing
        urls = []
        for i in range(100):
            key = f"{prefix}part_{i:02d}.mp4"
            try:
                urls.append(s3_presign_get(key))
            except Exception:
                break
        if urls:
            out.append({"mode": mode, "parts": len(urls), "segments": urls})
    return {"video_id": video_id, "outputs": out}
