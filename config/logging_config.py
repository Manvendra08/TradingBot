"""Logging setup — call configure_logging() once at startup."""
import logging
import logging.handlers
import os

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

    log_path = LOG_DIR / f"{name}.log"

    # Windows note:
    # TimedRotatingFileHandler performs renames on rollover (via doRollover()).
    # On Windows, this often fails with WinError 32 if the file is still opened
    # by another thread/process. To prevent scheduler startup from being spammed
    # with logging errors, use a size-based handler on Windows.
    if os.name == "nt":
        fh = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    else:
        fh = logging.handlers.TimedRotatingFileHandler(
            log_path,
            when=LOG_ROTATION,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )

    fh.setFormatter(fmt)
    root.addHandler(fh)
