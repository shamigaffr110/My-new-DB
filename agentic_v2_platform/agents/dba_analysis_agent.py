# =========================================================
# DBA Analysis Agent
#
# Uses OpenAI (via OpenRouter) to:
#   1. Analyse raw diagnostic data from MCP tools.
#   2. Produce a structured recommendation dict with:
#      - action_sql  : exact SQL to execute (or None)
#      - risk_level  : LOW | MEDIUM | HIGH | CRITICAL
#      - rationale   : human-readable explanation
#      - requires_hitl: bool — True when HITL approval needed
# =========================================================

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx
from openai import OpenAI

from agentic_v2_platform.config.settings import get_settings

logger = logging.getLogger(__name__)

# Risk levels that must be gated by HITL before execution
HITL_REQUIRED_RISKS = {"HIGH", "CRITICAL"}


# =========================================================
# AGENT
# =========================================================

class DBAAnalysisAgent:
    """
    LLM-backed agent that analyses diagnostic context from
    MCP tools and returns a structured recommendation.
    """

    SYSTEM_PROMPT = """
You are an expert SQL Server DBA and database reliability engineer with 15+ years
of production experience.

You receive structured diagnostic data collected from SQL Server monitoring queries
and must produce a concise, actionable analysis.

Your response MUST be a valid JSON object (no markdown, no explanation outside the JSON)
with the following structure:
{
  "incident_title": "<one-line title>",
  "severity": "LOW | MEDIUM | HIGH | CRITICAL",
  "root_cause": "<2-3 sentence RCA>",
  "recommendation": "<natural-language recommendation>",
  "action_sql": "<exact SQL to execute, or null if no DML is needed>",
  "risk_level": "LOW | MEDIUM | HIGH | CRITICAL",
  "requires_hitl": true | false,
  "rationale": "<why HITL is or is not required>"
}

Rules:
- requires_hitl must be true when risk_level is HIGH or CRITICAL, or when
  action_sql involves KILL, DROP, TRUNCATE, ALTER, UPDATE, DELETE, or job restarts.
- action_sql should be null for read-only diagnostic findings.
- Be precise — action_sql will be executed verbatim on a production server.
"""

    def __init__(self):
        cfg = get_settings().ai
        if not cfg.is_configured:
            logger.warning("OPENAI_API_KEY not set — analysis agent will use fallback mode")
            self._client = None
            return

        http_client = httpx.Client(verify=False, timeout=60.0)
        self._client = OpenAI(
            api_key=cfg.openai_api_key,
            base_url=cfg.openai_base_url,
            http_client=http_client,
            default_headers={
                "HTTP-Referer": "http://localhost",
                "X-Title": "AI DBA Operations Platform V2",
            },
        )
        self._model = cfg.model

    def analyse(
        self,
        ticket_summary: str,
        diagnostic_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Analyse ticket context + live diagnostic data and return
        a structured recommendation.
        """
        user_content = (
            f"TICKET SUMMARY:\n{ticket_summary}\n\n"
            f"DIAGNOSTIC DATA:\n{json.dumps(diagnostic_data, indent=2, default=str)}"
        )

        if self._client is None:
            return self._fallback(ticket_summary, diagnostic_data)

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            raw = response.choices[0].message.content
            result = json.loads(raw)
            logger.info(
                "AI analysis complete — severity=%s, requires_hitl=%s",
                result.get("severity"),
                result.get("requires_hitl"),
            )
            return result
        except Exception as exc:
            logger.error("AI analysis failed: %s — using fallback", exc)
            return self._fallback(ticket_summary, diagnostic_data)

    # --------------------------------------------------
    # FALLBACK — rule-based heuristics when LLM unavailable
    # --------------------------------------------------

    @staticmethod
    def _fallback(
        ticket_summary: str,
        diagnostic_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        blocking = diagnostic_data.get("blocking_sessions", [])
        cpu = diagnostic_data.get("cpu_pressure", [])
        long_q = diagnostic_data.get("long_running_queries", [])

        if blocking:
            blocker = blocking[0]
            spid = blocker.get("blocker_spid", "?")
            return {
                "incident_title": f"Blocking Session Detected (SPID {spid})",
                "severity": "HIGH",
                "root_cause": (
                    f"SPID {spid} is blocking "
                    f"{len(blocking)} other session(s). "
                    "This can cause cascading timeouts across the workload."
                ),
                "recommendation": (
                    f"Kill blocking SPID {spid} to release the lock chain. "
                    "Investigate the root query afterwards to prevent recurrence."
                ),
                "action_sql": f"KILL {spid};",
                "risk_level": "HIGH",
                "requires_hitl": True,
                "rationale": "KILL statement terminates an active session — requires human approval.",
            }

        if long_q:
            q = long_q[0]
            minutes = q.get("run_minutes", "?")
            session = q.get("session_id", "?")
            return {
                "incident_title": f"Long-Running Query on Session {session}",
                "severity": "MEDIUM",
                "root_cause": (
                    f"Session {session} has been running for {minutes} minutes. "
                    "This may indicate a missing index, parameter sniffing issue, "
                    "or lock contention."
                ),
                "recommendation": (
                    "Capture the query plan and review index coverage. "
                    "Consider killing the session if it is blocking other work."
                ),
                "action_sql": None,
                "risk_level": "MEDIUM",
                "requires_hitl": False,
                "rationale": "No destructive action required at this time.",
            }

        if cpu:
            return {
                "incident_title": "Elevated CPU Detected",
                "severity": "MEDIUM",
                "root_cause": "High CPU sessions detected on the SQL Server instance.",
                "recommendation": "Review top CPU queries and consider index optimisation.",
                "action_sql": None,
                "risk_level": "MEDIUM",
                "requires_hitl": False,
                "rationale": "Monitoring only — no DML required.",
            }

        return {
            "incident_title": ticket_summary[:80],
            "severity": "LOW",
            "root_cause": "No critical conditions detected in diagnostic data.",
            "recommendation": "Continue routine monitoring.",
            "action_sql": None,
            "risk_level": "LOW",
            "requires_hitl": False,
            "rationale": "No actionable conditions found.",
        }


# =========================================================
# MODULE-LEVEL SINGLETON
# =========================================================

_agent: Optional[DBAAnalysisAgent] = None


def get_analysis_agent() -> DBAAnalysisAgent:
    global _agent
    if _agent is None:
        _agent = DBAAnalysisAgent()
    return _agent
