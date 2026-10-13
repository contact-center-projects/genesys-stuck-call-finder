import time
import icon_stuck_calls as m

conn_str = input("Conn str: ").strip()
queue = input("Virtual Queue name (exact): ").strip()

print("Polling every 2 seconds. Ctrl+C to stop.")
print("Make/hold a test call into this queue now and watch for a row to appear.\n")

while True:
    try:
        headers, rows = m.run_query(conn_str, m.DEFAULT_QUERY, queue=queue)
        stamp = time.strftime("%H:%M:%S")
        if rows:
            print(f"[{stamp}] {len(rows)} row(s):")
            for r in rows:
                print("   ", r)
        else:
            print(f"[{stamp}] 0 rows")
    except Exception as exc:
        print("ERROR:", exc)
    time.sleep(2)
