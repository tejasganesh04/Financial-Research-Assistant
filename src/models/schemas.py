from typing import Literal

from pydantic import BaseModel, Field, field_validator

from src.config.settings import settings


class RouterDecision(BaseModel):
    """Model-produced routing decision: query intent and simple_lookup/research_required complexity."""

    intent: Literal["retrieval", "clarification", "out_of_scope"]
    reason: str = Field(description="Brief reason for the classification")
    complexity: Literal["simple_lookup", "research_required"] = Field(
        default="simple_lookup",
        description="Query complexity tier — drives whether to use the research agent.",
    )


class GradeResult(BaseModel):
    """Structured output for chunk relevance grading."""

    relevant: bool
    reason: str = Field(description="Why the chunk is or isn't relevant")


class HallucinationCheck(BaseModel):
    """Structured output for hallucination checking."""

    grounded: bool
    score: float = Field(ge=0.0, le=1.0, description="Confidence that answer is grounded in sources")
    explanation: str = Field(description="Explanation of grounding assessment")


class InjectionCheck(BaseModel):
    """Structured output for prompt injection detection."""

    is_injection: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class MetadataExtraction(BaseModel):
    """Meaning-based search restrictions; empty lists leave that dimension unfiltered."""

    companies: list[str] = Field(default_factory=list)
    fiscal_years: list[int] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=500)


# REQUEST BODY: FastAPI validates incoming JSON. The path-based endpoint copies its thread ID into this object.
class ChatRequest(BaseModel):
    """Request body for the chat endpoint."""

    message: str = Field(max_length=settings.MAX_MESSAGE_CHARACTERS)
    thread_id: str | None = None

    @field_validator("message")
    @classmethod
    def require_nonempty_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message must not be empty or whitespace-only.")
        return value


# JSON RESPONSE: Used by ordinary question endpoints. SSE event dictionaries are built separately in chat_stream.
class ChatResponse(BaseModel):
    """Response body for the chat endpoint."""

    response: str
    sources: list[dict] = []
    confidence: float | None = None
    requires_approval: bool = False
    thread_id: str | None = None
    request_id: str | None = None
    sanitized_message: str | None = None
    access_scope: dict | None = None
    cost_usd: float | None = None
    tokens: dict | None = None


# LOGIN BODY: Both username and password are required strings; malformed input is rejected before login runs.
class LoginRequest(BaseModel):
    """Request body for login."""

    username: str
    password: str


# LOGIN RESPONSE: Return the signed token and demo identity. The browser stores the token in sessionStorage.
class TokenResponse(BaseModel):
    """Login response containing the signed JWT and demo identity. React then calls /auth/me to load permissions."""

    access_token: str
    token_type: str = "bearer"
    user_id: str
    name: str
    role: str
    department: str


# IDENTITY RESPONSE: Exposes the document-type/confidentiality rules and the explicit-escalation approval policy.
class UserPermissions(BaseModel):
    """Role-derived RBAC permissions surfaced on `/auth/me`."""

    allowed_doc_types: list[str]
    allowed_confidentiality: list[str]
    approval_policy: str = "Explicit access escalations require Admin approval"


# GET /auth/me returns this identity/permissions object after the JWT dependency succeeds.
class UserMeResponse(BaseModel):
    """`GET /auth/me` response. Validates the JWT is still good and lets
    the frontend re-fetch user state after a refresh.
    """

    user_id: str
    name: str
    role: str
    department: str
    permissions: UserPermissions
