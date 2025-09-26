# app/ssm_params.py
import os, time, boto3

AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-2")
_ssm = boto3.client("ssm", region_name=AWS_REGION)
_cache = {}

def _get_cached(k):
    v = _cache.get(k)
    if not v: return None
    val, exp = v
    if exp and exp < time.time():
        _cache.pop(k, None)
        return None
    return val

def _set_cached(k, val, ttl=300):
    _cache[k] = (val, time.time()+ttl if ttl else None)

def get_param(name: str, ttl_seconds: int = 300) -> str:
    # allow local override via env (replace slashes with __)
    env_key = name.strip("/").replace("/", "__").upper()
    if env_key in os.environ:
        return os.environ[env_key]

    ck = f"param:{name}"
    cv = _get_cached(ck)
    if cv is not None:
        return cv

    resp = _ssm.get_parameter(Name=name)  # plain String, no decryption needed
    val = resp["Parameter"]["Value"]
    _set_cached(ck, val, ttl_seconds)
    return val
