# app/auth.py
import os
from fastapi import Header, HTTPException
from jose import jwt, JWTError
from dotenv import load_dotenv

load_dotenv()  # read .env for local dev

ALGO = "HS256"
SECRET = os.getenv("JWT_SECRET", "dev-only-change-me")

def _decode_bearer(authorization: str) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1]
    try:
        return jwt.decode(token, SECRET, algorithms=[ALGO])
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")

# Used by routes_videos.py (FastAPI dependency or manual call)
def require_user(authorization: str = Header(default="")) -> dict:
    claims = _decode_bearer(authorization)
    return {
        "username": claims.get("sub"),
        "role": claims.get("role"),
        "email": claims.get("email"),
    }
