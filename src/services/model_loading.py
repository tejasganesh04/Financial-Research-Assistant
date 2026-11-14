"""Serialize first-time loading of local models.

Hugging Face downloads share a progress-bar lock that breaks when two threads
load models at once, so concurrent first requests after a restart failed.
"""

import threading

MODEL_LOAD_LOCK = threading.Lock()
