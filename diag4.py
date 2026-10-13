import icon_stuck_calls as m

conn_str = input("Conn str: ").strip()

checks = [
    ("How stale is G_VIRTUAL_QUEUE? (latest CREATED vs now)",
     "SELECT MAX(CREATED) AS LatestVQRow, GETDATE() AS DbNow, "
     "DATEDIFF(second, MAX(CREATED), GETDATE()) AS LagSeconds FROM G_VIRTUAL_QUEUE"),
    ("How stale is G_CALL_ACTIVE? (latest CREATED vs now)",
     "SELECT MAX(CREATED) AS LatestCallRow, GETDATE() AS DbNow, "
     "DATEDIFF(second, MAX(CREATED), GETDATE()) AS LagSeconds FROM G_CALL_ACTIVE"),
    ("Any VQ rows right now with TERMINATED IS NULL (any queue)",
     "SELECT TOP 20 VQDN, ORIGCALLID, CREATED FROM G_VIRTUAL_QUEUE "
     "WHERE TERMINATED IS NULL ORDER BY CREATED DESC"),
]

for label, sql in checks:
    print(f"\n=== {label} ===")
    headers, rows = m.run_query(conn_str, sql)
    print(headers)
    for r in rows:
        print(r)
