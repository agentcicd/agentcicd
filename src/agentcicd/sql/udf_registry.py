import logging
from collections.abc import Mapping
from typing import Optional

from agentcicd.sql.runtime.udf_compat.udf import Udf

logger = logging.getLogger(__name__)

_REGISTERED_UDFS: dict[str, type[Udf]] = {}
_IMPLEMENTATION_UDF_PREFIXES = ("agentcicd.", "sql.")
BUILTIN_UDF_EXECUTION_RUNTIME = "function_runner"


def canonical_udf_name(udf_name: str) -> str:
    normalized = udf_name.strip()
    for prefix in _IMPLEMENTATION_UDF_PREFIXES:
        if normalized.startswith(prefix):
            return normalized[len(prefix) :]
    return normalized


def register_udf(udf_cls: type[Udf], udf_name: Optional[str] = None) -> str:
    source_name = udf_name or udf_cls._udf_name
    resolved_name = canonical_udf_name(source_name) if source_name else source_name
    if not resolved_name:
        raise ValueError(f"UDF class {udf_cls.__name__} must define a registration name.")

    existing = _REGISTERED_UDFS.get(resolved_name)
    if existing is not None and existing is not udf_cls:
        raise ValueError(f"UDF with name '{resolved_name}' is already registered")

    _REGISTERED_UDFS[resolved_name] = udf_cls
    return resolved_name


def get_registered_udf(udf_name: str) -> Optional[type[Udf]]:
    return _REGISTERED_UDFS.get(udf_name)


def list_registered_udfs() -> dict[str, type[Udf]]:
    return _REGISTERED_UDFS.copy()


def registered_udf_name(udf_cls: type[Udf]) -> Optional[str]:
    for udf_name, registered_cls in _REGISTERED_UDFS.items():
        if registered_cls is udf_cls:
            return udf_name
    return None


def clear_registered_udfs() -> None:
    _REGISTERED_UDFS.clear()


def load_builtin_udfs() -> dict[str, type[Udf]]:
    return list_registered_udfs()


def builtin_udf_metadata(udf_name: str, udf_cls: type[Udf] | None = None) -> dict[str, object]:
    resolved_cls = udf_cls or get_registered_udf(udf_name)
    metadata: dict[str, object] = {}
    if resolved_cls is not None:
        try:
            raw_metadata = resolved_cls().metadata()
        except Exception as exc:
            logger.warning("Could not inspect metadata for built-in UDF %s: %s", udf_name, exc)
        else:
            if isinstance(raw_metadata, Mapping):
                metadata.update(dict(raw_metadata))

    metadata.setdefault("execution_runtime", BUILTIN_UDF_EXECUTION_RUNTIME)
    metadata.setdefault("entrypoint_name", _default_builtin_entrypoint_name(udf_name))
    return metadata


def _default_builtin_entrypoint_name(udf_name: str) -> str:
    normalized = canonical_udf_name(udf_name)
    return normalized.rsplit(".", 1)[-1].strip() or normalized.replace(".", "_")


def _register_named_udf_subclasses() -> None:
    for udf_cls in _iter_udf_subclasses(Udf):
        udf_name = udf_cls._udf_name
        if udf_name:
            register_udf(udf_cls, udf_name)


def _iter_udf_subclasses(base_cls: type[Udf]) -> list[type[Udf]]:
    discovered: list[type[Udf]] = []
    for subclass in base_cls.__subclasses__():
        discovered.append(subclass)
        discovered.extend(_iter_udf_subclasses(subclass))
    return discovered


def _import_builtin_udf_modules() -> None:
    return None
