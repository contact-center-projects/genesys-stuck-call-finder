/* =====================================================================
   stuck_calls_v4.sql - same result as DEFAULT_QUERY (v3), rewritten with
   temp tables. Run in SSMS next to v3 and compare time + row count.

   WHY v3 IS SLOWER THAN IT LOOKS
   SQL Server does NOT materialize CTEs - every reference is re-executed.
   In v3, VQFirst (the expensive G_USERDATA_HISTORY lookup) is referenced
   twice: once in the final SELECT, once via MatchedCalls -> ConnFromHistory.
   Candidates/Parties (the G_PARTY aggregation) is referenced three times.
   So the heaviest work can run 2-3x per refresh.

   v4 runs each stage ONCE into a small temp table. Temp tables also get
   real statistics, so each later step is planned with an accurate row
   count instead of a guess (a wrong guess is how a plan flips to a full
   G_USERDATA_HISTORY scan).

   Result columns and semantics are unchanged from v3.
   ===================================================================== */
SET NOCOUNT ON;
SET ARITHABORT ON;
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;

-- SSMS only: in the app, @queue comes in as a parameter - delete this line.
DECLARE @queue nvarchar(255) = N'LeumiCall_SECTOR_1';

-- The app sends @queue as NVARCHAR (AddWithValue). If VALUE is VARCHAR
-- (see diag section 3), comparing it to NVARCHAR converts the column on
-- every row. Casting the parameter once avoids that.
DECLARE @q varchar(255) = CAST(@queue AS varchar(255));

DECLARE @t datetime2 = SYSDATETIME(), @timing varchar(400) = '';

DROP TABLE IF EXISTS #cand, #vq, #matched, #conn;

/* Stage 1 - calls that haven't reached an agent. Driven from
   G_CALL_ACTIVE (live calls only), so G_PARTY is reached per CALLID
   instead of aggregating every open party row site-wide first. */
SELECT c.CALLID, c.CALLANI, c.CREATED AS CallCreated,
       MAX(CASE WHEN p.AGENTID > 0 THEN p.AGENTID END)         AS AgentID,
       MAX(CASE WHEN p.AGENTID > 0 THEN p.STATE END)           AS AgentState,
       MAX(CASE WHEN p.ENDPOINTTYPE = 4 THEN 1 ELSE 0 END)     AS InAcdQueue,
       MAX(CASE WHEN p.AGENTID > 0 OR p.ENDPOINTTYPE = 4
                THEN p.CREATED END)                            AS WaitLegCreated,
       MAX(CASE WHEN p.AGENTID IS NULL OR p.AGENTID = 0
                THEN p.ENDPOINTDN END)                         AS QueueEndpointDN
INTO #cand
FROM G_CALL_ACTIVE c
JOIN G_PARTY p ON p.CALLID = c.CALLID
WHERE p.TERMINATED IS NULL
GROUP BY c.CALLID, c.CALLANI, c.CREATED
HAVING MAX(CASE WHEN p.ENDPOINTTYPE = 4 THEN 1 ELSE 0 END) = 1
    OR MAX(CASE WHEN p.AGENTID > 0 THEN p.STATE END) IS NULL;

DECLARE @candRows int = @@ROWCOUNT;
CREATE CLUSTERED INDEX cx ON #cand (CALLID);
SET @timing += 'cand=' + CAST(DATEDIFF(MILLISECOND, @t, SYSDATETIME()) AS varchar)
             + 'ms(' + CAST(@candRows AS varchar) + ' calls) ';
SET @t = SYSDATETIME();

/* Stage 2 - the expensive part, now executed exactly once: first time
   each candidate was tagged with this VQ_Name / RP value, inside the
   10-minute window after its current wait leg started. */
SELECT c.CALLID, MIN(h.ADDED) AS FirstTagged
INTO #vq
FROM #cand c
JOIN G_USERDATA_HISTORY h ON h.CALLID = c.CALLID
WHERE h.VALUE = @q
  AND h.ADDED >= COALESCE(c.WaitLegCreated, c.CallCreated)
  AND h.ADDED <  DATEADD(MINUTE, 10, COALESCE(c.WaitLegCreated, c.CallCreated))
GROUP BY c.CALLID
OPTION (RECOMPILE);

SET @timing += 'vq=' + CAST(DATEDIFF(MILLISECOND, @t, SYSDATETIME()) AS varchar) + 'ms ';
SET @t = SYSDATETIME();

/* Stage 3 - matched by attached data OR by the DN it's parked at (VRP). */
SELECT c.*, vq.FirstTagged
INTO #matched
FROM #cand c
LEFT JOIN #vq vq ON vq.CALLID = c.CALLID
WHERE vq.CALLID IS NOT NULL OR c.QueueEndpointDN = @q;

/* Stage 4 - ConnID only for the handful of matched calls. */
SELECT h.CALLID, MIN(h.VALUE) AS ConnID
INTO #conn
FROM #matched m
JOIN G_USERDATA_HISTORY h ON h.CALLID = m.CALLID
WHERE h.KEYNAME = 'ConnectionID'
GROUP BY h.CALLID;

SET @timing += 'conn=' + CAST(DATEDIFF(MILLISECOND, @t, SYSDATETIME()) AS varchar) + 'ms';
PRINT @timing;   -- shows in the SSMS Messages tab

SELECT TOP (500)
    m.CALLID                                   AS CallID,
    conn.ConnID                                AS ConnID,
    m.CALLANI                                  AS ANI,
    @queue                                     AS VirtualQueue,
    CAST(COALESCE(m.FirstTagged, m.WaitLegCreated) AT TIME ZONE 'UTC'
         AT TIME ZONE 'Israel Standard Time' AS datetime2(0)) AS QueueEntered,
    DATEDIFF(second, COALESCE(m.FirstTagged, m.WaitLegCreated), GETUTCDATE()) AS WaitingSec,
    DATEDIFF(second, m.WaitLegCreated, GETUTCDATE()) AS InStateSec,
    m.AgentID                                  AS AgentID,
    CASE WHEN m.InAcdQueue = 1 THEN 'in acd queue' ELSE 'no agent leg' END AS Status
FROM #matched m
LEFT JOIN #conn conn ON conn.CALLID = m.CALLID
ORDER BY COALESCE(m.FirstTagged, m.WaitLegCreated) ASC;

DROP TABLE IF EXISTS #cand, #vq, #matched, #conn;
