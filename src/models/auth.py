from pydantic import BaseModel


# Built from verified JWT claims, never from client-supplied user data.
class User(BaseModel):
    """Authenticated user representation."""

    user_id: str
    name: str
    role: str  # "analyst", "finance", "hr", "c_level", "admin"
    department: str = ""
    # The sign-in session the access token belongs to (absent in tokens made directly in tests).
    session_id: str | None = None
