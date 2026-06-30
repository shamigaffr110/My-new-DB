# =========================================================
# DBA Diagnostic Queries
# All queries are READ-ONLY.  Write operations live only
# in remediation_executor.py and are blocked until HITL
# approval is received.
# =========================================================

from __future__ import annotations
from typing import Any, Dict, List
from agentic_v2_platform.database.multi_db_router import get_router


def check_blocking_sessions(alias: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT
            blocking_session_id AS blocker_spid,
            session_id          AS blocked_spid,
            wait_time / 1000    AS wait_seconds,
            wait_type,
            SUBSTRING(st.text, (r.statement_start_offset/2)+1,
                ((CASE r.statement_end_offset WHEN -1 THEN DATALENGTH(st.text)
                  ELSE r.statement_end_offset END - r.statement_start_offset)/2)+1
            ) AS blocked_sql
        FROM sys.dm_exec_requests r
        CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) st
        WHERE blocking_session_id <> 0
        ORDER BY wait_time DESC;
    """
    return get_router().query(alias, sql)


def check_cpu_pressure(alias: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT TOP 10
            s.session_id,
            s.cpu_time,
            s.logical_reads,
            s.status,
            SUBSTRING(st.text, (r.statement_start_offset/2)+1,
                ((CASE r.statement_end_offset WHEN -1 THEN DATALENGTH(st.text)
                  ELSE r.statement_end_offset END - r.statement_start_offset)/2)+1
            ) AS current_sql
        FROM sys.dm_exec_sessions s
        INNER JOIN sys.dm_exec_requests r ON s.session_id = r.session_id
        CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) st
        WHERE s.is_user_process = 1
        ORDER BY s.cpu_time DESC;
    """
    return get_router().query(alias, sql)


def check_long_running_queries(alias: str, threshold_minutes: int = 5) -> List[Dict[str, Any]]:
    sql = """
        SELECT
            session_id,
            DATEDIFF(MINUTE, start_time, GETDATE()) AS run_minutes,
            status,
            wait_type,
            SUBSTRING(st.text, (r.statement_start_offset/2)+1,
                ((CASE r.statement_end_offset WHEN -1 THEN DATALENGTH(st.text)
                  ELSE r.statement_end_offset END - r.statement_start_offset)/2)+1
            ) AS sql_text
        FROM sys.dm_exec_requests r
        CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) st
        WHERE DATEDIFF(MINUTE, start_time, GETDATE()) > ?
          AND session_id <> @@SPID
        ORDER BY run_minutes DESC;
    """
    return get_router().query(alias, sql, (threshold_minutes,))


def check_backup_status(alias: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT TOP 20
            d.name                  AS database_name,
            MAX(b.backup_finish_date) AS last_backup,
            DATEDIFF(HOUR, MAX(b.backup_finish_date), GETDATE()) AS hours_since_backup,
            b.type                  AS backup_type
        FROM sys.databases d
        LEFT JOIN msdb.dbo.backupset b ON b.database_name = d.name
        WHERE d.database_id > 4
        GROUP BY d.name, b.type
        ORDER BY last_backup ASC;
    """
    return get_router().query(alias, sql)


def check_database_space(alias: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT
            DB_NAME()                          AS database_name,
            name                               AS logical_name,
            type_desc,
            CAST(size * 8.0 / 1024 AS DECIMAL(10,2)) AS size_mb,
            CAST(FILEPROPERTY(name,'SpaceUsed') * 8.0 / 1024 AS DECIMAL(10,2)) AS used_mb,
            CAST((size - FILEPROPERTY(name,'SpaceUsed')) * 8.0 / 1024 AS DECIMAL(10,2)) AS free_mb
        FROM sys.database_files;
    """
    return get_router().query(alias, sql)


def check_failed_jobs(alias: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT TOP 20
            j.name                   AS job_name,
            h.run_date,
            h.run_time,
            h.message
        FROM msdb.dbo.sysjobhistory h
        JOIN msdb.dbo.sysjobs j ON j.job_id = h.job_id
        WHERE h.run_status = 0
          AND h.step_id   = 0
        ORDER BY h.run_date DESC, h.run_time DESC;
    """
    return get_router().query(alias, sql)


def check_index_fragmentation(alias: str, threshold_pct: float = 30.0) -> List[Dict[str, Any]]:
    sql = """
        SELECT TOP 20
            OBJECT_NAME(i.object_id)    AS table_name,
            i.name                       AS index_name,
            ips.avg_fragmentation_in_percent,
            ips.page_count
        FROM sys.dm_db_index_physical_stats(DB_ID(), NULL, NULL, NULL, 'LIMITED') ips
        JOIN sys.indexes i ON i.object_id = ips.object_id
                           AND i.index_id = ips.index_id
        WHERE ips.avg_fragmentation_in_percent > ?
          AND ips.page_count > 100
        ORDER BY ips.avg_fragmentation_in_percent DESC;
    """
    return get_router().query(alias, sql, (threshold_pct,))


def get_active_spids(alias: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT
            s.session_id,
            s.login_name,
            s.host_name,
            s.program_name,
            s.status,
            s.cpu_time,
            s.memory_usage,
            s.reads,
            s.writes
        FROM sys.dm_exec_sessions s
        WHERE s.is_user_process = 1
        ORDER BY s.cpu_time DESC;
    """
    return get_router().query(alias, sql)
