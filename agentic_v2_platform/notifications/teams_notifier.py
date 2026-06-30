# =========================================================
# MS Teams HITL Notifier
#
# Sends an Adaptive Card to a Teams channel via an
# Incoming Webhook.  The card includes [Approve] and
# [Deny] deep-links back to the platform's HITL API.
#
# Falls back to simulation / console print when the
# webhook URL is not configured.
# =========================================================

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Optional

from agentic_v2_platform.config.settings import get_settings

logger = logging.getLogger(__name__)


# =========================================================
# LOW-LEVEL SEND
# =========================================================

def _post_to_webhook(webhook_url: str, payload: dict) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            ok = 200 <= resp.status < 300
            if ok:
                logger.info("Teams webhook delivered (HTTP %s)", resp.status)
            else:
                logger.warning("Teams webhook returned HTTP %s", resp.status)
            return ok
    except Exception as exc:
        logger.error("Teams webhook failed: %s", exc)
        return False


# =========================================================
# HITL APPROVAL CARD
# Sends a rich MessageCard with Approve / Deny links.
# =========================================================

def send_hitl_teams_alert(
    *,
    approval_id: str,
    ticket_key: str,
    db_alias: str,
    summary: str,
    recommendation: str,
    risk_level: str,
    approve_url: str,
    deny_url: str,
) -> bool:
    """
    Post a Human-in-the-Loop approval request to MS Teams.

    The card renders inline in Teams with two action buttons
    that call the platform's HITL API endpoint.
    """
    cfg = get_settings().notifications
    webhook_url = cfg.teams_webhook_url

    color_map = {"HIGH": "FF0000", "MEDIUM": "FFA500", "LOW": "00AA00", "CRITICAL": "8B0000"}
    color = color_map.get(risk_level.upper(), "0078D7")

    payload = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary": f"[HITL] DBA Action Required – {ticket_key}",
        "themeColor": color,
        "title": f"🔔 DBA Action Required — {ticket_key}",
        "sections": [
            {
                "activityTitle": f"**Ticket:** {ticket_key}",
                "activitySubtitle": f"**Database:** {db_alias} | **Risk:** {risk_level}",
                "activityText": f"**Summary:** {summary}",
                "facts": [
                    {"name": "Approval ID", "value": approval_id},
                    {"name": "Recommendation", "value": recommendation},
                    {"name": "Risk Level", "value": risk_level},
                    {"name": "Database Target", "value": db_alias},
                ],
            }
        ],
        "potentialAction": [
            {
                "@type": "OpenUri",
                "name": "✅ APPROVE",
                "targets": [{"os": "default", "uri": f"{approve_url}?id={approval_id}"}],
            },
            {
                "@type": "OpenUri",
                "name": "❌ DENY",
                "targets": [{"os": "default", "uri": f"{deny_url}?id={approval_id}"}],
            },
        ],
    }

    if not webhook_url:
        _simulate(payload, reason="TEAMS_WEBHOOK_URL not set")
        return False

    return _post_to_webhook(webhook_url, payload)


# =========================================================
# GENERIC ALERT (non-HITL)
# =========================================================

def send_teams_alert(title: str, message: str) -> bool:
    """Simple text alert to Teams — no action buttons."""
    cfg = get_settings().notifications
    webhook_url = cfg.teams_webhook_url

    payload = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary": title,
        "themeColor": "0078D7",
        "title": title,
        "text": message.replace("\n", "<br>"),
    }

    if not webhook_url:
        _simulate(payload, reason="TEAMS_WEBHOOK_URL not set")
        return False

    return _post_to_webhook(webhook_url, payload)


# =========================================================
# SIMULATION / CONSOLE FALLBACK
# =========================================================

def _simulate(payload: dict, reason: str) -> None:
    logger.warning("TEAMS SIMULATION — %s", reason)
    print("\n" + "=" * 60)
    print(" TEAMS NOTIFICATION — SIMULATION MODE")
    print("=" * 60)
    print(f"Reason : {reason}")
    print(f"Title  : {payload.get('title', payload.get('summary', ''))}")
    for section in payload.get("sections", []):
        print(f"\n{section.get('activityTitle', '')}")
        for fact in section.get("facts", []):
            print(f"  {fact['name']}: {fact['value']}")
    for action in payload.get("potentialAction", []):
        for target in action.get("targets", []):
            print(f"  [{action['name']}] -> {target['uri']}")
    print("=" * 60 + "\n")
