"""Concurrent first requests must not load local models at the same time."""

import sys
import threading
import time
from types import SimpleNamespace

from src.services import reranker_service, vector_store


def test_concurrent_first_loads_run_one_at_a_time_and_load_once(monkeypatch):
    active, peak, loads = 0, 0, []
    guard = threading.Lock()

    def slow_model(kind):
        def build(*args, **kwargs):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with guard:
                active -= 1
                loads.append(kind)
            return kind

        return build

    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(SparseTextEmbedding=slow_model("bm25")))
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(CrossEncoder=slow_model("reranker")))
    vector_store._load_sparse_embedder.cache_clear()
    reranker_service._load_reranker.cache_clear()
    try:
        callers = [vector_store.get_sparse_embedder, reranker_service.get_reranker] * 4
        threads = [threading.Thread(target=call) for call in callers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        vector_store._load_sparse_embedder.cache_clear()
        reranker_service._load_reranker.cache_clear()
    assert peak == 1
    assert sorted(loads) == ["bm25", "reranker"]
