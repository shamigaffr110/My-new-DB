# =========================================================
# V2 Platform Entry Point — CLI Operations Console
#
# Provides an interactive menu mirroring the original V1
# console while adding V2 Jira workflow and multi-DB ops.
# =========================================================

from __future__ import annotations

import logging
import sys
from dataclasses import asdict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

from agentic_v2_platform.config.settings import get_settings
from agentic_v2_platform.database.multi_db_router import get_router
from agentic_v2_platform.governance.hitl_store import (
    get_approval,
    list_all,
    list_pending,
    update_approval_status,
)
from agentic_v2_platform.governance.remediation_executor import execute_approved_sql
from agentic_v2_platform.workflow.jira_dba_workflow import (
    run_jira_dba_workflow,
    run_single_ticket_workflow,
)


# =========================================================
# MENU ACTIONS
# =========================================================

def _header(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")


def show_databases() -> None:
    _header("Registered Database Targets")
    for t in get_router().list_targets():
        print(f"  [{t['alias']}]  {t['server']} / {t['database']}")
        if t.get("description"):
            print(f"         {t['description']}")


def run_jira_workflow() -> None:
    _header("Jira DBA Agentic Workflow")
    try:
        results = run_jira_dba_workflow()
        for r in results:
            print(f"  {r.get('ticket', 'N/A')} → {r.get('status')} | "
                  f"approval={r.get('approval_id', 'N/A')}")
    except Exception as exc:
        print(f"  ERROR: {exc}")


def run_single_ticket() -> None:
    key = input("  Enter Jira ticket key (e.g. DBA-42): ").strip()
    if not key:
        print("  No ticket key entered.")
        return
    result = run_single_ticket_workflow(key)
    print(f"\n  Status    : {result.get('status')}")
    print(f"  DB Alias  : {result.get('db_alias', 'N/A')}")
    if result.get("approval_id"):
        print(f"  Approval  : {result['approval_id']}")
    if result.get("analysis"):
        a = result["analysis"]
        print(f"  Severity  : {a.get('severity')}")
        print(f"  RCA       : {a.get('root_cause', '')[:120]}")
        print(f"  Action SQL: {a.get('action_sql', 'None')}")


def view_pending() -> None:
    _header("Pending HITL Approvals")
    pending = list_pending()
    if not pending:
        print("  No pending approvals.")
        return
    for p in pending:
        print(f"  [{p.approval_id[:8]}...]  {p.ticket_key}  "
              f"DB={p.db_alias}  Risk={p.risk_level}")
        print(f"    Action: {p.action}")
        print(f"    Created: {p.created_at}\n")


def approve_request() -> None:
    _header("Approve HITL Request")
    view_pending()
    approval_id = input("  Enter full Approval ID: ").strip()
    if not approval_id:
        return
    req = get_approval(approval_id)
    if not req:
        print("  Approval not found.")
        return
    by = input("  Approved by [DBA_LEAD]: ").strip() or "DBA_LEAD"
    reason = input("  Reason [Approved via CLI]: ").strip() or "Approved via CLI"

    update_approval_status(approval_id, "APPROVED", decided_by=by, reason=reason)
    result = execute_approved_sql(approval_id, executed_by=by)
    print(f"\n  Execution Status: {result.get('status')}")
    if result.get("error"):
        print(f"  Error: {result['error']}")


def deny_request() -> None:
    _header("Deny HITL Request")
    view_pending()
    approval_id = input("  Enter full Approval ID: ").strip()
    if not approval_id:
        return
    req = get_approval(approval_id)
    if not req:
        print("  Approval not found.")
        return
    by = input("  Denied by [DBA_LEAD]: ").strip() or "DBA_LEAD"
    reason = input("  Reason [Denied via CLI]: ").strip() or "Denied via CLI"
    update_approval_status(approval_id, "DENIED", decided_by=by, reason=reason)
    print("  Request denied. Database was NOT modified.")


def view_all_approvals() -> None:
    _header("Approval History")
    for r in list_all():
        print(f"  [{r.status:10}]  {r.approval_id[:8]}...  "
              f"{r.ticket_key}  DB={r.db_alias}")


def start_api_server() -> None:
    _header("Starting V2 API Server")
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "agentic_v2_platform.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )


def start_mcp_server() -> None:
    _header("Starting MCP Server")
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "agentic_v2_platform.mcp_server.server:app",
        host=settings.mcp_host,
        port=settings.mcp_port,
        reload=False,
    )


# =========================================================
# MENU
# =========================================================

def show_menu() -> None:
    print("""
============================================================
  AI DBA Operations Platform  V2 — Agentic Console
============================================================

  [Database Management]
  1.  Show registered database targets

  [Agentic Jira Workflow]
  2.  Run Jira DBA workflow  (fetch all open DBA tickets)
  3.  Process single Jira ticket

  [HITL Governance]
  4.  View pending approvals
  5.  Approve & execute request
  6.  Deny request
  7.  View approval history

  [Servers]
  8.  Start API server  (port 8000)
  9.  Start MCP server  (port 9000)

  10. Exit
""")


def main() -> None:
    actions = {
        "1": show_databases,
        "2": run_jira_workflow,
        "3": run_single_ticket,
        "4": view_pending,
        "5": approve_request,
        "6": deny_request,
        "7": view_all_approvals,
        "8": start_api_server,
        "9": start_mcp_server,
    }

    while True:
        show_menu()
        choice = input("  Enter choice: ").strip()

        if choice == "10":
            print("\n  Exiting platform. Goodbye.\n")
            break

        action = actions.get(choice)
        if action:
            action()
        else:
            print("  Invalid choice — please try again.")


if __name__ == "__main__":
    main()
