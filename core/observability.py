"""P1-T4 — Observability infrastructure for failure handling and pipeline monitoring.

Provides structured logging for pipeline stages, failure taxonomy, and privacy-safe metadata.
"""

import time
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


# --- Failure Taxonomy ---

class FailureType(str, Enum):
    """Explicit failure categories for the pipeline."""
    PDF_ERROR = "pdf_error"
    LLM_TIMEOUT = "llm_timeout"
    LLM_NETWORK_ERROR = "llm_network_error"
    LLM_AUTH_ERROR = "llm_auth_error"
    LLM_RATE_LIMIT = "llm_rate_limit"
    LLM_SERVER_ERROR = "llm_server_error"
    LLM_PARSE_ERROR = "llm_parse_error"
    SCHEMA_VALIDATION_ERROR = "schema_validation_error"
    REFERENCE_UNAVAILABLE = "reference_unavailable"
    MEDLINEPLUS_NO_MATCH = "medlineplus_no_match"
    MEDLINEPLUS_TIMEOUT = "medlineplus_timeout"
    MEDLINEPLUS_NETWORK_ERROR = "medlineplus_network_error"
    MEDLINEPLUS_INVALID_RESPONSE = "medlineplus_invalid_response"
    VERIFICATION_FAILURE = "verification_failure"
    EXTRACTION_FAILURE = "extraction_failure"
    RISK_CLASSIFICATION_FAILURE = "risk_classification_failure"
    EXPLANATION_FAILURE = "explanation_failure"
    UNKNOWN_FAILURE = "unknown_failure"


class PipelineStage(str, Enum):
    """Major pipeline stages for observability."""
    PDF_EXTRACTION = "pdf_extraction"
    EXTRACTION = "extraction"
    REFERENCE_LOOKUP = "reference_lookup"
    RISK_FLAGGING = "risk_flagging"
    EXPLANATION = "explanation"
    VERIFICATION = "verification"
    MEDLINEPLUS_GROUNDING = "medlineplus_grounding"


# --- Structured Event ---

@dataclass
class PipelineEvent:
    """Structured observability event for a pipeline stage."""
    stage: PipelineStage
    status: str  # "success", "failed", "skipped"
    failure_type: Optional[FailureType] = None
    retry_count: int = 0
    latency_ms: Optional[float] = None
    reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/serialization."""
        result = {
            "stage": self.stage.value,
            "status": self.status,
            "retry_count": self.retry_count,
        }
        if self.failure_type:
            result["failure_type"] = self.failure_type.value
        if self.latency_ms is not None:
            result["latency_ms"] = round(self.latency_ms, 2)
        if self.reason:
            result["reason"] = self.reason
        if self.metadata:
            result["metadata"] = self.metadata
        return result


# --- Pipeline Logger ---

class PipelineLogger:
    """Structured logger for pipeline events with privacy protection."""

    def __init__(self, enable_console: bool = False):
        self.events: List[PipelineEvent] = []
        self.enable_console = enable_console
        self.logger = logging.getLogger("medreportai.pipeline")
        if enable_console and not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s"
            ))
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)

    def log_event(self, event: PipelineEvent):
        """Record a pipeline event."""
        self.events.append(event)
        if self.enable_console:
            self.logger.info(json.dumps(event.to_dict()))

    def log_stage_start(self, stage: PipelineStage) -> float:
        """Log stage start, return start time for latency calculation."""
        return time.monotonic()

    def log_stage_success(self, stage: PipelineStage, start_time: float,
                          metadata: Optional[Dict[str, Any]] = None):
        """Log successful stage completion."""
        latency_ms = (time.monotonic() - start_time) * 1000
        event = PipelineEvent(
            stage=stage,
            status="success",
            latency_ms=latency_ms,
            metadata=metadata or {},
        )
        self.log_event(event)

    def log_stage_failure(self, stage: PipelineStage, start_time: float,
                          failure_type: FailureType, reason: str,
                          retry_count: int = 0,
                          metadata: Optional[Dict[str, Any]] = None):
        """Log stage failure with controlled failure type."""
        latency_ms = (time.monotonic() - start_time) * 1000
        event = PipelineEvent(
            stage=stage,
            status="failed",
            failure_type=failure_type,
            retry_count=retry_count,
            latency_ms=latency_ms,
            reason=reason,
            metadata=metadata or {},
        )
        self.log_event(event)

    def get_events_for_stage(self, stage: PipelineStage) -> List[PipelineEvent]:
        """Get all events for a specific stage."""
        return [e for e in self.events if e.stage == stage]

    def get_failure_summary(self) -> Dict[str, int]:
        """Get count of failures by type."""
        summary = {}
        for event in self.events:
            if event.status == "failed" and event.failure_type:
                key = event.failure_type.value
                summary[key] = summary.get(key, 0) + 1
        return summary


# --- Privacy Protection ---

# Patterns that indicate sensitive data that must NOT be logged
SENSITIVE_PATTERNS = [
    r'\b\d{3}-\d{2}-\d{4}\b',  # SSN
    r'\b\d{10}\b',  # Phone numbers
    r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',  # Email
    r'patient.*name',  # Patient name references
    r'date.*birth',  # DOB references
    r'\bMRN\b',  # Medical Record Number
]

import re


def contains_sensitive_data(text: str) -> bool:
    """Check if text contains sensitive patient data patterns."""
    if not text:
        return False
    text_lower = text.lower()
    for pattern in SENSITIVE_PATTERNS:
        if re.search(pattern, text_lower):
            return True
    return False


def sanitize_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Remove or redact sensitive fields from metadata before logging."""
    sanitized = {}
    for key, value in metadata.items():
        if isinstance(value, str) and contains_sensitive_data(value):
            sanitized[key] = "[REDACTED]"
        elif isinstance(value, str) and len(value) > 1000:
            # Truncate very long strings that might contain report content
            sanitized[key] = value[:100] + "...[TRUNCATED]"
        else:
            sanitized[key] = value
    return sanitized


# --- Latency Tracker ---

class LatencyTracker:
    """Track latency for pipeline stages."""

    def __init__(self):
        self._start_times: Dict[str, float] = {}
        self._latencies: Dict[str, List[float]] = {}

    def start(self, stage: str):
        """Record start time for a stage."""
        self._start_times[stage] = time.monotonic()

    def end(self, stage: str) -> float:
        """Record end time, return latency in ms."""
        if stage not in self._start_times:
            return 0.0
        latency_ms = (time.monotonic() - self._start_times[stage]) * 1000
        if stage not in self._latencies:
            self._latencies[stage] = []
        self._latencies[stage].append(latency_ms)
        del self._start_times[stage]
        return latency_ms

    def get_stats(self, stage: str) -> Dict[str, float]:
        """Get latency statistics for a stage."""
        if stage not in self._latencies or not self._latencies[stage]:
            return {"count": 0, "min": 0.0, "max": 0.0, "avg": 0.0}
        latencies = self._latencies[stage]
        return {
            "count": len(latencies),
            "min": min(latencies),
            "max": max(latencies),
            "avg": sum(latencies) / len(latencies),
        }


# Import json at module level for the logger
import json
