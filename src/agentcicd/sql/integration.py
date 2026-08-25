from __future__ import annotations

import re
from typing import Mapping, Optional
from xml.etree import ElementTree

from agentcicd.sql.contracts import RegisteredRuntimeFunction
from agentcicd.sql.engine.entrypoint import EngineEntrypoint
from agentcicd.sql.ir.expressions import CallExpr, SqlAstExpr
from agentcicd.sql.ir.functions import RegisteredFunctionSpec, coerce_registered_function_specs
from agentcicd.sql.ir.statements import DeclareInputStmt, PublishAnnotationStmt, QueryStmt, SqlFunctionDefStmt, StatementIR
from agentcicd.sql.ir.visitors import walk_ir
from agentcicd.sql.semantics.dependency_graph import _find_function_calls
from agentcicd.sql.semantics.registry import build_function_registry

_SUBMIT_CONTROL_TAGS = {"submit", "submitbutton"}
_SUBMIT_CONTROL_LABEL_PATTERN = re.compile(r"\b(submit|review|done|complete)\b", re.IGNORECASE)


def validate_script_text(
    script: str,
    *,
    registered_functions: Optional[list[RegisteredRuntimeFunction | RegisteredFunctionSpec | Mapping[str, object]]] = None,
) -> None:
    _reject_trailing_projection_commas(script)
    entrypoint = EngineEntrypoint(script, registered_functions=registered_functions)
    statements = entrypoint.resolve(apply_defaults=True)
    _validate_annotation_templates(statements)


def _reject_trailing_projection_commas(script: str) -> None:
    if re.search(r",\s*\bFROM\b", script, flags=re.IGNORECASE):
        raise ValueError("Invalid expression: trailing projection comma before FROM")


def validate_label_studio_template_xml(template: str, *, context: str = "annotation template") -> None:
    try:
        root = ElementTree.fromstring(template)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Invalid {context}: must be valid XML: {exc}") from exc

    if root.tag.lower() != "view":
        raise ValueError(f"Invalid {context}: root element must be <View>")

    if _contains_submit_control(root):
        raise ValueError(
            f"Invalid {context}: submit controls are owned by the annotation UI and must not be included in TEMPLATE",
        )


def _contains_submit_control(root: ElementTree.Element) -> bool:
    for element in root.iter():
        tag = element.tag.replace("-", "").replace("_", "").lower()
        if tag in _SUBMIT_CONTROL_TAGS:
            return True
        if tag != "button":
            continue

        button_type = (element.attrib.get("type") or "").strip().lower()
        if button_type == "submit":
            return True

        label = " ".join(
            value
            for value in (
                element.attrib.get("value"),
                element.attrib.get("label"),
                element.attrib.get("text"),
                "".join(element.itertext()),
            )
            if value
        )
        if _SUBMIT_CONTROL_LABEL_PATTERN.search(label):
            return True

    return False


def _validate_annotation_templates(statements: list[StatementIR]) -> None:
    for statement in statements:
        if not isinstance(statement, PublishAnnotationStmt):
            continue
        template = statement.options.get("template") if statement.options else None
        if template is None:
            continue
        template_text = str(template)
        step_name = statement.alias or statement.table or "annotation"
        validate_label_studio_template_xml(
            template_text,
            context=f"annotation template XML in PUBLISH ANNOTATION step '{step_name}'",
        )


def declared_inputs_from_sql(script: str) -> list[DeclareInputStmt]:
    return [
        statement
        for statement in EngineEntrypoint(script).parse()
        if isinstance(statement, DeclareInputStmt)
    ]


def discover_registered_function_references(
    script: str,
    *,
    registered_functions: Optional[list[RegisteredRuntimeFunction | RegisteredFunctionSpec | Mapping[str, object]]] = None,
) -> list[str]:
    specs = coerce_registered_function_specs(registered_functions or [])
    entrypoint = EngineEntrypoint(script, registered_functions=specs)
    statements = entrypoint.parse()
    registry = build_function_registry(statements, specs)

    local_function_names = {
        statement.definition.canonical_name.lower()
        for statement in statements
        if isinstance(statement, SqlFunctionDefStmt) and statement.definition is not None
    }
    registered_function_names_by_canonical = {
        spec.name.strip().lower(): str(spec.call_name or spec.name).strip()
        for spec in specs
        if spec.name.strip() and str(spec.call_name or spec.name).strip()
    }
    references: dict[str, str] = {}

    for statement in statements:
        _collect_registered_references(
            statement,
            registry=registry,
            registered_function_names_by_canonical=registered_function_names_by_canonical,
            local_function_names=local_function_names,
            references=references,
        )

    return sorted(references.values())


def _collect_registered_references(
    statement: StatementIR,
    *,
    registry,
    registered_function_names_by_canonical: dict[str, str],
    local_function_names: set[str],
    references: dict[str, str],
) -> None:
    if isinstance(statement, QueryStmt) and statement.query is not None:
        _collect_registered_references_from_node(
            statement.query,
            registry=registry,
            registered_function_names_by_canonical=registered_function_names_by_canonical,
            local_function_names=local_function_names,
            references=references,
        )
        return

    _collect_registered_references_from_node(
        statement,
        registry=registry,
        registered_function_names_by_canonical=registered_function_names_by_canonical,
        local_function_names=local_function_names,
        references=references,
    )


def _collect_registered_references_from_node(
    node: object,
    *,
    registry,
    registered_function_names_by_canonical: dict[str, str],
    local_function_names: set[str],
    references: dict[str, str],
) -> None:
    def register_function_name(function_name: str) -> None:
        _collect_registered_references_from_node(
            CallExpr(function_name=function_name, args=[]),
            registry=registry,
            registered_function_names_by_canonical=registered_function_names_by_canonical,
            local_function_names=local_function_names,
            references=references,
        )

    if isinstance(node, SqlAstExpr):
        for function_name in _find_function_calls(node):
            register_function_name(function_name)

    def visit(node: object) -> None:
        if isinstance(node, SqlAstExpr):
            for function_name in _find_function_calls(node):
                register_function_name(function_name)
            return
        if not isinstance(node, CallExpr):
            return
        resolved = registry.resolve(node.function_name)
        if resolved is None:
            return
        canonical = resolved.canonical_name.strip().lower()
        if not canonical or canonical in local_function_names:
            return
        reference_name = registered_function_names_by_canonical.get(canonical)
        if not reference_name:
            return
        references[canonical] = reference_name

    walk_ir(node, visit)
