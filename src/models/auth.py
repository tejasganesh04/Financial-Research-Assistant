from pydantic import BaseModel


# Built from verified JWT claims, never from client-supplied user data.
class User(BaseModel):
    """Authenticated user representation."""

    user_id: str
    name: str
    role: str  # "analyst", "finance", "hr", "c_level", "admin"
    department: str = ""
