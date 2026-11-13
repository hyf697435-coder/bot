import os
import glob

workspace = r"C:\Users\Administrator\Desktop\v1\a"

print("=== Checking dashboard.py for B' or slice artifacts ===")
with open(os.path.join(workspace, "dashboard.py"), encoding="utf-8", errors="ignore") as f:
    for i, line in enumerate(f):
        if "b'" in line or "B'" in line or "b\"" in line:
            print(f"dashboard.py:{i+1}: {line.strip()[:100]}")

print("\n=== Checking log for B' ===")
log_path = r"C:\Users\Administrator\Desktop\v1\log\trading_bot.log"
if os.path.exists(log_path):
    with open(log_path, encoding="utf-8", errors="ignore") as f:
        for i, line in enumerate(f):
            if "b'" in line.lower() or "b\"" in line.lower():
                print(f"log:{i+1}: {line.strip()[:100]}")
