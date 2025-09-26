# app/auth_cognito.py
import base64, hmac, hashlib, json, os, requests
from typing import Optional, Dict
from urllib.parse import quote
from fastapi import APIRouter, HTTPException, Body, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt
import boto3
from dotenv import load_dotenv

load_dotenv()
router = APIRouter(prefix="/auth", tags=["auth"])

REGION = os.getenv("COGNITO_REGION", "ap-southeast-2")
POOL_ID = os.getenv("COGNITO_POOL_ID") or os.getenv("COGNITO_USER_POOL_ID")
CLIENT_ID = os.getenv("COGNITO_CLIENT_ID")
CLIENT_SECRET = os.getenv("COGNITO_CLIENT_SECRET")
TOTP_ISSUER = os.getenv("COGNITO_TOTP_ISSUER", "CAB432-App")

if not all([POOL_ID, CLIENT_ID, CLIENT_SECRET]):
    raise RuntimeError("Missing COGNITO_* env vars")

ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"

_cidp = boto3.client("cognito-idp", region_name=REGION)
_jwks: Optional[Dict] = None

def _secret_hash(username: str) -> str:
    msg = (username + CLIENT_ID).encode("utf-8")
    key = CLIENT_SECRET.encode("utf-8")
    digest = hmac.new(key, msg, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()

def _get_jwks() -> Dict:
    global _jwks
    if _jwks is None:
        _jwks = requests.get(JWKS_URL, timeout=10).json()
    return _jwks

def verify_cognito_id_token(id_token: str) -> Dict:
    jwks = _get_jwks()
    try:
        claims = jwt.decode(
            id_token,
            jwks,                      
            algorithms=["RS256"],
            audience=CLIENT_ID,
            issuer=ISSUER,
            options={"verify_at_hash": False},
        )
        return claims
    except Exception as e:
        raise HTTPException(401, f"Invalid token: {e}")

# ---------- Public endpoints ----------

@router.post("/signup")
def signup(
    username: str = Body(...),
    password: str = Body(...),
    email: str = Body(...),
):
    try:
        _cidp.sign_up(
            ClientId=CLIENT_ID,
            Username=username,
            Password=password,
            SecretHash=_secret_hash(username),
            UserAttributes=[{"Name": "email", "Value": email}],
        )
        return {"message": "Signup initiated. Check your email for the code."}
    except _cidp.exceptions.UsernameExistsException:
        raise HTTPException(400, "Username already exists")
    except Exception as e:
        raise HTTPException(400, str(e))

@router.post("/confirm")
def confirm(username: str = Body(...), code: str = Body(...)):
    try:
        _cidp.confirm_sign_up(
            ClientId=CLIENT_ID,
            Username=username,
            ConfirmationCode=code,
            SecretHash=_secret_hash(username),
        )
        return {"message": "User confirmed"}
    except Exception as e:
        raise HTTPException(400, str(e))

@router.post("/login")
def login(username: str = Body(...), password: str = Body(...)):
    """
    Start login. Handles three cases:
    - Success (no MFA) -> returns tokens
    - MFA_SETUP        -> return {challenge, session}
    - SOFTWARE_TOKEN_MFA -> return {challenge, session}
    """
    try:
        resp = _cidp.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            ClientId=CLIENT_ID,
            AuthParameters={
                "USERNAME": username,
                "PASSWORD": password,
                "SECRET_HASH": _secret_hash(username),
            },
        )

        if "AuthenticationResult" in resp:
            id_token = resp["AuthenticationResult"]["IdToken"]
            claims = verify_cognito_id_token(id_token)
            return {
                "id_token": id_token,
                "access_token": resp["AuthenticationResult"]["AccessToken"],
                "refresh_token": resp["AuthenticationResult"].get("RefreshToken"),
                "email": claims.get("email"),
                "sub": claims.get("sub"),
            }

        # Challenge path
        challenge = resp.get("ChallengeName")
        session = resp.get("Session")
        if challenge in ("MFA_SETUP", "SOFTWARE_TOKEN_MFA", "SMS_MFA"):
            return {"challenge": challenge, "session": session}
        else:
            raise HTTPException(400, f"Unexpected challenge: {challenge}")

    except _cidp.exceptions.NotAuthorizedException:
        raise HTTPException(401, "Incorrect username or password")
    except _cidp.exceptions.UserNotConfirmedException:
        raise HTTPException(401, "User not confirmed")
    except Exception as e:
        raise HTTPException(400, str(e))

