from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    """A free liveness check; never invokes a model."""
    return {"status": "ok"}
