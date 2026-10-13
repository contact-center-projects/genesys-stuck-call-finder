/* =====================================================================
   diag_blocking.sql - FOR THE DBA / whoever has VIEW SERVER STATE.

   Context: a query that ran fine 3 days ago now hangs, and even a
   lightweight, narrowly-scoped read (READ UNCOMMITTED, no row locks
   needed) took 4 minutes just to CANCEL on the client side. That
   points at server-side blocking or resource contention, not a bad
   query plan - please run this WHILE the app/SSMS query is hanging,
   in a separate connection.
   ===================================================================== */
SET NOCOUNT ON;

/* 1) Who's blocking whom, right now. If BlockedBy is non-zero and the
      chain leads to one session, that's the head blocker - what is it
      running, how long has it been running, what does it want? */
SELECT r.session_id                    AS SPID,
       r.blocking_session_id           AS BlockedBy,
       r.status, r.command,
       r.wait_type, r.wait_resource, r.wait_time AS wait_ms,
       r.total_elapsed_time / 1000     AS elapsed_sec,
       r.cpu_time, r.logical_reads, r.reads, r.writes,
       s.login_name, s.host_name, s.program_name,
       DB_NAME(r.database_id)          AS DbName,
       SUBSTRING(st.text, 1, 500)      AS sql_text
FROM sys.dm_exec_requests r
JOIN sys.dm_exec_sessions s ON s.session_id = r.session_id
CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) st
WHERE s.is_user_process = 1
ORDER BY r.blocking_session_id DESC, r.total_elapsed_time DESC;

/* 2) Same info, task-level (catches parallel-query / latch waits that
      request-level view can miss). */
SELECT wt.session_id, wt.blocking_session_id, wt.wait_type,
       wt.wait_duration_ms, wt.resource_description
FROM sys.dm_os_waiting_tasks wt
WHERE wt.session_id > 50
ORDER BY wt.wait_duration_ms DESC;

/* 3) Any open transaction sitting uncommitted for a long time? An app
      that crashed or a session that never committed/rolled back can
      hold locks (or block others via Sch-S/Sch-M) indefinitely, on
      SQL Server tables even under READ UNCOMMITTED for anyone reading
      the same tables. */
SELECT s.session_id, s.login_name, s.host_name, s.program_name,
       at.transaction_id, at.name, at.transaction_begin_time,
       DATEDIFF(SECOND, at.transaction_begin_time, GETDATE()) AS open_sec
FROM sys.dm_tran_active_transactions at
JOIN sys.dm_tran_session_transactions st ON st.transaction_id = at.transaction_id
JOIN sys.dm_exec_sessions s ON s.session_id = st.session_id
ORDER BY open_sec DESC;

/* 4) Anything scheduled that could line up with "it broke 3 days ago" -
      ICON purge/archiving, index maintenance, backups. Check whether
      one of these changed schedule, started failing, or started
      running long around then. */
SELECT j.name AS JobName, ja.run_requested_date, jh.run_date, jh.run_time,
       jh.run_duration, jh.run_status, jh.message
FROM msdb.dbo.sysjobs j
JOIN msdb.dbo.sysjobactivity ja ON ja.job_id = j.job_id
LEFT JOIN msdb.dbo.sysjobhistory jh ON jh.job_id = j.job_id AND jh.step_id = 0
WHERE ja.run_requested_date >= DATEADD(DAY, -5, GETDATE())
ORDER BY ja.run_requested_date DESC;

/* 5) Page Life Expectancy + current wait stats snapshot - low PLE or a
      dominant wait type (PAGEIOLATCH_*, WRITELOG, RESOURCE_SEMAPHORE,
      etc.) point at memory/disk/tempdb pressure instead of blocking. */
SELECT RTRIM(object_name) AS ObjectName, cntr_value AS PageLifeExpectancySec
FROM sys.dm_os_performance_counters
WHERE counter_name = 'Page life expectancy';

SELECT TOP 15 wait_type, waiting_tasks_count, wait_time_ms, signal_wait_time_ms
FROM sys.dm_os_wait_stats
WHERE wait_time_ms > 0
  AND wait_type NOT LIKE '%SLEEP%' AND wait_type NOT LIKE 'XE%'
  AND wait_type NOT IN ('BROKER_TASK_STOP','BROKER_TO_FLUSH','CLR_SEMAPHORE',
                         'LAZYWRITER_SLEEP','SQLTRACE_BUFFER_FLUSH')
ORDER BY wait_time_ms DESC;
