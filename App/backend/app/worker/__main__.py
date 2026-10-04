"""Entry point: `python -m app.worker`. SIGTERM/SIGINT trigger a graceful shutdown."""

import logging
import signal

from app.config import settings
from app.db import SessionLocal
from app.logging_config import configure_logging
from app.queue import JobQueue
from app.worker.worker import Worker


def main() -> None:
    configure_logging(settings.log_level)
    worker = Worker(SessionLocal, JobQueue.from_url(settings.redis_url, settings.queue_prefix))

    def on_signal(signum, _frame):
        logging.getLogger("jobq.worker").info("received signal", extra={"signal": signal.Signals(signum).name})
        worker.request_stop()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    worker.start()
    # Short waits keep the main thread responsive to signals.
    while not worker.wait_for_stop(0.5):
        pass
    worker.stop()


if __name__ == "__main__":
    main()
