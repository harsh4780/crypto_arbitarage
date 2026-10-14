"""
Day-by-Day Rotating High-Granularity Logging System
===================================================
Generates clean daily log files (logs/trade_YYYY-MM-DD.log) capturing every single
execution detail, tick anomaly, broker rejection, and latency benchmark.
Provides reading/filtering utilities for the web dashboard log inspector.
"""

import os
import glob
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
from typing import List, Dict, Any, Optional

LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

class DailyFileHandler(logging.FileHandler):
    """FileHandler that automatically routes to the current day's filename (trade_YYYY-MM-DD.log)."""
    def __init__(self, directory: str = LOGS_DIR, encoding: str = "utf-8"):
        self.directory = directory
        self.current_date = datetime.now().strftime("%Y-%m-%d")
        filepath = os.path.join(self.directory, f"trade_{self.current_date}.log")
        super().__init__(filepath, mode="a", encoding=encoding)

    def emit(self, record):
        now_date = datetime.now().strftime("%Y-%m-%d")
        if now_date != self.current_date:
            self.current_date = now_date
            self.close()
            self.baseFilename = os.path.join(self.directory, f"trade_{self.current_date}.log")
            self.stream = self._open()
        super().emit(record)

def setup_system_logging(log_level: int = logging.INFO) -> logging.Logger:
    """Configures the root and application loggers with high granularity and daily file rotation."""
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Detailed formatter with milliseconds, module, line number and thread
    formatter = logging.Formatter(
        fmt="[%(asctime)s.%(msecs)03d] [%(levelname)-7s] [%(name)s:%(funcName)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Check if DailyFileHandler already added
    has_daily_handler = any(isinstance(h, DailyFileHandler) for h in root_logger.handlers)
    if not has_daily_handler:
        daily_handler = DailyFileHandler(LOGS_DIR)
        daily_handler.setFormatter(formatter)
        daily_handler.setLevel(logging.DEBUG)
        root_logger.addHandler(daily_handler)

    # Console Handler for real-time terminal output
    has_stream_handler = any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root_logger.handlers)
    if not has_stream_handler:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(log_level)
        root_logger.addHandler(console_handler)

    logger = logging.getLogger("arbitrage")
    logger.info("=" * 70)
    logger.info("Arbitrage Logging System Initialized - Rotating Daily Log Active")
    logger.info("=" * 70)
    return logger

def get_available_log_dates() -> List[str]:
    """Returns list of dates available in logs directory sorted newest first."""
    files = glob.glob(os.path.join(LOGS_DIR, "trade_*.log"))
    dates = []
    for f in files:
        basename = os.path.basename(f)
        # expected trade_YYYY-MM-DD.log
        if basename.startswith("trade_") and basename.endswith(".log"):
            date_part = basename[6:-4]
            dates.append(date_part)
    
    # Also check if today's log exists or should be listed
    today = datetime.now().strftime("%Y-%m-%d")
    if today not in dates:
        dates.append(today)

    dates.sort(reverse=True)
    return dates

def read_log_file(date_str: str, max_lines: int = 500, level_filter: Optional[str] = None, search: Optional[str] = None) -> Dict[str, Any]:
    """Reads lines from trade_YYYY-MM-DD.log with optional filtering."""
    filename = f"trade_{date_str}.log"
    filepath = os.path.join(LOGS_DIR, filename)

    if not os.path.exists(filepath):
        return {
            "date": date_str,
            "filename": filename,
            "exists": False,
            "total_lines": 0,
            "lines": []
        }

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        filtered_lines = []
        filter_lvl = level_filter.upper() if level_filter and level_filter.upper() != "ALL" else None
        search_lower = search.lower() if search else None

        for line in reversed(all_lines):
            line_str = line.strip()
            if not line_str:
                continue

            if filter_lvl and f"[{filter_lvl}" not in line_str:
                continue

            if search_lower and search_lower not in line_str.lower():
                continue

            filtered_lines.append(line_str)
            if len(filtered_lines) >= max_lines:
                break

        return {
            "date": date_str,
            "filename": filename,
            "exists": True,
            "total_lines": len(all_lines),
            "matched_lines": len(filtered_lines),
            "lines": filtered_lines # Newest first
        }
    except Exception as e:
        return {
            "date": date_str,
            "filename": filename,
            "exists": True,
            "error": str(e),
            "lines": []
        }
