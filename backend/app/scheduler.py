"""Periodic public-source refresh for a single application process."""
import logging
import os
import threading
from datetime import datetime, timezone

log = logging.getLogger("conference_finder")
status = {"running": False, "last_started": None, "last_finished": None, "error": None}


def start_refresh_worker():
    interval = float(os.environ.get("CONFERENCE_FINDER_REFRESH_HOURS", "6")) * 3600
    stop = threading.Event()
    if interval <= 0:
        return stop, None

    def run():
        # Start serving immediately; populate data in the background, then
        # refresh periodically.
        delay = 1
        while not stop.wait(delay):
            status.update(running=True, last_started=datetime.now(timezone.utc).isoformat(), error=None)
            try:
                from .refresh import main
                results = main()
                failed = [name for name, result in results.items() if result is None or result.get("errors") or result.get("file_errors")]
                if failed:
                    status["error"] = "Failed sources: " + ", ".join(failed)
            except Exception as exc:
                log.exception("Periodic refresh failed")
                status["error"] = str(exc)
            finally:
                status.update(running=False, last_finished=datetime.now(timezone.utc).isoformat())
            delay = interval

    thread = threading.Thread(target=run, name="venue-refresh", daemon=True)
    thread.start()
    return stop, thread
