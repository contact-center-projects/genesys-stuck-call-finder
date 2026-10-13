import time
import icon_stuck_calls as m

conn_str = input("Conn str: ").strip()

sql = ("SELECT VQDN, ORIGCALLID, CREATED, "
       "DATEDIFF(second, CREATED, GETDATE()) AS AgeSec "
       "FROM G_VIRTUAL_QUEUE WHERE TERMINATED IS NULL ORDER BY CREATED DESC")

print("Polling ALL queues for any open (TERMINATED IS NULL) row, every 2s.")
print("Watch CCP at the same time. Ctrl+C to stop.\n")

while True:
    try:
        headers, rows = m.run_query(conn_str, sql)
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] {len(rows)} open row(s) system-wide"
              + (f" - e.g. {rows[0]}" if rows else ""))
    except Exception as exc:
        print("ERROR:", exc)
    time.sleep(2)
