"""Content-free operational logs; detailed query history is encrypted separately."""
import json
import logging
from datetime import datetime, timezone


logger = logging.getLogger("vault.retrieval")
FIELDS = {"trace_id", "model", "mode", "fallback_reason", "vector_status", "descriptor_count", "lexical_count", "vector_count", "candidate_count", "model_calls", "tool_calls", "result_count", "weak_count", "elapsed_ms"}


def configure():
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)
    logger.propagate = False


def emit(value):
    safe = {key: value[key] for key in FIELDS if key in value}
    safe.update(event="query", time=datetime.now(timezone.utc).isoformat())
    logger.info(json.dumps(safe, ensure_ascii=False))
