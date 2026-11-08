"""
Sentinel Cloud Vision - User Application 
"""

import os
from datetime import datetime, timedelta
from typing import Optional
from enum import Enum

from fastapi import FastAPI, HTTPException, Depends, Header, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST
from fastapi import UploadFile, File
import jwt
from passlib.context import CryptContext
import sys
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Only import user-facing modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from edge.preprocess import run_pipeline

# ============================================================================
# CONFIGURATION
# ============================================================================
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()

app = FastAPI(
    title="Sentinel Cloud Vision - User App",
    description="User-facing application (separate domain from admin)"
)

# ============================================================================
# CORS - User domain only
# ============================================================================
ALLOWED_ORIGINS = [
    "http://localhost:3000",
    os.getenv("USER_FRONTEND_URL", "http://localhost:3000"),
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

# ============================================================================
# MODELS
# ============================================================================

class RoleEnum(str, Enum):
    USER = "user"
    ADMIN = "admin"

class User(BaseModel):
    username: str
    password: str
    email: Optional[str] = None

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int

class TokenPayload(BaseModel):
    sub: str
    role: RoleEnum
    exp: datetime
    iat: datetime

# ============================================================================
# DATABASE - Connected to shared auth service
# ============================================================================
class AuthServiceClient:
    """Client to authenticate against shared auth service"""

    def __init__(self, auth_service_url: str = "http://localhost:8001"):
        self.auth_service_url = auth_service_url
        self.users = {}  # Local cache of authenticated users
        self.tokens = {}  # Local token cache

auth_client = AuthServiceClient()

# ============================================================================
# PROMETHEUS METRICS
# ============================================================================
USER_ACTIONS = Counter("user_actions_total", "Total user actions", ["action", "status"])

# ============================================================================
# TOKEN UTILITIES
# ============================================================================

def decode_token(token: str) -> Optional[TokenPayload]:
    """Decode and validate JWT token"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        role = payload.get("role")

        if not all([username, role]):
            return None

        return TokenPayload(
            sub=username,
            role=RoleEnum(role),
            exp=datetime.fromtimestamp(payload.get("exp")),
            iat=datetime.fromtimestamp(payload.get("iat"))
        )
    except jwt.ExpiredSignatureError:
        logger.warning("Token expired")
        return None
    except jwt.InvalidTokenError:
        logger.warning("Invalid token")
        return None

# ============================================================================
# DEPENDENCY INJECTION - AUTHORIZATION
# ============================================================================

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> TokenPayload:
    """Extract and validate JWT from Authorization header"""
    token = credentials.credentials

    payload = decode_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return payload

# ============================================================================
# PUBLIC ENDPOINTS (NO AUTH REQUIRED)
# ============================================================================

@app.get("/")
async def root():
    """Health check"""
    return {
        "app": "User Application",
        "domain": "app.example.com",
        "status": "running",
        "endpoints": {
            "public": ["/health", "/auth/signup", "/auth/login"],
            "user": ["/preprocess"]
        },
        "note": "Admin endpoints are NOT available on this domain. Use admin.example.com"
    }

@app.get("/health")
async def health_check():
    """Health check for load balancers"""
    return {"status": "healthy", "app": "user", "timestamp": datetime.utcnow().isoformat()}

# ============================================================================
# AUTHENTICATION ENDPOINTS
# ============================================================================

@app.post("/auth/signup", response_model=TokenResponse)
async def signup(user: User):
    """
    User registration
    Note: Admin creation is ONLY possible on admin domain
    """
    if not user.username or len(user.username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")

    if not user.password or len(user.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    # In production: Call shared auth service
    # For now: Create local user with USER role only
    password_hash = pwd_context.hash(user.password)

    if user.username in auth_client.users:
        raise HTTPException(status_code=400, detail="Username already exists")

    auth_client.users[user.username] = {
        "password_hash": password_hash,
        "role": RoleEnum.USER,  # ◄── Always USER on user domain
        "email": user.email or "",
        "created_at": datetime.utcnow()
    }

    # Create token with USER role only
    now = datetime.utcnow()
    expires = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    payload = {
        "sub": user.username,
        "role": RoleEnum.USER.value,
        "exp": expires,
        "iat": now
    }

    access_token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    auth_client.tokens[access_token] = user.username

    logger.info(f"User registered on user app: {user.username}")

    return {
        "access_token": access_token,
        "expires_in": int(ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    }

@app.post("/auth/login", response_model=TokenResponse)
async def login(user: User):
    """
    User login
    Returns token with USER role (never ADMIN)
    """
    stored_user = auth_client.users.get(user.username)

    if not stored_user or not pwd_context.verify(user.password, stored_user["password_hash"]):
        logger.warning(f"Failed login attempt: {user.username}")
        raise HTTPException(status_code=401, detail="Invalid username or password")

    # Always return USER role from user domain
    # If user is admin, they need to use admin domain
    role = RoleEnum.USER

    now = datetime.utcnow()
    expires = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    payload = {
        "sub": user.username,
        "role": role.value,
        "exp": expires,
        "iat": now
    }

    access_token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    auth_client.tokens[access_token] = user.username

    logger.info(f"User logged in on user app: {user.username}")

    return {
        "access_token": access_token,
        "expires_in": int(ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    }

# ============================================================================
# USER ENDPOINTS (AUTH REQUIRED)
# ============================================================================

@app.post("/preprocess")
async def preprocess_image(
    file: UploadFile = File(...),
    current_user: TokenPayload = Depends(get_current_user)
):
    """Image preprocessing - authenticated users only"""
    logger.info(f"Preprocessing requested by {current_user.sub}")
    USER_ACTIONS.labels(action="preprocess", status="started").inc()

    try:
        image_bytes = await file.read()
        result = run_pipeline(image_bytes)
        if result["status"] == "error":
            raise HTTPException(status_code=400, detail=result["message"])
        USER_ACTIONS.labels(action="preprocess", status="success").inc()
        return result
    except Exception as e:
        USER_ACTIONS.labels(action="preprocess", status="error").inc()
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================================
# NO ADMIN ENDPOINTS ON USER DOMAIN
# ============================================================================
# Notice: There are NO admin endpoints here
# Admin endpoints are ONLY on admin domain (admin.example.com)
# This ensures complete architectural separation

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
