/* =====================================================================
   diag_timeout.sql - why did the stuck-call query start timing out?
   Run in SSMS against ICON_VOICE.

   Sections are separated by GO so a permission error on one section
   does NOT stop the others - just run the whole script and look at
   each result grid / the Messages tab.

   PERMISSIONS NEEDED PER SECTION:
     A (age buckets)      - none beyond what the app already uses
     B (index list)       - none beyond normal read access
     C (column types)     - none (INFORMATION_SCHEMA)
     D (table sizes)      - VIEW DATABASE STATE
     E (stats freshness)  - VIEW DATABASE STATE
     F (memory pressure)  - VIEW SERVER STATE
     G (who else is busy) - VIEW SERVER STATE
   If your login lacks VIEW SERVER STATE / VIEW DATABASE STATE, D/E/F/G
   will each show a permission error in their own section and the rest
   still run. Send back whatever grids you get - A, B and C alone are
   already useful.
   ===================================================================== */
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
SET NOCOUNT ON;
GO

/* ---- A) Are there "zombie" active calls, bucketed by age? -----------
   After an ICON / T-Server restart or a link drop, calls can stay in
   G_CALL_ACTIVE / G_PARTY with TERMINATED IS NULL forever. Each one
   becomes a "no agent leg" candidate and costs ~2000 reads in the
   history lookup - this is the number that directly drives the 13s
   we already measured for the VQ lookup. If most candidates fall in
   "> 4h" or "> 24h", they're not real waiting calls - that's the
   likely root cause, not the query shape. --------------------------- */
DECLARE @t0 datetime2 = SYSDATETIME();

SELECT COUNT(*)                                                                     AS ActiveCalls,
       SUM(CASE WHEN CREATED < DATEADD(HOUR, -1,  GETUTCDATE()) THEN 1 ELSE 0 END)  AS Older_1h,
       SUM(CASE WHEN CREATED < DATEADD(HOUR, -4,  GETUTCDATE()) THEN 1 ELSE 0 END)  AS Older_4h,
       SUM(CASE WHEN CREATED < DATEADD(HOUR, -24, GETUTCDATE()) THEN 1 ELSE 0 END)  AS Older_24h,
       MIN(CREATED)                                                                 AS OldestCreatedUtc
FROM G_CALL_ACTIVE;

-- Candidate set exactly as the app builds it (and as v4's #cand stage
-- builds it), bucketed by age - this is where the "340" came from.
WITH Parties AS (
    SELECT p.CALLID,
           MAX(CASE WHEN p.AGENTID > 0 THEN p.STATE END)       AS AgentState,
           MAX(CASE WHEN p.ENDPOINTTYPE = 4 THEN 1 ELSE 0 END) AS InAcdQueue
    FROM G_PARTY p
    WHERE p.TERMINATED IS NULL
    GROUP BY p.CALLID
)
SELECT CASE WHEN c.CREATED >= DATEADD(MINUTE, -30, GETUTCDATE()) THEN '1: < 30 min'
            WHEN c.CREATED >= DATEADD(HOUR,   -4,  GETUTCDATE()) THEN '2: 30 min - 4 h'
            WHEN c.CREATED >= DATEADD(HOUR,   -24, GETUTCDATE()) THEN '3: 4 h - 24 h'
            ELSE                                                      '4: > 24 h' END AS AgeBucket,
       COUNT(*) AS Candidates
FROM G_CALL_ACTIVE c
JOIN Parties pt ON pt.CALLID = c.CALLID
WHERE pt.InAcdQueue = 1 OR pt.AgentState IS NULL
GROUP BY CASE WHEN c.CREATED >= DATEADD(MINUTE, -30, GETUTCDATE()) THEN '1: < 30 min'
              WHEN c.CREATED >= DATEADD(HOUR,   -4,  GETUTCDATE()) THEN '2: 30 min - 4 h'
              WHEN c.CREATED >= DATEADD(HOUR,   -24, GETUTCDATE()) THEN '3: 4 h - 24 h'
              ELSE                                                      '4: > 24 h' END
ORDER BY AgeBucket;

SELECT DATEDIFF(MILLISECOND, @t0, SYSDATETIME()) AS SectionA_ms;
GO

/* ---- B) Every index on the three tables the query touches -----------
   This tells us whether G_USERDATA_HISTORY has an index usable for
   "all rows for this CALLID, filtered by VALUE and an ADDED range" -
   if CALLID isn't the leading key of a useful index, that alone could
   explain why 340 lookups cost 13 seconds. -------------------------- */
