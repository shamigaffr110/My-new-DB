# =========================================================
# Multi-Database Router
# Resolves a target alias → live pyodbc connection.
# The router is the ONLY place that opens DB connections;
# all other modules call it by alias — they never hold
# credentials themselves.
# =========================================================

from __future__ import annotations

import pyodbc
from typing import Any, Dict, List, Optional, Tuple

from agentic_v2_platform.config.settings import get_settings, DatabaseTarget


# =========================================================
# ROUTER
# =========================================================

class MultiDBRouter:
    """
    Thread-safe multi-database connection router.
    Uses short-lived connections (open → query → close)
    so the execution layer stays isolated until the HITL
    approval signal is received.
    """

    def __init__(self):
        self._settings = get_settings()

    # --------------------------------------------------
    # PUBLIC: list registered targets
    # --------------------------------------------------

    def list_targets(self) -> List[Dict[str, str]]:
        return [
            {
                "alias": t.alias,
                "server": t.server,
                "database": t.database,
                "description": t.description,
            }
            for t in self._settings.databases.values()
        ]

    # --------------------------------------------------
    # PUBLIC: resolve alias → DatabaseTarget
    # --------------------------------------------------

    def resolve(self, alias: str) -> Optional[DatabaseTarget]:
        alias_upper = alias.upper()
        return self._settings.databases.get(alias_upper)

    # --------------------------------------------------
    # PUBLIC: execute a read-only query
    # Returns list-of-dicts (column → value).
    # Raises ValueError for unknown alias.
    # Raises RuntimeError for connection / query failure.
    # --------------------------------------------------

    def query(
        self,
        alias: str,
        sql: str,
        params: Optional[Tuple] = None,
    ) -> List[Dict[str, Any]]:
        target = self._resolve_or_raise(alias)
        conn = self._connect(target)
        try:
            cursor = conn.cursor()
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
            columns = [col[0] for col in cursor.description or []]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            return rows
        finally:
            conn.close()

    # --------------------------------------------------
    # PUBLIC: execute a DML statement (guarded — call
    # only AFTER HITL approval is confirmed).
    # --------------------------------------------------

    def execute(
        self,
        alias: str,
        sql: str,
        params: Optional[Tuple] = None,
    ) -> Dict[str, Any]:
        target = self._resolve_or_raise(alias)
        conn = self._connect(target)
        try:
            cursor = conn.cursor()
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
            conn.commit()
            return {"status": "SUCCESS", "alias": alias, "sql": sql}
        except Exception as exc:
            conn.rollback()
            raise RuntimeError(f"Execute failed on {alias}: {exc}") from exc
        finally:
            conn.close()

    # --------------------------------------------------
    # INTERNAL helpers
    # --------------------------------------------------

    def _resolve_or_raise(self, alias: str) -> DatabaseTarget:
        target = self.resolve(alias)
        if not target:
            known = list(self._settings.databases.keys())
            raise ValueError(
                f"Unknown database alias '{alias}'. "
                f"Registered targets: {known}"
            )
        return target

    @staticmethod
    def _connect(target: DatabaseTarget) -> pyodbc.Connection:
        try:
            return pyodbc.connect(target.connection_string, timeout=10)
        except Exception as exc:
            raise RuntimeError(
                f"Cannot connect to [{target.alias}] "
                f"({target.server}/{target.database}): {exc}"
            ) from exc


# =========================================================
# MODULE-LEVEL SINGLETON
# =========================================================

_router: Optional[MultiDBRouter] = None


def get_router() -> MultiDBRouter:
    global _router
    if _router is None:
        _router = MultiDBRouter()
    return _router
