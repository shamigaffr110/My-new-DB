# =========================================================
# Jira Integration Agent
#
# Fetches open DBA tickets from Jira, enriches them with
# live DB diagnostics via MCP tools, and hands off to the
# HITL notification layer before any execution occurs.
#
# Dependencies:  jira (atlassian-python-api package)
# Auth:          API token (Basic auth over HTTPS)
# =========================================================

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from jira import JIRA, Issue

from agentic_v2_platform.config.settings import get_settings, JiraSettings

logger = logging.getLogger(__name__)


# =========================================================
# DATA CLASSES
# =========================================================

@dataclass
class JiraTicket:
    """Normalised DBA ticket from Jira."""
    key: str
    summary: str
    description: str
    status: str
    priority: str
    assignee: str
    reporter: str
    created: str
    labels: List[str] = field(default_factory=list)
    db_alias: str = ""           # parsed from ticket if present
    raw: Optional[Dict] = None   # original Jira issue fields


# =========================================================
# JIRA CLIENT
# =========================================================

class JiraAgent:
    """
    Connects to Jira and fetches DBA operation tickets.

    All credentials come exclusively from environment
    variables (via PlatformSettings) — never hard-coded.
    """

    def __init__(self, settings: Optional[JiraSettings] = None):
        self._cfg = settings or get_settings().jira
        self._client: Optional[JIRA] = None

    # --------------------------------------------------
    # CONNECTION
    # --------------------------------------------------

    def _ensure_connected(self) -> None:
        if self._client:
            return
        if not self._cfg.is_configured:
            raise RuntimeError(
                "Jira is not configured. Set JIRA_URL, JIRA_USERNAME, "
                "and JIRA_API_TOKEN environment variables."
            )
        self._client = JIRA(
            server=self._cfg.url,
            basic_auth=(self._cfg.username, self._cfg.api_token),
        )
        logger.info("Connected to Jira at %s", self._cfg.url)

    # --------------------------------------------------
    # FETCH OPEN DBA TICKETS
    # --------------------------------------------------

    def fetch_open_dba_tickets(self, max_results: int = 50) -> List[JiraTicket]:
        """
        Return open DBA tickets from the configured project.
        Filters by label (JIRA_POLL_LABEL) and statuses that
        indicate work is pending or in-progress.
        """
        self._ensure_connected()
        jql = (
            f'project = "{self._cfg.project_key}" '
            f'AND labels = "{self._cfg.poll_label}" '
            f'AND status IN ("Open", "To Do", "In Progress", "Reopened") '
            f'ORDER BY priority ASC, created DESC'
        )
        logger.info("Fetching Jira tickets with JQL: %s", jql)
        issues: List[Issue] = self._client.search_issues(jql, maxResults=max_results)  # type: ignore[attr-defined]
        return [self._normalise(issue) for issue in issues]

    # --------------------------------------------------
    # FETCH SINGLE TICKET BY KEY
    # --------------------------------------------------

    def fetch_ticket(self, ticket_key: str) -> Optional[JiraTicket]:
        """Return a single ticket by its key (e.g. DBA-42)."""
        self._ensure_connected()
        try:
            issue = self._client.issue(ticket_key)  # type: ignore[attr-defined]
            return self._normalise(issue)
        except Exception as exc:
            logger.warning("Could not fetch ticket %s: %s", ticket_key, exc)
            return None

    # --------------------------------------------------
    # UPDATE TICKET COMMENT
    # --------------------------------------------------

    def add_comment(self, ticket_key: str, comment: str) -> None:
        """Post an agent-generated comment on a Jira ticket."""
        self._ensure_connected()
        self._client.add_comment(ticket_key, comment)  # type: ignore[attr-defined]
        logger.info("Posted comment on %s", ticket_key)

    # --------------------------------------------------
    # TRANSITION TICKET (e.g. resolve it)
    # --------------------------------------------------

    def resolve_ticket(self, ticket_key: str, comment: str = "") -> None:
        """
        Move a ticket to 'Done' / 'Resolved'.
        Transitions available depend on the project workflow.
        """
        self._ensure_connected()
        transitions = self._client.transitions(ticket_key)  # type: ignore[attr-defined]
        done_ids = [
            t["id"]
            for t in transitions
            if t["name"].lower() in ("done", "resolved", "close", "closed")
        ]
        if not done_ids:
            logger.warning(
                "No 'Done/Resolved' transition found for %s. "
                "Available: %s",
                ticket_key,
                [t["name"] for t in transitions],
            )
            return
        self._client.transition_issue(ticket_key, done_ids[0])  # type: ignore[attr-defined]
        if comment:
            self.add_comment(ticket_key, comment)
        logger.info("Resolved ticket %s", ticket_key)

    # --------------------------------------------------
    # NORMALISE raw Jira issue → JiraTicket
    # --------------------------------------------------

    @staticmethod
    def _normalise(issue: Issue) -> JiraTicket:
        fields = issue.fields
        description = getattr(fields, "description", "") or ""
        # Attempt to extract a database alias from ticket body.
        # Convention: "DB_ALIAS: PROD_01" anywhere in description.
        db_alias = ""
        for line in description.splitlines():
            if line.strip().upper().startswith("DB_ALIAS:"):
                db_alias = line.split(":", 1)[1].strip().upper()
                break

        assignee_name = ""
        if hasattr(fields, "assignee") and fields.assignee:
            assignee_name = getattr(fields.assignee, "displayName", "")

        reporter_name = ""
        if hasattr(fields, "reporter") and fields.reporter:
            reporter_name = getattr(fields.reporter, "displayName", "")

        priority_name = ""
        if hasattr(fields, "priority") and fields.priority:
            priority_name = getattr(fields.priority, "name", "")

        return JiraTicket(
            key=issue.key,
            summary=fields.summary or "",
            description=description,
            status=str(fields.status or ""),
            priority=priority_name,
            assignee=assignee_name,
            reporter=reporter_name,
            created=str(getattr(fields, "created", datetime.utcnow().isoformat())),
            labels=list(getattr(fields, "labels", []) or []),
            db_alias=db_alias,
            raw={
                "id": issue.id,
                "key": issue.key,
                "url": issue.permalink(),
            },
        )


# =========================================================
# MODULE-LEVEL HELPER
# =========================================================

_agent: Optional[JiraAgent] = None


def get_jira_agent() -> JiraAgent:
    global _agent
    if _agent is None:
        _agent = JiraAgent()
    return _agent
