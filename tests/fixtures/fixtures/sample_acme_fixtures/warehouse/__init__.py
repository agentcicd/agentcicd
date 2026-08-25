from __future__ import annotations

from agentcicd.fixtures import Environment, Int, NamedStruct, Required, Str, environment, function


class WarehouseSpec(NamedStruct):
    database: Required[Str]
    schema: Str


@environment
class Warehouse(Environment[WarehouseSpec]):
    def __init__(self, spec: WarehouseSpec) -> None:
        self.spec = spec

    @function
    def count_rows(self, table: Str, token: Str) -> Int:
        return 1
