"""Create the schema: `python -m app.migrate`. Run once by the `migrate` compose service before
the API and workers start. (A production setup would use Alembic migrations instead.)"""

import logging
import time

from sqlalchemy import text

from app import models  # noqa: F401  (registers tables on Base.metadata)
from app.config import settings
from app.db import Base, engine
from app.logging_config import configure_logging

log = logging.getLogger("jobq.migrate")


def main(retries: int = 30) -> None:
    configure_logging(settings.log_level)
    for attempt in range(1, retries + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            break
        except Exception as exc:
            log.info("waiting for database", extra={"attempt": attempt, "error": str(exc)})
            time.sleep(1)
    else:
        raise SystemExit("database not reachable")
    Base.metadata.create_all(engine)
    log.info("schema ready", extra={"tables": sorted(Base.metadata.tables)})


if __name__ == "__main__":
    main()
