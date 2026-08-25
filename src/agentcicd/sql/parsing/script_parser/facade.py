from __future__ import annotations

from typing import Any, Dict, List, Optional

from agentcicd.sql.contracts import RegisteredRuntimeFunction
from agentcicd.sql.ir.functions import coerce_registered_runtime_specs
from agentcicd.sql.parsing.runtime_signature_registry import register_runtime_signature_specs
from agentcicd.sql.parsing.script_parser.binding import AgentCICDScriptParserBindingMixin
from agentcicd.sql.parsing.script_parser.core import AgentCICDScriptParserCoreMixin
from agentcicd.sql.parsing.script_parser.functions import AgentCICDScriptParserFunctionMixin
from agentcicd.sql.parsing.script_parser.segments import AgentCICDScriptParserSegmentMixin


class AgentCICDScriptParser(
    AgentCICDScriptParserCoreMixin,
    AgentCICDScriptParserFunctionMixin,
    AgentCICDScriptParserSegmentMixin,
    AgentCICDScriptParserBindingMixin,
):
    def __init__(
        self,
        script: str,
        *,
        enable_sql_transpile: bool = False,
        enable_function_semantics: bool = True,
        registered_functions: Optional[List[RegisteredRuntimeFunction | Dict[str, Any]]] = None,
    ):
        self._script = script
        self._functions: Dict[str, Any] = {}
        self._enable_sql_transpile = enable_sql_transpile
        self._enable_function_semantics = enable_function_semantics
        runtime_functions = coerce_registered_runtime_specs(registered_functions or [])
        register_runtime_signature_specs(runtime_functions)
        self._registered_functions = self._build_registered_function_index(runtime_functions)
        self._register_builtin_function_signatures()
