import logging
from pathlib import Path

LOG_FMT = (
    "[%(asctime)s] "
    "%(levelname)-8s "
    "[%(name)s] "
    "%(message)-70s "
    "(%(filename)s:%(lineno)d)"
)

COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[1;41m",
    "reset": "\033[0m",
}


class ColorFormatter(logging.Formatter):
    def format(self, record) -> str:
        color = COLORS.get(record.levelname, COLORS["reset"])
        levelname = record.levelname
        record.levelname = f"{color}{levelname:<8}{COLORS['reset']}"
        formatted = super().format(record)
        record.levelname = levelname

        return formatted


def get_logger(name: str | None = None) -> logging.Logger:
    if not name:
        name = Path(__file__).stem

    logger = logging.getLogger(name)

    if not logger.hasHandlers():
        handler = logging.StreamHandler()
        formatter = ColorFormatter(LOG_FMT, datefmt="%Y-%m-%d | %H:%M:%S")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

    return logger


__all__ = ["get_logger", "ColorFormatter"]
