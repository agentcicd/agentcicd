import logging
from pathlib import Path
from typing import Any


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEBUG_LOG_FILE = "debug.log"
CONSOLE_HANDLER_NAME = "agentcicd_console"
PLAIN_FILE_HANDLER_NAME = "agentcicd_plain_file"
DEBUG_FILE_HANDLER_NAME = "agentcicd_debug_file"
OBJECT_STORE_HANDLER_NAME = "agentcicd_object_store"


class PlainTextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        original_exc_info = record.exc_info
        original_exc_text = record.exc_text
        original_stack_info = record.stack_info
        try:
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            return super().format(record)
        finally:
            record.exc_info = original_exc_info
            record.exc_text = original_exc_text
            record.stack_info = original_stack_info


class ObjectStoreTextHandler(logging.Handler):
    def __init__(self, *, log_uri: str, store: Any) -> None:
        super().__init__(logging.INFO)
        self.log_uri = log_uri
        self._store = store
        self._lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._lines.append(self.format(record))
            self._store.put_text(
                self.log_uri,
                "\n".join(self._lines) + "\n",
                content_type="text/plain; charset=utf-8",
            )
        except Exception:
            self.handleError(record)


def configure_application_logging(working_dir: Path, *, primary_log_name: str) -> Path:
    logs_dir = working_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    _ensure_stream_handler(root_logger)
    _ensure_plain_file_handler(root_logger, logs_dir / primary_log_name)
    debug_log_path = logs_dir / DEBUG_LOG_FILE
    _ensure_debug_file_handler(root_logger, debug_log_path)
    _quiet_dependency_loggers()
    return debug_log_path


def configure_object_store_logging(*, log_uri: str, store: Any) -> None:
    if not log_uri.strip():
        return

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    resolved_uri = log_uri.rstrip("/")
    for handler in root_logger.handlers:
        if handler.get_name() != OBJECT_STORE_HANDLER_NAME:
            continue
        if isinstance(handler, ObjectStoreTextHandler) and handler.log_uri == resolved_uri:
            return

    handler = ObjectStoreTextHandler(log_uri=resolved_uri, store=store)
    handler.set_name(OBJECT_STORE_HANDLER_NAME)
    handler.setLevel(logging.INFO)
    handler.setFormatter(PlainTextFormatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    root_logger.addHandler(handler)


def _ensure_stream_handler(root_logger: logging.Logger) -> None:
    for handler in root_logger.handlers:
        if handler.get_name() == CONSOLE_HANDLER_NAME:
            return

    handler = logging.StreamHandler()
    handler.set_name(CONSOLE_HANDLER_NAME)
    handler.setLevel(logging.INFO)
    handler.setFormatter(PlainTextFormatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    root_logger.addHandler(handler)


def _ensure_plain_file_handler(root_logger: logging.Logger, log_path: Path) -> None:
    resolved_path = log_path.resolve()
    for handler in root_logger.handlers:
        if handler.get_name() != PLAIN_FILE_HANDLER_NAME:
            continue
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).resolve() == resolved_path:
            return

    handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    handler.set_name(PLAIN_FILE_HANDLER_NAME)
    handler.setLevel(logging.INFO)
    handler.setFormatter(PlainTextFormatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    root_logger.addHandler(handler)


def _ensure_debug_file_handler(root_logger: logging.Logger, log_path: Path) -> None:
    resolved_path = log_path.resolve()
    for handler in root_logger.handlers:
        if handler.get_name() != DEBUG_FILE_HANDLER_NAME:
            continue
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).resolve() == resolved_path:
            return

    handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    handler.set_name(DEBUG_FILE_HANDLER_NAME)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    root_logger.addHandler(handler)


def _quiet_dependency_loggers() -> None:
    for logger_name in (
        "py4j",
        "pyspark",
        "pyspark.java_gateway",
        "pyspark.sql.connect",
        "org.apache.spark",
        "org.sparkproject",
    ):
        logging.getLogger(logger_name).setLevel(logging.WARN)
