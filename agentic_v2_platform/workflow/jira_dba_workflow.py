# =========================================================
# Jira → DBA Agentic Workflow (V2)
#
# COMPLETE PIPELINE:
#
#   1. Fetch open DBA tickets from Jira.
#   2. For each ticket, resolve the target DB alias.
#   3. Run MCP diagnostic tools against that DB (READ ONLY).
#   4. Pass diagnostic data + ticket context to the AI
#      Analysis Agent.
#   5. If requires_hitl == True:
#        a. Create an ApprovalRequest (PENDING).
#        b. Send Teams HITL alert (Approve / Deny card).
#        c. Send HITL email.
#        d. Post the recommendation as a Jira comment.
#        e. PAUSE — no DB changes until APPROVE signal.
#      Else:
#        Post diagnostic summary as a Jira comment.
#   6. When /hitl/approve is called (via the FastAPI layer),
#      execute_approved_sql() fires and resolves the ticket.
#
# EXECUTION IS STRICTLY ISOLATED behind the ApprovalGate.
# =========================================================

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from agentic_v2_platform.agents.dba_analysis_agent import get_analysis_agent
from agentic_v2_platform.config.settings import get_settings
from agentic_v2_platform.database.dba_queries import (
    check_backup_status,
    check_blocking_sessions,
    check_cpu_pressure,
    check_failed_jobs,
    check_long_running_queries,
)
from agentic_v2_platform.governance.hitl_store import create_approval
from agentic_v2_platform.integrations.jira_agent import JiraTicket, get_jira_agent
from agentic_v2_platform.notifications.email_notifier import send_hitl_email
from agentic_v2_platform.notifications.teams_notifier import send_hitl_teams_alert

logger = logging.getLogger(__name__)


# =========================================================
# STEP 3: COLLECT DIAGNOSTICS
# =========================================================

def _collect_diagnostics(alias: str) -> Dict[str, Any]:
    """
    Run all READ-ONLY diagnostic queries against the target
    database.  Returns a dict keyed by check name.
    Returns empty sub-dicts on individual failures so the
    rest of the pipeline is not blocked.
    """
    data: Dict[str, Any] = {}

    for check_name, fn, kwargs in [
        ("blocking_sessions",    check_blocking_sessions,    {}),
        ("cpu_pressure",         check_cpu_pressure,         {}),
        ("long_running_queries", check_long_running_queries, {"threshold_minutes": 5}),
        ("backup_status",        check_backup_status,        {}),
        ("failed_jobs",          check_failed_jobs,          {}),
    ]:
        try:
            data[check_name] = fn(alias, **kwargs)  # type: ignore[call-arg]
        except Exception as exc:
            logger.warning("Diagnostic check %s on %s failed: %s", check_name, alias, exc)
            data[check_name] = {"error": str(exc)}

    return data


# =========================================================
# STEP 5: HITL NOTIFICATION
# =========================================================

def _send_hitl_notifications(
    approval_id: str,
    ticket: JiraTicket,
    analysis: Dict[str, Any],
) -> None:
    cfg = get_settings().notifications
    approve_url = cfg.hitl_approve_url
    deny_url = cfg.hitl_deny_url

    common = dict(
        approval_id=approval_id,
        ticket_key=ticket.key,
        db_alias=ticket.db_alias or "UNKNOWN",
        summary=analysis.get("incident_title", ticket.summary),
        recommendation=analysis.get("recommendation", ""),
        risk_level=analysis.get("risk_level", "MEDIUM"),
        approve_url=approve_url,
        deny_url=deny_url,
    )

    teams_ok = send_hitl_teams_alert(**common)
    email_ok = send_hitl_email(**common)

    logger.info(
        "HITL notifications sent — Teams=%s, Email=%s",
        teams_ok,
        email_ok,
    )


# =========================================================
# PROCESS A SINGLE TICKET
# =========================================================

