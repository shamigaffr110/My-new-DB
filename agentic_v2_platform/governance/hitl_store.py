# =========================================================
# HITL Approval Store
#
# Persists approval requests as JSON.  This is the ONLY
# gate between the diagnosis phase and the execution phase.
# The execution layer (remediation_executor.py) MUST call
# get_approval() and verify status == "APPROVED" before
# touching any database.
# =========================================================

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from threading import Lock
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)
_LOCK = Lock()


# =========================================================
# DATA MODEL
# =========================================================

@dataclass
class ApprovalRequest:
    approval_id: str
    ticket_key: str
    db_alias: str
    action: str               # e.g. "KILL SPID 54"
    recommendation: str
    risk_level: str           # LOW | MEDIUM | HIGH | CRITICAL
    status: str = "PENDING"   # PENDING | APPROVED | DENIED | EXPIRED | EXECUTED
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None
    decision_reason: Optional[str] = None
    execution_result: Optional[Dict] = None
    diagnostic_summary: str = ""


# =========================================================
# PERSISTENCE HELPERS
# =========================================================

def _store_path() -> str:
    from agentic_v2_platform.config.settings import get_settings
    path = get_settings().approval_store_path
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return path


def _load_all() -> List[Dict]:
    path = _store_path()
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError:
            return []


def _save_all(records: List[Dict]) -> None:
    path = _store_path()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2, default=str)


# =========================================================
# PUBLIC API
# =========================================================

def create_approval(
    *,
    ticket_key: str,
    db_alias: str,
    action: str,
    recommendation: str,
    risk_level: str,
    diagnostic_summary: str = "",
) -> ApprovalRequest:
    """Persist a new PENDING approval request and return it."""
    req = ApprovalRequest(
        approval_id=str(uuid.uuid4()),
        ticket_key=ticket_key,
        db_alias=db_alias,
        action=action,
        recommendation=recommendation,
        risk_level=risk_level,
        diagnostic_summary=diagnostic_summary,
    )
    with _LOCK:
        records = _load_all()
        records.append(asdict(req))
        _save_all(records)
    logger.info("Created approval %s for ticket %s", req.approval_id, ticket_key)
    return req


def get_approval(approval_id: str) -> Optional[ApprovalRequest]:
    """Retrieve an approval request by ID."""
    with _LOCK:
        records = _load_all()
    for rec in records:
        if rec["approval_id"] == approval_id:
            return ApprovalRequest(**rec)
    return None


def update_approval_status(
    approval_id: str,
    status: str,
    decided_by: str = "SYSTEM",
    reason: str = "",
) -> Optional[ApprovalRequest]:
    """Transition status (APPROVED | DENIED | EXPIRED | EXECUTED)."""
    with _LOCK:
        records = _load_all()
        for rec in records:
            if rec["approval_id"] == approval_id:
                rec["status"] = status
                rec["decided_at"] = datetime.utcnow().isoformat()
                rec["decided_by"] = decided_by
                rec["decision_reason"] = reason
                _save_all(records)
                return ApprovalRequest(**rec)
    logger.warning("Approval %s not found for status update", approval_id)
    return None


def record_execution_result(approval_id: str, result: Dict) -> None:
    with _LOCK:
        records = _load_all()
        for rec in records:
            if rec["approval_id"] == approval_id:
                rec["execution_result"] = result
                rec["status"] = "EXECUTED"
                _save_all(records)
                return


def list_pending() -> List[ApprovalRequest]:
    with _LOCK:
        records = _load_all()
    return [ApprovalRequest(**r) for r in records if r.get("status") == "PENDING"]


def list_all() -> List[ApprovalRequest]:
    with _LOCK:
        records = _load_all()
    return [ApprovalRequest(**r) for r in records]
