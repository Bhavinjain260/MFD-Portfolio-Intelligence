from __future__ import annotations
import logging
import os
import signal
import sys
import time
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from app.core.config import settings
from app.workers.tasks import poll_and_run_manual_jobs, run_all, task_bse

os.environ.setdefault("TZ", settings.tz)
try: time.tzset()
except AttributeError: pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s | %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("worker")
_sched = BlockingScheduler(timezone=settings.tz)

def _stop(signum, _frame):
    log.info("Signal %s received — shutting down", signum)
    try: _sched.shutdown(wait=False)
    except Exception: pass

def main() -> None:
    # Composite run at 11:00, 15:00, and 23:00
    _sched.add_job(
        run_all, CronTrigger(hour="11,15,23", minute=0),
        id="main", max_instances=1, coalesce=True, misfire_grace_time=3600,
        kwargs={"trigger": "cron"}, replace_existing=True,
    )
    _sched.add_job(
        task_bse, CronTrigger(hour=9, minute=30),
        id="bse", max_instances=1, coalesce=True, misfire_grace_time=3600,
        kwargs={"trigger": "cron"}, replace_existing=True,
    )
    _sched.add_job(
        poll_and_run_manual_jobs, IntervalTrigger(seconds=10),
        id="manual_poll", max_instances=1, coalesce=True, replace_existing=True,
    )

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("Scheduler starting. Jobs: %s", [j.id for j in _sched.get_jobs()])
    _sched.start()

if __name__ == "__main__":
    main()