# ----- MFA Enrolment (first login → MFA_SETUP) -----

@router.post("/mfa/associate")
def mfa_associate(session: str = Body(...), username: str = Body(...)):
    """
    When /auth/login returned {challenge:'MFA_SETUP'}:
    call this to choose SOFTWARE_TOKEN (TOTP) and get the secret + otpauth URL.
    """
    try:
        assoc = _cidp.associate_software_token(Session=session)
        secret = assoc["SecretCode"]
        new_session = assoc["Session"]

        # Build otpauth:// URL for QR scanners
        label = f"{TOTP_ISSUER}:{username}"
        otpauth = (
            f"otpauth://totp/{quote(label)}"
            f"?secret={secret}&issuer={quote(TOTP_ISSUER)}&algorithm=SHA1&digits=6&period=30"
        )
        return {"secret_code": secret, "otpauth_url": otpauth, "session": new_session}
    except Exception as e:
        raise HTTPException(400, f"MFA associate failed: {e}")

@router.post("/mfa/verify_setup")
def mfa_verify_setup(username: str = Body(...), code: str = Body(...), session: str = Body(...)):
    """
    User enters the 6-digit TOTP from the authenticator app.
    We verify it and immediately complete MFA_SETUP to return tokens.
    """
    try:
        verify = _cidp.verify_software_token(Session=session, UserCode=code)
        if verify.get("Status") != "SUCCESS":
            raise HTTPException(400, "Invalid TOTP code")

        done = _cidp.respond_to_auth_challenge(
            ClientId=CLIENT_ID,
            ChallengeName="MFA_SETUP",
            Session=verify["Session"],
            ChallengeResponses={
                "USERNAME": username,
                "SECRET_HASH": _secret_hash(username),
            },
        )
        if "AuthenticationResult" not in done:
            raise HTTPException(400, "MFA setup did not return tokens")

        auth = done["AuthenticationResult"]
        claims = verify_cognito_id_token(auth["IdToken"])
        return {
            "id_token": auth["IdToken"],
            "access_token": auth["AccessToken"],
            "refresh_token": auth.get("RefreshToken"),
            "email": claims.get("email"),
            "sub": claims.get("sub"),
        }
    except Exception as e:
        raise HTTPException(400, f"MFA verify (setup) failed: {e}")

# --- Enrol TOTP for an already-logged-in user (AccessToken path) ---

@router.post("/mfa/associate_loggedin")
def mfa_associate_loggedin(access_token: str = Body(...)):
    """
    Start TOTP enrolment for a user who is ALREADY authenticated.
    Requires the Cognito AccessToken (NOT the ID token).
    """
    try:
        resp = _cidp.associate_software_token(AccessToken=access_token)
        # Returns { 'SecretCode': 'XXXXX' }
        secret = resp["SecretCode"]
        # Build otpauth URL for QR apps
        # We don't know the username here, so use a generic label.
        label = TOTP_ISSUER
        otpauth = (
            f"otpauth://totp/{quote(label)}"
            f"?secret={secret}&issuer={quote(TOTP_ISSUER)}&algorithm=SHA1&digits=6&period=30"
        )
        return {"secret_code": secret, "otpauth_url": otpauth}
    except Exception as e:
        raise HTTPException(400, f"MFA associate (logged-in) failed: {e}")