def process_ticket(ticket: JiraTicket) -> Dict[str, Any]:
    """
    Run the full agentic pipeline for one Jira ticket.
    Returns a dict describing what happened.
    """
    logger.info("Processing ticket %s: %s", ticket.key, ticket.summary)

    # ── Resolve DB alias ───────────────────────────────
    alias = ticket.db_alias
    settings = get_settings()

    if not alias or alias not in settings.databases:
        if settings.databases:
            alias = next(iter(settings.databases))  # use first registered DB
            logger.warning(
                "Ticket %s has no DB_ALIAS — defaulting to '%s'",
                ticket.key,
                alias,
            )
        else:
            msg = f"No database targets configured. Cannot process {ticket.key}."
            logger.error(msg)
            return {"ticket": ticket.key, "status": "SKIPPED", "reason": msg}

    # ── Step 3: Diagnostics (READ ONLY) ────────────────
    logger.info("[%s] Running diagnostics on %s …", ticket.key, alias)
    diagnostic_data = _collect_diagnostics(alias)

    # ── Step 4: AI Analysis ────────────────────────────
    logger.info("[%s] Running AI analysis …", ticket.key)
    ticket_context = (
        f"Jira Ticket {ticket.key}: {ticket.summary}\n"
        f"Priority: {ticket.priority}\n"
        f"Description: {ticket.description[:500]}"
    )
    analysis = get_analysis_agent().analyse(ticket_context, diagnostic_data)

    logger.info(
        "[%s] Analysis: severity=%s, requires_hitl=%s, action=%s",
        ticket.key,
        analysis.get("severity"),
        analysis.get("requires_hitl"),
        analysis.get("action_sql"),
    )

    # ── Step 5a: HITL path ─────────────────────────────
    if analysis.get("requires_hitl") and analysis.get("action_sql"):
        approval = create_approval(
            ticket_key=ticket.key,
            db_alias=alias,
            action=analysis["action_sql"],
            recommendation=analysis.get("recommendation", ""),
            risk_level=analysis.get("risk_level", "HIGH"),
            diagnostic_summary=analysis.get("root_cause", ""),
        )
        logger.info("[%s] Approval %s created — waiting for HITL", ticket.key, approval.approval_id)

        # ── Step 5b-c: Notify ──────────────────────────
        _send_hitl_notifications(approval.approval_id, ticket, analysis)

        # ── Step 5d: Comment on Jira ───────────────────
        jira_comment = (
            f"🤖 *AI DBA Agent — Action Required*\n\n"
            f"*Approval ID:* {approval.approval_id}\n"
            f"*Risk Level:* {analysis.get('risk_level')}\n"
            f"*Severity:* {analysis.get('severity')}\n\n"
            f"*Root Cause:*\n{analysis.get('root_cause')}\n\n"
            f"*Recommendation:*\n{analysis.get('recommendation')}\n\n"
            f"*Proposed Action (pending approval):*\n{{code}}{analysis['action_sql']}{{code}}\n\n"
            f"An MS Teams and email alert has been sent with Approve / Deny options.\n"
            f"No database changes will be made until APPROVED."
        )
        try:
            get_jira_agent().add_comment(ticket.key, jira_comment)
        except Exception as exc:
            logger.warning("Could not comment on %s: %s", ticket.key, exc)

        return {
            "ticket": ticket.key,
            "db_alias": alias,
            "status": "HITL_PENDING",
            "approval_id": approval.approval_id,
            "analysis": analysis,
        }

    # ── No HITL required — informational comment ───────
    comment = (
        f"🤖 *AI DBA Agent — Diagnostic Complete*\n\n"
        f"*Severity:* {analysis.get('severity')}\n\n"
        f"*Root Cause:*\n{analysis.get('root_cause')}\n\n"
        f"*Recommendation:*\n{analysis.get('recommendation')}\n\n"
        f"No immediate DBA action required. Monitoring continues."
    )
    try:
        get_jira_agent().add_comment(ticket.key, comment)
    except Exception as exc:
        logger.warning("Could not comment on %s: %s", ticket.key, exc)

    return {
        "ticket": ticket.key,
        "db_alias": alias,
        "status": "NO_ACTION_REQUIRED",
        "analysis": analysis,
    }


# =========================================================
# MAIN WORKFLOW RUNNER
# =========================================================

def run_jira_dba_workflow(max_tickets: int = 20) -> List[Dict[str, Any]]:
    """
    Entry-point for the Jira → DBA agentic pipeline.

    Fetches all open DBA tickets and processes each one.
    Returns a list of result dicts (one per ticket).
    """
    logger.info("=== Jira DBA Agentic Workflow Starting ===")
    results: List[Dict[str, Any]] = []

    try:
        tickets = get_jira_agent().fetch_open_dba_tickets(max_results=max_tickets)
    except Exception as exc:
        logger.error("Failed to fetch Jira tickets: %s", exc)
        return [{"status": "JIRA_FETCH_ERROR", "error": str(exc)}]

    if not tickets:
        logger.info("No open DBA tickets found.")
        return [{"status": "NO_TICKETS"}]

    logger.info("Found %d open DBA ticket(s).", len(tickets))

    for ticket in tickets:
        try:
            result = process_ticket(ticket)
        except Exception as exc:
            logger.exception("Unexpected error processing %s: %s", ticket.key, exc)
            result = {"ticket": ticket.key, "status": "ERROR", "error": str(exc)}
        results.append(result)

    logger.info("=== Jira DBA Agentic Workflow Complete (%d tickets) ===", len(results))
    return results


# =========================================================
# SINGLE-TICKET RUNNER (for CLI / API)
# =========================================================

def run_single_ticket_workflow(ticket_key: str) -> Dict[str, Any]:
    """Process one specific Jira ticket by key."""
    ticket = get_jira_agent().fetch_ticket(ticket_key)
    if not ticket:
        return {"status": "NOT_FOUND", "ticket": ticket_key}
    return process_ticket(ticket)
