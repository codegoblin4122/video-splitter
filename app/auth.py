# app/auth.py
from fastapi import Header, HTTPException
from jose import jwt, JWTError
from .config import JWT_SECRET

# Minimal dependency used by routes_videos.py
def require_user(authorization: str | None = Header(default=None)):
    """
    Accepts either:
    - Bearer <JWT> signed with HS256 using JWT_SECRET, containing 'email' or 'sub'
    - No header at all (dev mode): returns a default user for testing
    """
    if not authorization:
        # Dev fallback so you can test without tokens
        return {"email": "devuser@example.com"}

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid Authorization header")

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    email = payload.get("email") or payload.get("sub")
    if not email:
        raise HTTPException(status_code=401, detail="Token missing email/sub")

    return {"email": email}
