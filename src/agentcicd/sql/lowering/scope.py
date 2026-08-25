from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, Set

from sqlglot import expressions as exp

from agentcicd.sql.ir.metadata import CellComponentsIR


ColumnRepresentation = Literal["cell", "raw"]


@dataclass(frozen=True)
class AgentCICDValueType:
    kind: str = "unknown"
    spark_type: str | None = None
    nullable: bool = True
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ColumnBinding:
    name: str
    qualifier: str | None = None
    representation: ColumnRepresentation = "cell"
    value_type: AgentCICDValueType = field(default_factory=AgentCICDValueType)
    value_sql: exp.Expression | None = None
    cell_sql: exp.Expression | None = None
    error_sql: exp.Expression | None = None
    latency_sql: exp.Expression | None = None
    source: str = "table"

    def components(self) -> CellComponentsIR:
        value_sql = self.value_sql or (
            _parse_scalar(f"{self.sql_name}.value")
            if self.representation == "cell"
            else _parse_scalar(self.sql_name)
        )
        return CellComponentsIR(
            value_sql=value_sql.copy(),
            error_sql=(self.error_sql or _parse_scalar(f"{self.sql_name}.metadata.errors")).copy()
            if self.representation == "cell"
            else exp.Array(expressions=[]),
            latency_sql=(self.latency_sql or _parse_scalar(f"{self.sql_name}.metadata.latency_ms")).copy()
            if self.representation == "cell"
            else exp.Null(),
            cell_sql=(self.cell_sql or _parse_scalar(self.sql_name)).copy()
            if self.representation == "cell"
            else None,
            representation=self.representation,
        )

    @property
    def sql_name(self) -> str:
        return f"{self.qualifier}.{self.name}" if self.qualifier else self.name


@dataclass
class LoweringScope:
    bindings: dict[str, ColumnBinding] = field(default_factory=dict)
    variant_columns: set[str] = field(default_factory=set)
    non_cell_columns: set[str] = field(default_factory=set)

    @classmethod
    def bridge(
        cls,
        *,
        variant_columns: Optional[Set[str]] = None,
        non_cell_columns: Optional[Set[str]] = None,
    ) -> "LoweringScope":
        return cls(
            variant_columns={item.lower() for item in (variant_columns or set())},
            non_cell_columns={item.lower() for item in (non_cell_columns or set())},
        )

    def resolve(self, name: str) -> ColumnBinding | None:
        key = name.lower()
        if key in self.bindings:
            return self.bindings[key]
        if key in self.non_cell_columns:
            return ColumnBinding(name=name, representation="raw", source="input")
        return None

    def bind(self, binding: ColumnBinding) -> None:
        keys = [binding.name.lower()]
        if binding.qualifier:
            keys.append(f"{binding.qualifier.lower()}.{binding.name.lower()}")
        for key in keys:
            existing = self.bindings.get(key)
            if existing is not None and existing != binding:
                raise ValueError(f"Ambiguous wrapped-mode column binding for '{key}'")
            self.bindings[key] = binding

    def child(self) -> "LoweringScope":
        return LoweringScope(
            bindings=dict(self.bindings),
            variant_columns=set(self.variant_columns),
            non_cell_columns=set(self.non_cell_columns),
        )


def _parse_scalar(sql_text: str) -> exp.Expression:
    import sqlglot

    parsed = sqlglot.parse_one(f"SELECT {sql_text}", read="spark")
    return parsed.expressions[0]
