import os
from .ssm_params import get_param

# Base identifiers
QUT_USERNAME = os.getenv("QUT_USERNAME", "n10254854@qut.edu.au")
AWS_REGION   = os.getenv("AWS_REGION", "ap-southeast-2")

# Allow pure-local/dev via ENV_ONLY=1
ENV_ONLY = os.getenv("ENV_ONLY", "0") == "1"

# SSM parameter base path
SSM_BASE = f"/{QUT_USERNAME.split('@')[0]}/video-splitter/prod" 

def _ssm_or_env(env_key: str, default: str, ssm_name: str | None = None) -> str:
    """
    Order of precedence:
      1) Explicit env var (e.g. DDB_VIDEOS_TABLE)
      2) SSM Parameter (if ENV_ONLY != 1)
      3) Default fallback
    """
    val = os.getenv(env_key)
    if val:
        return val
    if not ENV_ONLY and ssm_name:
        try:
            return get_param(ssm_name, ttl_seconds=int(os.getenv("SSM_TTL_SECONDS", "300")))
        except Exception:
            # Fall back to default if SSM not reachable or param missing
            pass
    return default

# Concrete parameters
S3_BUCKET        = _ssm_or_env("S3_BUCKET",        "n10254854-video-splitter", f"{SSM_BASE}/s3/bucket")
DDB_VIDEOS_TABLE = _ssm_or_env("DDB_VIDEOS_TABLE", "n10254854-videos",         f"{SSM_BASE}/ddb/videos_table")
DDB_JOBS_TABLE   = _ssm_or_env("DDB_JOBS_TABLE",   "n10254854-jobs",           f"{SSM_BASE}/ddb/jobs_table")
JWT_SECRET       = _ssm_or_env("JWT_SECRET",       "dev-secret",               f"{SSM_BASE}/auth/jwt_secret")  # move to Secrets Manager later
API_BASE_URL     = _ssm_or_env("API_BASE_URL",     "http://localhost:8000",    f"{SSM_BASE}/api/base_url")

def validate_cloud_env():
    missing = [k for k,v in {
        "S3_BUCKET": S3_BUCKET,
        "DDB_VIDEOS_TABLE": DDB_VIDEOS_TABLE,
        "DDB_JOBS_TABLE": DDB_JOBS_TABLE,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"Missing config: {', '.join(missing)}")
