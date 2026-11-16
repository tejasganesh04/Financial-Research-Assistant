from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from src.api.dependencies import get_current_user
from src.config.rbac_config import get_permissions
from src.config.settings import settings
from src.models.auth import User
from src.models.schemas import (
    LoginRequest,
    McpTokenRequest,
    McpTokenResponse,
    TokenResponse,
    UserMeResponse,
    UserPermissions,
)
from src.services.auth_service import create_token

router = APIRouter(prefix="/auth", tags=["auth"])

# Fixed demo accounts. Replace with a real user store in production.
DEV_USERS = {
    "analyst": {"password": "analyst123", "name": "Test Analyst", "role": "analyst", "department": "Research"},
    "finance": {"password": "finance123", "name": "Test Finance", "role": "finance", "department": "FP&A"},
    "hr": {"password": "hr123", "name": "Test HR", "role": "hr", "department": "Human Resources"},
    "clevel": {"password": "clevel123", "name": "Test CEO", "role": "c_level", "department": "Executive"},
    "admin": {"password": "admin123", "name": "Test Admin", "role": "admin", "department": "IT"},
}


@router.post("/login", response_model=TokenResponse)
async def login(request: LoginRequest):
    user = DEV_USERS.get(request.username)
    if not user or user["password"] != request.password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

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


@router.post("/mcp-token", response_model=McpTokenResponse)
async def create_mcp_access_token(
    request: McpTokenRequest | None = None, user: User = Depends(get_current_user)
) -> McpTokenResponse:
    """Issue a search-only token for MCP clients, with the caller's own role.

    It needs an ordinary login; an MCP token cannot create another token or use the API.
    """
    if not settings.MCP_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The MCP server is disabled.")
    from src.mcp_server.auth import SCOPE, create_mcp_token

    try:
        token, expires, token_id = create_mcp_token(
            user.user_id, user.name, user.role, user.department, days=request.days if request else None
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    return McpTokenResponse(
        access_token=token,
        token_id=token_id,
        expires_at=datetime.fromtimestamp(expires, timezone.utc).isoformat(),
        role=user.role,
        scope=SCOPE,
        mcp_url=f"{settings.MCP_PUBLIC_URL.rstrip('/')}/mcp",
    )
