import icon_stuck_calls as m

conn_str = input("Conn str: ").strip()
queue = input("Virtual Queue name: ").strip()

base_query = """
WITH LastVQ AS (
    SELECT vq.ORIGCALLID, vq.VQDN, vq.CREATED,
           ROW_NUMBER() OVER (PARTITION BY vq.ORIGCALLID
                               ORDER BY vq.CREATED DESC) AS rn
    FROM G_VIRTUAL_QUEUE vq
)
SELECT TOP 10
    c.CALLID, c.CONNID, lv.VQDN, lv.CREATED,
    DATEDIFF(second, lv.CREATED, GETDATE()) AS WaitingSec
FROM G_CALL_ACTIVE c
JOIN LastVQ lv ON lv.ORIGCALLID = c.CALLID AND lv.rn = 1
WHERE lv.VQDN = @queue
ORDER BY lv.CREATED ASC
"""

print("=== Base join (no G_PARTY filter) ===")
headers, rows = m.run_query(conn_str, base_query, queue=queue)
print(headers)
callids = []
for r in rows:
    print(r)
    callids.append(r[0])

if callids:
    print("\n=== G_PARTY rows for those CALLIDs ===")
    ids = ",".join(f"'{c}'" for c in callids)
    party_query = f"""
    SELECT CALLID, PARTYID, TYPE, PROLE, ENDPOINTDN, AGENTID, CREATED, TERMINATED
    FROM G_PARTY
    WHERE CALLID IN ({ids})
    ORDER BY CALLID, CREATED
    """
    headers, rows = m.run_query(conn_str, party_query)
    print(headers)
    for r in rows:
        print(r)
else:
    print("No rows from base join - the queue name or VQ join itself is the problem, not G_PARTY.")
