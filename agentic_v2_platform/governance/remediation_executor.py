# =========================================================
# Remediation Executor
#
# THE EXECUTION FIREWALL.
#
# This module is the ONLY component that writes to a
# database.  Every function here begins by verifying
# that the corresponding ApprovalRequest carries status
# "APPROVED" before issuing any DML command.
#
# Call flow:
#   workflow (diagnosis) → HITL gate → THIS MODULE
# =========================================================

from __future__ import annotations

import logging
from typing import Any, Dict

from agentic_v2_platform.database.multi_db_router import get_router
from agentic_v2_platform.governance.hitl_store import (
    get_approval,
    record_execution_result,
    update_approval_status,
)

logger = logging.getLogger(__name__)


class ApprovalGateError(Exception):
    """Raised when execution is attempted without a valid APPROVED signal."""


# =========================================================
# EXECUTION GATE
# =========================================================

def _assert_approved(approval_id: str) -> None:
    """
    Hard gate: raises ApprovalGateError if the approval
    record does not exist or is not in APPROVED state.
    """
    req = get_approval(approval_id)
    if req is None:
        raise ApprovalGateError(
            f"No approval record found for ID '{approval_id}'. "
            "Execution blocked."
        )
    if req.status != "APPROVED":
        raise ApprovalGateError(
            f"Approval '{approval_id}' has status '{req.status}'. "
            "Execution requires status APPROVED. Blocked."
        )
    logger.info(
        "Approval gate passed for %s (ticket=%s, action=%s)",
        approval_id,
        req.ticket_key,
        req.action,
    )


# =========================================================
# GENERIC APPROVED EXECUTION
# =========================================================

def execute_approved_sql(
    approval_id: str,
    executed_by: str = "AGENT",
) -> Dict[str, Any]:
    """
    Execute the DML action stored in the approval record.

    Steps:
    1. Verify approval status is APPROVED (gate).
    2. Execute the action SQL against the target DB.
    3. Record the outcome back into the approval store.

    Returns the execution result dict.
    """
    _assert_approved(approval_id)
    req = get_approval(approval_id)

    logger.warning(
        "EXECUTING approved action [%s] on [%s]: %s",
        approval_id,
        req.db_alias,
        req.action,
    )

    try:
        result = get_router().execute(req.db_alias, req.action)
        result["approval_id"] = approval_id
        result["executed_by"] = executed_by
        record_execution_result(approval_id, result)
        logger.info("Execution SUCCEEDED for approval %s", approval_id)
        return result
    except Exception as exc:
        failure = {
            "status": "FAILED",
            "approval_id": approval_id,
            "error": str(exc),
            "executed_by": executed_by,
        }
        record_execution_result(approval_id, failure)
        # Do NOT re-raise — caller decides how to handle
        logger.error("Execution FAILED for approval %s: %s", approval_id, exc)
        return failure


# =========================================================
# SPECIFIC REMEDIATION ACTIONS
# =========================================================

def kill_blocking_spid(
    approval_id: str,
    spid: int,
    executed_by: str = "AGENT",
) -> Dict[str, Any]:
    """
    Kill a blocking SQL Server session.

    The KILL statement is assembled here and stored in the
    approval record during workflow creation.  The actual
    execution only runs after HITL APPROVE.
    """
    _assert_approved(approval_id)
    req = get_approval(approval_id)

    kill_sql = f"KILL {spid};"
    logger.warning(
        "Executing KILL SPID %d on %s (approval=%s)",
        spid,
        req.db_alias,
        approval_id,
    )
    try:
        result = get_router().execute(req.db_alias, kill_sql)
        result.update({"approval_id": approval_id, "spid_killed": spid})
        record_execution_result(approval_id, result)
        return result
    except Exception as exc:
        failure = {
            "status": "FAILED",
            "approval_id": approval_id,
            "spid": spid,
            "error": str(exc),
        }
        record_execution_result(approval_id, failure)
        return failure


def rebuild_index(
    approval_id: str,
    table_name: str,
    index_name: str,
    executed_by: str = "AGENT",
) -> Dict[str, Any]:
    """Rebuild a fragmented index after HITL APPROVE."""
    _assert_approved(approval_id)
    req = get_approval(approval_id)
    sql = f"ALTER INDEX [{index_name}] ON [{table_name}] REBUILD WITH (ONLINE = OFF);"
    try:
        result = get_router().execute(req.db_alias, sql)
        result.update({"approval_id": approval_id})
        record_execution_result(approval_id, result)
        return result
    except Exception as exc:
        failure = {"status": "FAILED", "approval_id": approval_id, "error": str(exc)}
        record_execution_result(approval_id, failure)
        return failure


def restart_sql_agent_job(
    approval_id: str,
    job_name: str,
    executed_by: str = "AGENT",
) -> Dict[str, Any]:
    """Re-start a failed SQL Agent job after HITL APPROVE."""
    _assert_approved(approval_id)
    req = get_approval(approval_id)
    sql = f"EXEC msdb.dbo.sp_start_job N'{job_name}';"
    try:
        result = get_router().execute(req.db_alias, sql)
        result.update({"approval_id": approval_id, "job_name": job_name})
        record_execution_result(approval_id, result)
        return result
    except Exception as exc:
        failure = {"status": "FAILED", "approval_id": approval_id, "error": str(exc)}
        record_execution_result(approval_id, failure)
        return failure
