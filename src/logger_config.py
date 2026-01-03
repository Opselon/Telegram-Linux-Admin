"""
Modern Structured Logging Configuration (2026 Standards)
Provides structured logging with context and better observability.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any
import json
from datetime import datetime


class StructuredFormatter(logging.Formatter):
    """
    Modern structured JSON formatter for better log aggregation (2026 standards).
    Falls back to human-readable format for console.
    """
    
    def __init__(self, use_json: bool = False):
        super().__init__()
        self.use_json = use_json
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record as structured data."""
        if self.use_json:
            log_data = {
                "timestamp": datetime.fromtimestamp(record.created).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "module": record.module,
                "function": record.funcName,
                "line": record.lineno,
            }
            
            # Add exception info if present
            if record.exc_info:
                log_data["exception"] = self.formatException(record.exc_info)
            
            # Add extra context
            for key, value in record.__dict__.items():
                if key.startswith("ctx_"):
                    log_data[key[4:]] = value
            
            return json.dumps(log_data, ensure_ascii=False)
        else:
            # Human-readable format
            return super().format(record)


def setup_logging(
    level: int = logging.INFO,
    use_json: bool = False,
    log_file: Path | None = None,
) -> None:
    """
    Setup modern structured logging (2026 standards).
    
    Args:
        level: Logging level
        use_json: Use JSON format for structured logging
        log_file: Optional log file path
    """
    handlers: list[logging.Handler] = []
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(StructuredFormatter(use_json=False))
    handlers.append(console_handler)
    
    # File handler if specified
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setFormatter(StructuredFormatter(use_json=use_json))
        handlers.append(file_handler)
    
    # Configure root logger
    logging.basicConfig(
        level=level,
        handlers=handlers,
        force=True  # Override existing configuration
    )


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the given name."""
    return logging.getLogger(name)

