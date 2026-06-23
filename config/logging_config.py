"""Logging setup — call configure_logging() once at startup."""
import logging
import logging.handlers
import os

from config.settings import LOG_DIR, LOG_LEVEL, LOG_ROTATION, LOG_BACKUP_COUNT


class ColoredFormatter(logging.Formatter):
    """Custom logging formatter that adds ANSI color codes for terminal output."""
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BOLD_RED = "\033[1;31m"
    RESET = "\033[0m"
    
    COLORS = {
        logging.DEBUG: CYAN,
        logging.INFO: GREEN,
        logging.WARNING: YELLOW,
        logging.ERROR: RED,
        logging.CRITICAL: BOLD_RED,
    }

    def __init__(self, fmt=None, datefmt=None, style='%', use_color=True):
        super().__init__(fmt, datefmt, style)
        self.use_color = use_color

    def format(self, record):
        if not self.use_color:
            return super().format(record)
            
        color = self.COLORS.get(record.levelno, self.RESET)
        orig_levelname = record.levelname
        padded_levelname = f"{orig_levelname:<8}"
        record.levelname = f"{color}{padded_levelname}{self.RESET}"
        try:
            result = super().format(record)
        finally:
            record.levelname = orig_levelname
        return result


def configure_logging(name: str = "nsebot") -> None:
    root = logging.getLogger()
    if root.handlers:
        return  # already configured
    root.setLevel(getattr(logging, LOG_LEVEL))

    ch = logging.StreamHandler()
    use_color = ch.stream.isatty() if hasattr(ch.stream, "isatty") else False
    
    if use_color and os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h = kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                kernel32.SetConsoleMode(h, mode.value | 0x0004)
        except Exception:
            pass

    ch_fmt = ColoredFormatter(
        "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        use_color=use_color
    )
    ch.setFormatter(ch_fmt)
    root.addHandler(ch)

    file_fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

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

    fh.setFormatter(file_fmt)
    root.addHandler(fh)
