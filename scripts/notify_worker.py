"""Email worker: deadline reminders (teams), review + peer-review due/overdue reminders (reviewers), delivery-failure alerts (admins), and outbox delivery with retries.

  python scripts/notify_worker.py --once            # one pass (use from cron / Task Scheduler)
  python scripts/notify_worker.py --loop 300        # keep running, one pass every 300 s
"""
import argparse
import json
import time

import _path  # noqa: F401
from notifications.worker import one_pass
from workflow.context import open_app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true")
    g.add_argument("--loop", type=int, metavar="SECONDS")
    a = ap.parse_args()
    conn, settings, base = open_app()
    while True:
        print(json.dumps(one_pass(conn, settings, base)), flush=True)
        if a.once:
            break
        time.sleep(max(a.loop, 30))
