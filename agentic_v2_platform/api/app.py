# =========================================================
# FastAPI Application — V2 DBA Operations Platform
#
# Exposes:
#   /health              — platform health
#   /workflow/run        — trigger Jira DBA workflow
#   /workflow/ticket     — process single ticket
#   /hitl/approve        — HITL approve & execute
#   /hitl/deny           — HITL deny
#   /approvals           — list all approvals
#   /databases           — list registered DB targets
# =========================================================

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from agentic_v2_platform.config.settings import get_settings
from agentic_v2_platform.governance.hitl_store import (
    get_approval,
    list_all,
    list_pending,
    update_approval_status,
)
from agentic_v2_platform.governance.remediation_executor import (
    ApprovalGateError,
    execute_approved_sql,
)
from agentic_v2_platform.workflow.jira_dba_workflow import (
    run_jira_dba_workflow,
    run_single_ticket_workflow,
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="AI DBA Operations Platform V2",
    version="2.0.0",
    description=(
        "Agentic DBA platform with Jira integration, "
        "multi-database MCP tools, and Human-in-the-Loop governance."
    ),
)


# =========================================================
# HEALTH
# =========================================================

@app.get("/health", tags=["Platform"])
async def health() -> Dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "version": "2.0.0",
        "registered_databases": list(settings.databases.keys()),
        "jira_configured": settings.jira.is_configured,
        "teams_configured": settings.notifications.teams_configured,
        "email_configured": settings.notifications.email_configured,
        "ai_configured": settings.ai.is_configured,
    }


# =========================================================
# DATABASE TARGETS
# =========================================================

@app.get("/databases", tags=["Databases"])
async def list_databases() -> List[Dict[str, str]]:
    from agentic_v2_platform.database.multi_db_router import get_router
    return get_router().list_targets()


# =========================================================
# WORKFLOW TRIGGERS
# =========================================================

@app.post("/workflow/run", tags=["Workflow"])
async def trigger_jira_workflow(
    max_tickets: int = Query(default=20, ge=1, le=100)
) -> List[Dict[str, Any]]:
    """
    Fetch open DBA Jira tickets and process each one through
    the full agentic pipeline.  HITL approvals are sent via
    Teams and email; no DB changes happen in this call.
    """
    return run_jira_dba_workflow(max_tickets=max_tickets)


class SingleTicketRequest(BaseModel):
    ticket_key: str


@app.post("/workflow/ticket", tags=["Workflow"])
async def process_single_ticket(req: SingleTicketRequest) -> Dict[str, Any]:
    """Process one specific Jira ticket by key."""
    return run_single_ticket_workflow(req.ticket_key)


# =========================================================
# HITL ENDPOINTS
# =========================================================

@app.get("/hitl/approve", tags=["HITL"])
async def hitl_approve(
    id: str = Query(..., description="Approval ID from the HITL notification"),
    decided_by: str = Query(default="DBA_TEAM"),
    reason: str = Query(default="Approved via HITL link"),
) -> Dict[str, Any]:
    """
    Human-in-the-Loop APPROVE endpoint.

    Clicking the [Approve] link in Teams or email calls this.
    1. Marks the approval as APPROVED.
    2. Immediately executes the approved SQL against the target DB.
    3. Returns the execution result.

    The execution gate in remediation_executor.py re-verifies
    approval status even inside this handler.
    """
    approval = get_approval(id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"Approval '{id}' not found.")

    if approval.status != "PENDING":
        return {
            "approval_id": id,
            "status": approval.status,
            "message": f"Approval is already in state '{approval.status}'. No action taken.",
        }

    # Mark APPROVED in the store
    update_approval_status(id, status="APPROVED", decided_by=decided_by, reason=reason)
    logger.info("HITL APPROVE received for %s by %s", id, decided_by)

    # Execute the action (gate re-verified inside)
    try:
        exec_result = execute_approved_sql(id, executed_by=decided_by)
    except ApprovalGateError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        logger.error("Execution error after HITL approve: %s", exc)
        exec_result = {"status": "FAILED", "error": str(exc)}

    # Optionally resolve the Jira ticket
    try:
        from agentic_v2_platform.integrations.jira_agent import get_jira_agent
        from agentic_v2_platform.config.settings import get_settings
        if get_settings().jira.is_configured:
            get_jira_agent().resolve_ticket(
                approval.ticket_key,
                comment=(
                    f"✅ *DBA Action Executed & Ticket Resolved*\n\n"
                    f"Approval ID: {id}\n"
                    f"Approved By: {decided_by}\n"
                    f"Action: {approval.action}\n"
                    f"Result: {exec_result.get('status', 'UNKNOWN')}"
                ),
            )
    except Exception as exc:
        logger.warning("Could not resolve Jira ticket %s: %s", approval.ticket_key, exc)

    return {
        "approval_id": id,
        "status": "APPROVED_AND_EXECUTED",
        "decided_by": decided_by,
        "execution_result": exec_result,
    }


@app.get("/hitl/deny", tags=["HITL"])
async def hitl_deny(
    id: str = Query(..., description="Approval ID from the HITL notification"),
    decided_by: str = Query(default="DBA_TEAM"),
    reason: str = Query(default="Denied via HITL link"),
) -> Dict[str, Any]:
    """
    Human-in-the-Loop DENY endpoint.

    Marks the approval as DENIED and posts a Jira comment.
    The database is NOT touched.
    """
    approval = get_approval(id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"Approval '{id}' not found.")

    if approval.status != "PENDING":
        return {
            "approval_id": id,
            "status": approval.status,
            "message": f"Approval is already in state '{approval.status}'. No action taken.",
        }

    update_approval_status(id, status="DENIED", decided_by=decided_by, reason=reason)
    logger.info("HITL DENY received for %s by %s: %s", id, decided_by, reason)

    try:
        from agentic_v2_platform.integrations.jira_agent import get_jira_agent
        from agentic_v2_platform.config.settings import get_settings
        if get_settings().jira.is_configured:
            get_jira_agent().add_comment(
                approval.ticket_key,
                (
                    f"❌ *DBA Action Denied*\n\n"
                    f"Approval ID: {id}\n"
                    f"Denied By: {decided_by}\n"
                    f"Reason: {reason}\n"
                    f"Database was NOT modified. Ticket requires manual review."
                ),
            )
    except Exception as exc:
        logger.warning("Could not comment on Jira ticket %s: %s", approval.ticket_key, exc)

    return {
        "approval_id": id,
        "status": "DENIED",
        "decided_by": decided_by,
        "reason": reason,
        "message": "Database execution was blocked. No changes were made.",
    }


# =========================================================
# APPROVAL MANAGEMENT
# =========================================================

@app.get("/approvals", tags=["Governance"])
async def get_approvals(
    status: Optional[str] = Query(default=None, description="Filter by status")
) -> List[Dict[str, Any]]:
    """Return all approval records, optionally filtered by status."""
    from dataclasses import asdict
    records = list_all()
    if status:
        records = [r for r in records if r.status.upper() == status.upper()]
    return [asdict(r) for r in records]


@app.get("/approvals/pending", tags=["Governance"])
async def get_pending_approvals() -> List[Dict[str, Any]]:
    """Return only PENDING approvals."""
    from dataclasses import asdict
    return [asdict(r) for r in list_pending()]


# =========================================================
# ENTRYPOINT
# =========================================================

if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "agentic_v2_platform.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=True,
    )
