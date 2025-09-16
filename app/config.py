import os

AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-2")
S3_BUCKET = os.getenv("S3_BUCKET", "")
DDB_VIDEOS_TABLE = os.getenv("DDB_VIDEOS_TABLE", "")
DDB_JOBS_TABLE = os.getenv("DDB_JOBS_TABLE", "")
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret")


def validate_cloud_env():
    missing = [k for k,v in {
        "S3_BUCKET": S3_BUCKET,
        "DDB_VIDEOS_TABLE": DDB_VIDEOS_TABLE,
        "DDB_JOBS_TABLE": DDB_JOBS_TABLE,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")
import os

AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-2")
S3_BUCKET = os.getenv("S3_BUCKET")
DDB_VIDEOS_TABLE = os.getenv("DDB_VIDEOS_TABLE")
DDB_JOBS_TABLE = os.getenv("DDB_JOBS_TABLE")
