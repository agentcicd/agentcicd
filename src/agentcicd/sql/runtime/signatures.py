from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeControlArguments:
    indexes: frozenset[int]

    def data_values(self, values: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(value for index, value in enumerate(values) if index not in self.indexes)

    def control_values(self, values: tuple[Any, ...]) -> list[Any]:
        return [values[index] for index in self.indexes if index < len(values)]


def control_argument_indexes(parameters: list[Any]) -> RuntimeControlArguments:
    indexes = {
        index
        for index, parameter in enumerate(parameters)
        if str(getattr(parameter, "type_sql", "") or "").strip().upper() in {"RATELIMIT", "POOL"}
    }
    return RuntimeControlArguments(frozenset(indexes))
