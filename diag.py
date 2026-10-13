import icon_stuck_calls as m

m.PS_SCRIPT = m.PS_SCRIPT.replace("CommandTimeout = 75", "CommandTimeout = 90")

conn_str = input("Conn str: ").strip()

# Same fast pattern as before: find waiting/queued calls cheaply first (small
# set), then look up BOTH VQ_Name (URS) and RouteRule (possible ORS
# equivalent) only for those - not a broad history scan.
sql = """
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
WITH Parties AS (
    SELECT p.CALLID,
        MAX(CASE WHEN p.AGENTID > 0 THEN p.STATE END)        AS AgentState,
        MAX(CASE WHEN p.ENDPOINTTYPE = 4 THEN 1 ELSE 0 END)  AS InAcdQueue
    FROM G_PARTY p WHERE p.TERMINATED IS NULL GROUP BY p.CALLID
),
Candidates AS (
    SELECT c.CALLID, c.CONNID, c.CALLANI
    FROM G_CALL_ACTIVE c
    JOIN Parties pt ON pt.CALLID = c.CALLID
    WHERE pt.InAcdQueue = 1 OR pt.AgentState IS NULL
),
Tagged AS (
    SELECT h.CALLID, h.KEYNAME, h.VALUE,
           ROW_NUMBER() OVER (PARTITION BY h.CALLID, h.KEYNAME ORDER BY h.ADDED DESC) rn
    FROM G_USERDATA_HISTORY h
    WHERE h.CALLID IN (SELECT CALLID FROM Candidates)
      AND h.KEYNAME IN ('VQ_Name', 'RouteRule', 'Service')
)
SELECT cand.CALLID, cand.CONNID, cand.CALLANI,
       vq.VALUE AS VQName, rr.VALUE AS RouteRule, svc.VALUE AS Service
FROM Candidates cand
LEFT JOIN Tagged vq  ON vq.CALLID=cand.CALLID  AND vq.KEYNAME='VQ_Name'    AND vq.rn=1
LEFT JOIN Tagged rr  ON rr.CALLID=cand.CALLID  AND rr.KEYNAME='RouteRule'  AND rr.rn=1
LEFT JOIN Tagged svc ON svc.CALLID=cand.CALLID AND svc.KEYNAME='Service'   AND svc.rn=1
ORDER BY VQName, RouteRule;
"""

print("=== Every waiting/queued call right now: VQ_Name vs RouteRule vs Service ===")
try:
    headers, rows = m.run_query(conn_str, sql, timeout=100)
    print(headers)
    for r in rows:
        print(r)
except RuntimeError as e:
    print("ERROR:", e)
