from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.models.auth import User
from src.services.auth_service import decode_token
from src.services.request_context import current_user_id

# AUTH A: Read Authorization: Bearer <token>. This helper extracts the token; decode_token verifies the JWT.
security = HTTPBearer()


# AUTH B: Depends(security) supplies credentials. Return a verified User for endpoints using Depends(get_current_user).
async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> User:
    """Validate the extracted JWT, bind its user ID to this request context, and return the authenticated User."""
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authorization header")
    # credentials is the helper's object; credentials.credentials is the actual JWT string.
    user = decode_token(credentials.credentials)
    # Bind identity to this async request's context for helper code; do not use a shared global user variable.
    current_user_id.set(user.user_id)
    return user