@router.post("/mfa/verify_loggedin")
def mfa_verify_loggedin(access_token: str = Body(...), code: str = Body(...)):
    """
    Finish TOTP enrolment for an already-authenticated user:
      1) Verify the 6-digit code against the shared secret
      2) Enable software token MFA and set it as preferred
    """
    try:
        verify = _cidp.verify_software_token(AccessToken=access_token, UserCode=code)
        if verify.get("Status") != "SUCCESS":
            raise HTTPException(400, "Invalid TOTP code")

        # Turn on software token MFA for this user
        _cidp.set_user_mfa_preference(
            SoftwareTokenMfaSettings={
                "Enabled": True,
                "PreferredMfa": True,
            },
            AccessToken=access_token,
        )
        return {"message": "TOTP enabled and set as preferred MFA"}
    except Exception as e:
        raise HTTPException(400, f"MFA verify (logged-in) failed: {e}")


# ----- MFA on subsequent logins (SOFTWARE_TOKEN_MFA) -----

@router.post("/mfa/verify_login")
def mfa_verify_login(username: str = Body(...), code: str = Body(...), session: str = Body(...)):
    """
    When /auth/login returned {challenge:'SOFTWARE_TOKEN_MFA'}:
    submit the 6-digit code to finish login.
    """
    try:
        res = _cidp.respond_to_auth_challenge(
            ClientId=CLIENT_ID,
            ChallengeName="SOFTWARE_TOKEN_MFA",
            Session=session,
            ChallengeResponses={
                "USERNAME": username,
                "SOFTWARE_TOKEN_MFA_CODE": code,
                "SECRET_HASH": _secret_hash(username),
            },
        )
        if "AuthenticationResult" not in res:
            raise HTTPException(400, "MFA verify (login) did not return tokens")

        auth = res["AuthenticationResult"]
        claims = verify_cognito_id_token(auth["IdToken"])
        return {
            "id_token": auth["IdToken"],
            "access_token": auth["AccessToken"],
            "refresh_token": auth.get("RefreshToken"),
            "email": claims.get("email"),
            "sub": claims.get("sub"),
        }
    except Exception as e:
        raise HTTPException(400, f"MFA verify (login) failed: {e}")

# --- Optional MFA: Enrol TOTP for an already-logged-in user (uses AccessToken) ---

@router.post("/mfa/associate_loggedin")
def mfa_associate_loggedin(access_token: str = Body(...), username: str | None = Body(None)):
    """
    Start TOTP enrolment for a user who is ALREADY logged in (MFA optional).
    Requires AccessToken from current session (Hosted UI or USER_PASSWORD_AUTH).
    """
    try:
        assoc = _cidp.associate_software_token(AccessToken=access_token)
        secret = assoc["SecretCode"]

        label = f"{TOTP_ISSUER}:{username or 'user'}"
        otpauth = (
            f"otpauth://totp/{quote(label)}"
            f"?secret={secret}&issuer={quote(TOTP_ISSUER)}&algorithm=SHA1&digits=6&period=30"
        )
        return {"secret_code": secret, "otpauth_url": otpauth}
    except Exception as e:
        raise HTTPException(400, f"MFA associate (logged-in) failed: {e}")

@router.post("/mfa/verify_enable")
def mfa_verify_enable(access_token: str = Body(...), code: str = Body(...)):
    """
    Verify the user's 6-digit TOTP and enable Software Token MFA as preferred.
    """
    try:
        v = _cidp.verify_software_token(AccessToken=access_token, UserCode=code)
        if v.get("Status") != "SUCCESS":
            raise HTTPException(400, "Invalid TOTP code")

        _cidp.set_user_mfa_preference(
            AccessToken=access_token,
            SoftwareTokenMfaSettings={"Enabled": True, "PreferredMfa": True},
        )
        return {"status": "ok", "message": "TOTP enabled for this user"}
    except Exception as e:
        raise HTTPException(400, f"MFA verify/enable failed: {e}")

# ---------- Bearer dependency (unchanged) ----------

bearer = HTTPBearer(auto_error=False)

def require_cognito_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer)
) -> dict:
    if not creds or not creds.scheme.lower() == "bearer":
        raise HTTPException(401, "Missing Authorization: Bearer <token>")
    token = creds.credentials
    claims = verify_cognito_id_token(token)
    email = claims.get("email") or claims.get("cognito:username") or claims.get("sub")
    if not email:
        raise HTTPException(401, "Token missing identity (email/sub)")
    return {"email": email, "claims": claims}
