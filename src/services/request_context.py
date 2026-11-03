"""Request-local authenticated user ID. The optional model gateway uses it for attribution; direct-provider calls skip it."""
from contextvars import ContextVar

current_user_id: ContextVar[str | None] = ContextVar("current_user_id", default=None)
