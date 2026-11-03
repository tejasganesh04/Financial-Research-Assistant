from fastapi import APIRouter, Depends, HTTPException, status

from src.api.dependencies import get_current_user
from src.config.rbac_config import get_permissions
from src.models.auth import User
from src.models.schemas import (
    LoginRequest,
    TokenResponse,
    UserMeResponse,
    UserPermissions,
)
from src.services.auth_service import create_token

# main.py adds /v1 to this router's /auth prefix: the login URL is POST /v1/auth/login.
router = APIRouter(prefix="/auth", tags=["auth"])

# Simple in-memory users for development. Replace with a real user store in production.
# Fixed demo accounts live in this dictionary, not a user table in PostgreSQL.
DEV_USERS = {
    "analyst": {"password": "analyst123", "name": "Test Analyst", "role": "analyst", "department": "Research"},
    "finance": {"password": "finance123", "name": "Test Finance", "role": "finance", "department": "FP&A"},
    "hr": {"password": "hr123", "name": "Test HR", "role": "hr", "department": "Human Resources"},
    "clevel": {"password": "clevel123", "name": "Test CEO", "role": "c_level", "department": "Executive"},
    "admin": {"password": "admin123", "name": "Test Admin", "role": "admin", "department": "IT"},
}


# LOGIN: FastAPI validates JSON as LoginRequest; this endpoint checks the account and returns TokenResponse.
@router.post("/login", response_model=TokenResponse)
async def login(request: LoginRequest):
    # Look up the submitted username and reject unknown accounts or wrong passwords before creating a token.
    user = DEV_USERS.get(request.username)
    if not user or user["password"] != request.password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    # Sign identity and role with the backend secret; the browser receives the token, never that secret.
    token = create_token(
        user_id=request.username,
        name=user["name"],
        role=user["role"],
        department=user["department"],
    )
    return TokenResponse(
        access_token=token,
        user_id=request.username,
        name=user["name"],
        role=user["role"],
        department=user["department"],
    )


# WHO AM I: Validate the Bearer token through the dependency, then return identity and configured permissions.
@router.get("/me", response_model=UserMeResponse)
async def me(user: User = Depends(get_current_user)) -> UserMeResponse:
    """Return the logged-in identity and fixed role permissions."""
    perms = get_permissions(user.role)
    return UserMeResponse(
        user_id=user.user_id,
        name=user.name,
        role=user.role,
        department=user.department,
        permissions=UserPermissions(
            allowed_doc_types=perms["allowed_doc_types"],
            allowed_confidentiality=perms["allowed_confidentiality"],
        ),
    )
