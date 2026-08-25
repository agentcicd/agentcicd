from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

OptionValue = str | int | float | bool | None | tuple["OptionValue", ...] | dict[str, "OptionValue"]


@dataclass(frozen=True)
class StatementOptions(Mapping[str, OptionValue]):
    _values: dict[str, OptionValue] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> "StatementOptions":
        if not raw:
            return cls()
        values: dict[str, OptionValue] = {}
        for key, value in raw.items():
            normalized_key = str(key)
            values[normalized_key] = _normalize_option_value(value)
        return cls(values)

    def __getitem__(self, key: str) -> OptionValue:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def to_dict(self) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, value in self._values.items():
            normalized[key] = _option_value_to_json(value)
        return normalized

    def __eq__(self, other: object) -> bool:
        if isinstance(other, StatementOptions):
            return self._values == other._values
        if isinstance(other, Mapping):
            return self.to_dict() == StatementOptions.from_mapping(other).to_dict()
        return False


def _normalize_option_value(value: object) -> OptionValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return tuple(_normalize_option_value(item) for item in value)
    if isinstance(value, Mapping):
        return {str(key): _normalize_option_value(item) for key, item in value.items()}
    return str(value)


def _option_value_to_json(value: OptionValue) -> Any:
    if isinstance(value, tuple):
        return [_option_value_to_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _option_value_to_json(item) for key, item in value.items()}
    return value
