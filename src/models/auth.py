from pydantic import BaseModel


# Pydantic object constructed from verified JWT data. React's displayed user object is not trusted as identity.
class User(BaseModel):
    """Authenticated user representation."""

    user_id: str
    name: str
    role: str  # "analyst", "finance", "hr", "c_level", "admin"
    department: str = ""
