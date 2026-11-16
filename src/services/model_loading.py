"""Serialize first-time loading of local models.

Hugging Face downloads share a progress-bar lock that breaks when two threads
load models at once, so concurrent first requests after a restart failed.
"""

import threading

MODEL_LOAD_LOCK = threading.Lock()


def warm_local_models() -> None:
    """Load the question-screening models now, so the first question after a start does not wait.

    The first start on a new server downloads them (a few hundred MB); later starts read the cache.
    """
    import logging
    import time

    from src.services import guardrails_service

    logger = logging.getLogger(__name__)
    started = time.monotonic()
    guardrails_service._get_presidio_engines()
    guardrails_service._get_injection_scanner()
    logger.info("Question-screening models ready in %.1f s", time.monotonic() - started)
