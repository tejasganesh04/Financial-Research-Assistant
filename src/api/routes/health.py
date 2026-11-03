from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    """A free liveness check; never invokes a model."""
    return {"status": "ok"}
