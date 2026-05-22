"""Shared compaction lifecycle helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from uuid import uuid4

SAFE_FLUSH_OUTPUT_COVERAGE_STATUSES: Final[frozenset[str]] = frozenset({"ok", "unverifiable"})
SAFE_FLUSH_OBLIGATION_STATUSES: Final[frozenset[str]] = frozenset(
    {"ok", "backfilled", "unverifiable"}
)
COMPACTION_TRIGGERED_EVENT: Final[str] = "compaction.triggered"
COMPACTION_CHUNK_SUMMARIZED_EVENT: Final[str] = "compaction.chunk_summarized"
COMPACTION_SUMMARY_VERIFIED_EVENT: Final[str] = "compaction.summary_verified"
COMPACTION_PERSISTED_EVENT: Final[str] = "compaction.persisted"
COMPACTION_REPLAYED_EVENT: Final[str] = "compaction.replayed"
COMPACTION_COVERAGE_UNKNOWN: Final[str] = "unknown"


@dataclass(frozen=True)
class CompactionLifecycleResult:
    compacted: bool
    refused: bool
    reason: str | None = None
    tokens_before: int | None = None
    tokens_after: int | None = None
    remaining_budget_tokens: int | None = None
    removed_count: int = 0
    kept_count: int = 0
    summary_len: int = 0
    summary_source: str = "unknown"
    flush_receipt: Any = None


def new_compaction_id() -> str:
    """Return an opaque id used to correlate one compaction attempt's events."""

    return f"cmp_{uuid4().hex}"


def compaction_event_chain(event: str) -> list[str]:
    """Return the lifecycle events completed by the given telemetry event."""

    if event == COMPACTION_REPLAYED_EVENT:
        return [
            COMPACTION_TRIGGERED_EVENT,
            COMPACTION_CHUNK_SUMMARIZED_EVENT,
            COMPACTION_SUMMARY_VERIFIED_EVENT,
            COMPACTION_PERSISTED_EVENT,
            COMPACTION_REPLAYED_EVENT,
        ]
    if event == COMPACTION_PERSISTED_EVENT:
        return [
            COMPACTION_TRIGGERED_EVENT,
            COMPACTION_CHUNK_SUMMARIZED_EVENT,
            COMPACTION_SUMMARY_VERIFIED_EVENT,
            COMPACTION_PERSISTED_EVENT,
        ]
    if event == COMPACTION_SUMMARY_VERIFIED_EVENT:
        return [
            COMPACTION_TRIGGERED_EVENT,
            COMPACTION_CHUNK_SUMMARIZED_EVENT,
            COMPACTION_SUMMARY_VERIFIED_EVENT,
        ]
    if event == COMPACTION_CHUNK_SUMMARIZED_EVENT:
        return [COMPACTION_TRIGGERED_EVENT, COMPACTION_CHUNK_SUMMARIZED_EVENT]
    return [COMPACTION_TRIGGERED_EVENT]


def compaction_lifecycle_payload(compaction_id: str, event: str) -> dict[str, Any]:
    payload = {
        "compaction_id": compaction_id,
        "event": event,
        "event_chain": compaction_event_chain(event),
    }
    if event not in {COMPACTION_PERSISTED_EVENT, COMPACTION_REPLAYED_EVENT}:
        payload["coverage_status"] = COMPACTION_COVERAGE_UNKNOWN
    return payload


def compaction_result_payload(
    result: Any,
    *,
    tokens_before: int | None = None,
    tokens_after: int | None = None,
    remaining_budget_tokens: int | None = None,
) -> dict[str, Any]:
    kept_entries = getattr(result, "kept_entries", None) or []
    payload: dict[str, Any] = {
        "removed_count": int(getattr(result, "removed_count", 0) or 0),
        "kept_count": len(kept_entries),
        "chunk_count": int(getattr(result, "chunks_processed", 0) or 0),
        "summary_len": len(str(getattr(result, "summary", "") or "")),
        "summary_source": str(getattr(result, "summary_source", "unknown") or "unknown"),
        "coverage_status": str(getattr(result, "coverage_status", "unknown") or "unknown"),
        "missing_obligation_count": len(getattr(result, "missing_obligations", None) or []),
        "critical_carry_forward_count": len(getattr(result, "critical_carry_forward", None) or []),
        "state_kind": str(getattr(result, "summary_format", "text") or "text"),
    }
    if tokens_before is None:
        tokens_before = getattr(result, "tokens_before", None)
    if tokens_after is None:
        tokens_after = getattr(result, "tokens_after", None)
    if remaining_budget_tokens is None:
        remaining_budget_tokens = getattr(result, "remaining_budget_tokens", None)
    if tokens_before is not None:
        payload["tokens_before"] = int(tokens_before)
    if tokens_after is not None:
        payload["tokens_after"] = int(tokens_after)
    if remaining_budget_tokens is not None:
        payload["remaining_budget_tokens"] = int(remaining_budget_tokens)
    return payload


def flush_receipt_status(receipt: Any) -> str:
    if receipt is None:
        return "not_requested"
    return "safe" if flush_receipt_allows_destructive_compaction(receipt) else "unsafe"


def _receipt_value(receipt: Any, name: str, default: Any) -> Any:
    if isinstance(receipt, Mapping):
        return receipt.get(name, default)
    return getattr(receipt, name, default)


def _receipt_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def flush_receipt_allows_destructive_compaction(receipt: Any) -> bool:
    if _receipt_value(receipt, "mode", None) != "llm":
        return False
    if _receipt_int(_receipt_value(receipt, "indexed_chunk_count", 0)) <= 0:
        return False
    integrity_status = str(
        _receipt_value(receipt, "integrity_status", "unverified") or "unverified"
    )
    if integrity_status != "ok":
        return False
    output_coverage_status = str(
        _receipt_value(receipt, "output_coverage_status", "unverified") or "unverified"
    )
    if output_coverage_status not in SAFE_FLUSH_OUTPUT_COVERAGE_STATUSES:
        return False
    if _receipt_int(_receipt_value(receipt, "invalid_candidate_count", 0)) > 0:
        return False
    if _receipt_value(receipt, "candidate_missing_ids", []):
        return False
    obligation_status = str(
        _receipt_value(receipt, "obligation_status", "unverified") or "unverified"
    )
    if obligation_status not in SAFE_FLUSH_OBLIGATION_STATUSES:
        return False
    return not _receipt_value(receipt, "obligation_missing_ids", [])


def pre_compaction_flush_enabled(config: Any) -> bool:
    from opensquilla.memory.flush_config import is_session_flush_enabled

    if not is_session_flush_enabled():
        return False
    memory_cfg = getattr(config, "memory", None)
    return bool(getattr(memory_cfg, "flush_enabled", True))


def pre_compaction_flush_requires_safe_receipt(config: Any) -> bool:
    memory_cfg = getattr(config, "memory", None)
    if memory_cfg is None:
        return False
    return bool(getattr(memory_cfg, "flush_compaction_requires_safe_receipt", False))


def flush_receipt_to_dict(receipt: Any) -> dict[str, Any]:
    if receipt is None:
        return {}
    to_dict = getattr(receipt, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    if isinstance(receipt, Mapping):
        return dict(receipt)
    return dict(vars(receipt))
