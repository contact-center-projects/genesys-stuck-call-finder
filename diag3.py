import icon_stuck_calls as m

conn_str = input("Conn str: ").strip()
queue = input("Virtual Queue name (exact): ").strip()

checks = [
    ("Recent G_VIRTUAL_QUEUE rows for this VQDN",
     "SELECT TOP 10 ORIGCALLID, VQDN, CREATED, TERMINATED "
     "FROM G_VIRTUAL_QUEUE WHERE VQDN = @queue ORDER BY CREATED DESC"),
    ("Distinct VQDN values seen recently (spelling check)",
     "SELECT TOP 30 VQDN, MAX(CREATED) AS LastSeen FROM G_VIRTUAL_QUEUE "
     "GROUP BY VQDN ORDER BY LastSeen DESC"),
]

for label, sql in checks:
    print(f"\n=== {label} ===")
    headers, rows = m.run_query(conn_str, sql, queue=queue)
    print(headers)
    for r in rows:
        print(r)