SELECT t.name AS TableName, i.name AS IndexName, i.type_desc,
       i.has_filter, i.filter_definition,
       STUFF((SELECT ', ' + c.name
              FROM sys.index_columns ic
              JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
              WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id
                AND ic.is_included_column = 0
              ORDER BY ic.key_ordinal FOR XML PATH('')), 1, 2, '') AS KeyColumns,
       STUFF((SELECT ', ' + c.name
              FROM sys.index_columns ic
              JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
              WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id
                AND ic.is_included_column = 1
              FOR XML PATH('')), 1, 2, '') AS IncludedColumns
FROM sys.indexes i
JOIN sys.tables t ON t.object_id = i.object_id
WHERE t.name IN ('G_USERDATA_HISTORY', 'G_PARTY', 'G_CALL_ACTIVE')
  AND i.index_id > 0
ORDER BY t.name, i.index_id;
GO

/* ---- C) Column types that matter for joins/filters -------------------
   If G_USERDATA_HISTORY.VALUE is varchar but the app sends @queue as
   nvarchar (it does), every row gets implicitly converted before the
   comparison - small but free to fix once confirmed. ------------------ */
SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, COLLATION_NAME
FROM INFORMATION_SCHEMA.COLUMNS
WHERE (TABLE_NAME = 'G_USERDATA_HISTORY' AND COLUMN_NAME IN ('CALLID', 'KEYNAME', 'VALUE', 'ADDED'))
   OR (TABLE_NAME = 'G_PARTY'            AND COLUMN_NAME IN ('CALLID', 'TERMINATED'))
   OR (TABLE_NAME = 'G_CALL_ACTIVE'      AND COLUMN_NAME IN ('CALLID', 'CREATED'))
ORDER BY TABLE_NAME, COLUMN_NAME;
GO

/* ---- D) Table sizes (needs VIEW DATABASE STATE) ---------------------- */
SELECT t.name AS TableName,
       SUM(ps.row_count)                  AS RowCnt,
       SUM(ps.used_page_count) * 8 / 1024 AS UsedMB
FROM sys.dm_db_partition_stats ps
JOIN sys.tables t ON t.object_id = ps.object_id
WHERE ps.index_id IN (0, 1)
  AND t.name IN ('G_USERDATA_HISTORY', 'G_PARTY', 'G_CALL_ACTIVE', 'G_CALL',
                 'G_VIRTUAL_QUEUE', 'G_CUSTOM_DATA_S', 'G_CUSTOM_DATA_P')
GROUP BY t.name
ORDER BY RowCnt DESC;
GO

/* ---- E) When were statistics last updated? (needs VIEW DATABASE STATE)
   A plan flip ~3 days ago often lines up with an auto-stats update on
   one of these tables. ------------------------------------------------ */
SELECT t.name AS TableName, s.name AS StatName,
       sp.last_updated, sp.rows, sp.rows_sampled, sp.modification_counter
FROM sys.stats s
JOIN sys.tables t ON t.object_id = s.object_id
CROSS APPLY sys.dm_db_stats_properties(s.object_id, s.stats_id) sp
WHERE t.name IN ('G_USERDATA_HISTORY', 'G_PARTY', 'G_CALL_ACTIVE')
ORDER BY sp.last_updated DESC;
GO

/* ---- F) Memory pressure: Page Life Expectancy (needs VIEW SERVER STATE)
   The history lookup is only fast when cache-warm; a low PLE
   (< ~300s) means pages are being evicted and every refresh reads
   from disk. ----------------------------------------------------------- */
SELECT RTRIM(object_name) AS ObjectName, cntr_value AS PageLifeExpectancySec
FROM sys.dm_os_performance_counters
WHERE counter_name = 'Page life expectancy';
GO

/* ---- G) Anything else heavy running right now? (needs VIEW SERVER
   STATE). Run this WHILE the app (or v4) is actually querying, in a
   second SSMS tab, to catch it live - an ICON purge job, a Genesys
   Info Mart extract, an index rebuild, or someone else's report can
   all make this slower than it was 3 days ago with no code change. --- */
SELECT r.session_id, r.blocking_session_id, r.status, r.command,
       r.wait_type, r.wait_time AS wait_ms,
       r.total_elapsed_time / 1000 AS elapsed_sec,
       r.logical_reads, s.program_name, s.host_name, s.login_name,
       SUBSTRING(st.text, 1, 300) AS sql_text
FROM sys.dm_exec_requests r
JOIN sys.dm_exec_sessions s ON s.session_id = r.session_id
CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) st
WHERE r.session_id <> @@SPID AND s.is_user_process = 1
ORDER BY r.total_elapsed_time DESC;
GO
