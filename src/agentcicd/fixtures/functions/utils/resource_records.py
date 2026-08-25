from __future__ import annotations

from typing import Any, Callable, Mapping


def normalize_aisystem_records(
    aisystems: Mapping[str, Any] | list[Any],
    *,
    record_factory: Callable[..., Any],
) -> dict[str, Any]:
    records: dict[str, Any] = {}
    items = aisystems.values() if isinstance(aisystems, Mapping) else aisystems
    for item in items:
        if not isinstance(item, Mapping):
            continue
        record = _normalize_aisystem_record(item, record_factory=record_factory)
        if record is not None:
            records[record.id] = record
    return records


def normalize_secret_records(
    secrets: Mapping[str, Any] | list[Any],
    *,
    record_factory: Callable[..., Any],
    litellm_options_factory: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    by_id: dict[str, Any] = {}
    by_key: dict[str, Any] = {}
    items = secrets.values() if isinstance(secrets, Mapping) else secrets
    for item in items:
        if not isinstance(item, Mapping):
            continue
        record = _normalize_secret_record(
            item,
            record_factory=record_factory,
            litellm_options_factory=litellm_options_factory,
        )
        if record is None:
            continue
        by_id[record.id] = record
        by_key[record.key] = record
    return by_id, by_key


def normalize_aisystem_secret_bindings(
    bindings: Mapping[str, Any] | list[Any] | None,
    *,
    record_factory: Callable[..., Any],
) -> tuple[Any, ...]:
    items = bindings.values() if isinstance(bindings, Mapping) else (bindings or [])
    records: list[Any] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        record = _normalize_aisystem_secret_binding(item, record_factory=record_factory)
        if record is not None:
            records.append(record)
    return tuple(records)


def normalize_secret_id_list(values: list[Any] | tuple[Any, ...] | None) -> tuple[str, ...]:
    return tuple(str(value).strip() for value in values or [] if str(value).strip())


def _normalize_aisystem_record(item: Mapping[str, Any], *, record_factory: Callable[..., Any]) -> Any | None:
    aisystem_id = str(item.get("id") or "").strip()
    target = str(item.get("target") or "").strip()
    if not aisystem_id or not target:
        return None

    interface_type = str(item.get("interface_type") or "").strip()
    interface = item.get("interface")
    selected_interface = dict(interface) if isinstance(interface, Mapping) else {}
    if not interface_type and isinstance(interface, Mapping):
        interface_type = str(interface.get("interface_type") or interface.get("interfaceType") or "").strip()

    interface_records, interface_type, selected_interface = _normalize_interfaces(
        item.get("interfaces"),
        interface_type=interface_type,
        selected_interface=selected_interface,
    )
    config = item.get("config")
    if not isinstance(config, Mapping):
        config = item.get("runtime_config") if isinstance(item.get("runtime_config"), Mapping) else {}

    return record_factory(
        id=aisystem_id,
        target=target,
        interface_type=interface_type,
        name=str(item.get("name") or "").strip(),
        interface=selected_interface,
        interfaces=tuple(interface_records),
        config=dict(config),
        secret_ids=_normalize_record_secret_ids(item),
    )


def _normalize_interfaces(
    value: Any,
    *,
    interface_type: str,
    selected_interface: dict[str, Any],
) -> tuple[list[Mapping[str, Any]], str, dict[str, Any]]:
    if not isinstance(value, list):
        return [], interface_type, selected_interface
    interface_records = [dict(item) for item in value if isinstance(item, Mapping)]
    if interface_type:
        return interface_records, interface_type, selected_interface
    for interface_item in interface_records:
        actual = str(interface_item.get("interface_type") or interface_item.get("interfaceType") or "").strip()
        if actual:
            return interface_records, actual, dict(interface_item)
    return interface_records, interface_type, selected_interface


def _normalize_record_secret_ids(item: Mapping[str, Any]) -> tuple[str, ...]:
    raw_secret_ids = item.get("secret_ids") or item.get("secretIds") or []
    if not isinstance(raw_secret_ids, list):
        return ()
    return tuple(str(value).strip() for value in raw_secret_ids if str(value).strip())


def _normalize_secret_record(
    item: Mapping[str, Any],
    *,
    record_factory: Callable[..., Any],
    litellm_options_factory: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> Any | None:
    secret_id = str(item.get("id") or "").strip()
    key = str(item.get("key") or "").strip()
    if not secret_id or not key:
        return None
    secret_payload = item.get("secret")
    if not isinstance(secret_payload, Mapping):
        secret_payload = item
    litellm_options = item.get("litellm_options")
    if not isinstance(litellm_options, Mapping):
        litellm_options = litellm_options_factory(secret_payload)
    return record_factory(
        id=secret_id,
        key=key,
        secret=dict(secret_payload),
        litellm_options=dict(litellm_options),
    )


def _normalize_aisystem_secret_binding(item: Mapping[str, Any], *, record_factory: Callable[..., Any]) -> Any | None:
    binding_id = str(item.get("id") or "").strip()
    aisystem_id = str(item.get("aisystem_id") or item.get("aisystemId") or "").strip()
    secret_id = str(item.get("secret_id") or item.get("secretId") or "").strip()
    if not binding_id or not aisystem_id or not secret_id:
        return None
    return record_factory(
        id=binding_id,
        organization_id=str(item.get("organization_id") or item.get("organizationId") or "").strip(),
        aisystem_id=aisystem_id,
        secret_id=secret_id,
        is_default=bool(item.get("is_default") or item.get("isDefault")),
        status=str(item.get("status") or "active"),
    )
