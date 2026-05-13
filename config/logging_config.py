"""Logging setup — call configure_logging() once at startup."""
import logging
import logging.handlers
from config.settings import LOG_DIR, LOG_LEVEL, LOG_ROTATION, LOG_BACKUP_COUNT


def configure_logging(name: str = "nsebot") -> None:
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    if root.handlers:
        return  # already configured
    root.setLevel(getattr(logging, LOG_LEVEL))

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    fh = logging.handlers.TimedRotatingFileHandler(
        LOG_DIR / f"{name}.log",
        when=LOG_ROTATION,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
