# =========================================================
# V2 Platform Settings & Credential Management
# All secrets are loaded exclusively from environment
# variables — never hard-coded.
# =========================================================

import os
from dataclasses import dataclass, field
from typing import Dict, Optional
from dotenv import load_dotenv

load_dotenv()


# =========================================================
# DATABASE TARGET DESCRIPTOR
# =========================================================

@dataclass
class DatabaseTarget:
    """
    Represents one managed SQL Server instance.
    Connection string is built at runtime from env vars
    so credentials never appear in source code.
    """
    alias: str               # e.g. "DB_PROD_01"
    server: str
    database: str
    username: str
    password: str
    driver: str = "ODBC Driver 17 for SQL Server"
    description: str = ""

    @property
    def connection_string(self) -> str:
        return (
            f"DRIVER={{{self.driver}}};"
            f"SERVER={self.server};"
            f"DATABASE={self.database};"
            f"UID={self.username};"
            f"PWD={self.password};"
        )


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# =========================================================
# MULTI-DATABASE REGISTRY
# Reads DB_<ALIAS>_* env vars to build target list.
# Add a new block in .env for each additional server.
# =========================================================

def load_database_targets() -> Dict[str, DatabaseTarget]:
    """
    Discover all database targets from environment.
    Convention:  DB_<ALIAS>_SERVER, DB_<ALIAS>_DATABASE,
                 DB_<ALIAS>_USERNAME, DB_<ALIAS>_PASSWORD
    Example aliases: PROD_01, PROD_02, DR_01
    """
    targets: Dict[str, DatabaseTarget] = {}

    # Scan env for DB_*_SERVER entries to auto-discover aliases
    for key, value in os.environ.items():
        if key.startswith("DB_") and key.endswith("_SERVER") and value:
            alias = key[3:-7]  # strip "DB_" prefix and "_SERVER" suffix
            targets[alias] = DatabaseTarget(
                alias=alias,
                server=value.strip(),
                database=_env(f"DB_{alias}_DATABASE"),
                username=_env(f"DB_{alias}_USERNAME"),
                password=_env(f"DB_{alias}_PASSWORD"),
                description=_env(f"DB_{alias}_DESCRIPTION", alias),
            )

    # Legacy single-DB fallback (original platform compatibility)
    if not targets and _env("DB_SERVER"):
        targets["DEFAULT"] = DatabaseTarget(
            alias="DEFAULT",
            server=_env("DB_SERVER"),
            database=_env("DB_DATABASE"),
            username=_env("DB_USERNAME"),
            password=_env("DB_PASSWORD"),
            description="Legacy single-database target",
        )

    return targets


# =========================================================
# JIRA SETTINGS
# =========================================================

@dataclass
class JiraSettings:
    url: str = field(default_factory=lambda: _env("JIRA_URL"))
    username: str = field(default_factory=lambda: _env("JIRA_USERNAME"))
    api_token: str = field(default_factory=lambda: _env("JIRA_API_TOKEN"))
    project_key: str = field(default_factory=lambda: _env("JIRA_PROJECT_KEY", "DBA"))
    dba_issue_type: str = field(default_factory=lambda: _env("JIRA_ISSUE_TYPE", "Task"))
    poll_label: str = field(default_factory=lambda: _env("JIRA_POLL_LABEL", "dba-ops"))

    @property
    def is_configured(self) -> bool:
        return bool(self.url and self.username and self.api_token)


# =========================================================
# NOTIFICATION SETTINGS
# =========================================================

@dataclass
class NotificationSettings:
    # MS Teams
    teams_webhook_url: str = field(default_factory=lambda: _env("TEAMS_WEBHOOK_URL"))

    # SMTP / Email
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: int(_env("SMTP_PORT", "587")))
    smtp_username: str = field(default_factory=lambda: _env("SMTP_USERNAME"))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD"))
    smtp_from: str = field(default_factory=lambda: _env("SMTP_FROM", "ai-dba@example.com"))
    smtp_to: str = field(default_factory=lambda: _env("SMTP_TO"))
    smtp_use_tls: bool = field(default_factory=lambda: _env("SMTP_USE_TLS", "true").lower() == "true")

    # Approval callback
    hitl_approve_url: str = field(default_factory=lambda: _env("HITL_APPROVE_URL", "http://localhost:8000/hitl/approve"))
    hitl_deny_url: str = field(default_factory=lambda: _env("HITL_DENY_URL", "http://localhost:8000/hitl/deny"))

    @property
    def teams_configured(self) -> bool:
        return bool(self.teams_webhook_url)

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_to)


# =========================================================
# AI / LLM SETTINGS
# =========================================================

@dataclass
class AISettings:
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_base_url: str = field(default_factory=lambda: _env("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"))
    model: str = field(default_factory=lambda: _env("AI_MODEL", "openai/gpt-4o-mini"))

    @property
    def is_configured(self) -> bool:
        return bool(self.openai_api_key)


# =========================================================
# PLATFORM SETTINGS SINGLETON
# =========================================================

@dataclass
class PlatformSettings:
    databases: Dict[str, DatabaseTarget] = field(default_factory=load_database_targets)
    jira: JiraSettings = field(default_factory=JiraSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    ai: AISettings = field(default_factory=AISettings)

    # MCP server
    mcp_host: str = field(default_factory=lambda: _env("MCP_HOST", "0.0.0.0"))
    mcp_port: int = field(default_factory=lambda: int(_env("MCP_PORT", "9000")))

    # FastAPI web layer
    api_host: str = field(default_factory=lambda: _env("API_HOST", "0.0.0.0"))
    api_port: int = field(default_factory=lambda: int(_env("API_PORT", "8000")))

    # Governance
    hitl_timeout_seconds: int = field(default_factory=lambda: int(_env("HITL_TIMEOUT_SECONDS", "3600")))
    approval_store_path: str = field(default_factory=lambda: _env("APPROVAL_STORE_PATH", "data/approvals.json"))
    audit_log_path: str = field(default_factory=lambda: _env("AUDIT_LOG_PATH", "data/audit.log"))


def get_settings() -> PlatformSettings:
    return PlatformSettings()
