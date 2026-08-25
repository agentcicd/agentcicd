from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from typing import Any, Mapping, Protocol

import sqlglot
from sqlglot import expressions as exp

from agentcicd.sql.dialect import AgentCICDSqlDialect
from agentcicd.sql.parsing.segmentation import SQLSegmentation

from .runtime_dependencies import extract_runtime_dependencies_from_sql


SPARK_BUILTIN_FUNCTIONS = {
    "abs", "acos", "add_months", "aes_decrypt", "aes_encrypt", "and", "any_value",
    "approx_count_distinct", "array", "array_contains", "array_distinct", "array_except",
    "array_intersect", "array_join", "array_max", "array_min", "array_position",
    "array_remove", "array_repeat", "array_size", "array_sort", "array_union", "avg",
    "base64", "bin", "bit_and", "bit_count", "bit_length", "bit_or", "bit_xor", "bool_and",
    "bool_or", "bround", "cast", "ceil", "ceiling", "char_length", "character_length",
    "coalesce", "collect_list", "collect_set", "concat", "concat_ws", "contains",
    "conv", "corr", "cos", "cosh", "count", "count_if", "count_min_sketch",
    "covar_pop", "covar_samp", "crc32", "current_date", "current_timestamp", "date_add",
    "date_diff", "date_format", "date_sub", "date_trunc", "datediff", "day",
    "dayofmonth", "dayofweek", "dayofyear", "decode", "dense_rank", "element_at", "encode",
    "endswith", "exp", "explode", "explode_outer", "factorial", "filter", "first", "flatten",
    "floor", "from_json", "from_unixtime", "get_json_object", "greatest", "grouping",
    "grouping_id", "hash", "hex", "hour", "if", "ifnull", "initcap", "inline",
    "input_file_name", "instr", "isnan", "isnull", "json_array_length", "json_object_keys",
    "lag", "last", "last_day", "lead", "least", "length", "levenshtein", "ln", "locate",
    "log", "log10", "log1p", "lower", "lpad", "ltrim", "make_date", "map", "map_concat",
    "map_entries", "map_from_arrays", "map_from_entries", "map_keys", "map_values", "max",
    "md5", "mean", "min", "minute", "mode", "monotonically_increasing_id", "month", "nanvl",
    "named_struct", "next_day", "nvl", "nvl2", "octet_length", "or", "parse_url",
    "percent_rank", "percentile", "percentile_approx", "pmod", "posexplode", "pow",
    "printf", "radians", "rand", "randn", "rank", "regexp", "regexp_count", "regexp_extract",
    "regexp_extract_all", "regexp_instr", "regexp_like", "regexp_replace", "regexp_substr",
    "repeat", "replace", "reverse", "round", "row_number", "rpad", "rtrim", "schema_of_json",
    "second", "sha", "sha1", "sha2", "sign", "sin", "sinh", "size", "slice", "sort_array",
    "soundex", "split", "sqrt", "stack", "startswith", "stddev", "stddev_pop", "stddev_samp",
    "str_to_map", "struct", "substring", "sum", "tan", "tanh", "timestamp_millis",
    "timestamp_seconds", "to_date", "to_json", "to_timestamp", "translate", "trim",
    "try_element_at", "unbase64", "unhex", "unix_date", "unix_millis", "unix_seconds",
    "unix_timestamp", "upper", "uuid", "var_pop", "var_samp", "variance", "weekofyear",
    "when", "window", "xpath", "year",
}

UDF_FIXTURE_REQUIREMENTS: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class GraphNode:
    id: str
    type: str
    label: str


@dataclass(frozen=True)
class GraphEdge:
    from_id: str
    to_id: str
    relation: str


@dataclass(frozen=True)
class FixtureGraphInfo:
    label: str
    kind: str = ""


@dataclass(frozen=True)
class AiSystemGraphInfo:
    label: str


@dataclass(frozen=True)
class SecretGraphInfo:
    label: str


class RecipeGraphCallbacks(Protocol):
    def fixture_info(self, fixture_id: str) -> FixtureGraphInfo | None:
        ...

    def aisystem_info(self, aisystem_id: str) -> AiSystemGraphInfo | None:
        ...

    def secret_info(self, secret_id: str) -> SecretGraphInfo | None:
        ...


class EmptyRecipeGraphCallbacks:
    def fixture_info(self, fixture_id: str) -> FixtureGraphInfo | None:
        return None

    def aisystem_info(self, aisystem_id: str) -> AiSystemGraphInfo | None:
        return None

    def secret_info(self, secret_id: str) -> SecretGraphInfo | None:
        return None


@dataclass(frozen=True)
class MappingRecipeGraphCallbacks:
    fixtures_by_id: Mapping[str, Mapping[str, Any]]
    aisystems_by_id: Mapping[str, Mapping[str, Any]]
    secrets_by_id: Mapping[str, Mapping[str, Any]]

    def fixture_info(self, fixture_id: str) -> FixtureGraphInfo | None:
        fixture = self.fixtures_by_id.get(fixture_id)
        if fixture is None:
            return None
        return FixtureGraphInfo(
            label=str(fixture.get("name") or fixture_id),
            kind=str(fixture.get("kind") or "").strip(),
        )

    def aisystem_info(self, aisystem_id: str) -> AiSystemGraphInfo | None:
        aisystem = self.aisystems_by_id.get(aisystem_id)
        if aisystem is None:
            return None
        return AiSystemGraphInfo(label=str(aisystem.get("name") or aisystem_id))

    def secret_info(self, secret_id: str) -> SecretGraphInfo | None:
        secret = self.secrets_by_id.get(secret_id)
        if secret is None:
            return None
        return SecretGraphInfo(label=str(secret.get("key") or secret_id))


def build_recipe_dependency_graph(
    *,
    segmentation: SQLSegmentation,
    input_segments: list[Mapping[str, Any]] | None = None,
    function_segments: list[Mapping[str, Any]],
    load_segments: list[Mapping[str, Any]],
    table_segments: list[Mapping[str, Any]],
    save_segments: list[Mapping[str, Any]],
    publish_segments: list[Mapping[str, Any]],
    publish_annotation_segments: list[Mapping[str, Any]],
    retrieve_annotation_segments: list[Mapping[str, Any]],
    registered_function_names: set[str],
    callbacks: RecipeGraphCallbacks | None = None,
) -> tuple[list[GraphNode], list[GraphEdge]]:
    callbacks = callbacks or EmptyRecipeGraphCallbacks()

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    node_ids: set[str] = set()
    edge_keys: set[tuple[str, str, str]] = set()
    input_nodes: dict[str, str] = {}
    table_producer: dict[str, str] = {}
    pending_table_edges: list[tuple[str, str, str]] = []
    function_nodes: dict[str, str] = {}
    function_reference_nodes: dict[str, str] = {}
    fixture_kind_nodes: dict[str, str] = {}

    def add_node(node_id: str, node_type: str, label: str) -> None:
        if node_id in node_ids:
            return
        node_ids.add(node_id)
        nodes.append(GraphNode(id=node_id, type=node_type, label=label))

    def add_edge(from_id: str, to_id: str, relation: str) -> None:
        key = (from_id, to_id, relation)
        if key in edge_keys:
            return
        edge_keys.add(key)
        edges.append(GraphEdge(from_id=from_id, to_id=to_id, relation=relation))

    def register_table_producer(table_name: str, node_id: str) -> None:
        normalized = normalize_table_reference(table_name)
        if normalized:
            table_producer[normalized] = node_id

    def add_table_dependency(dep_table: object, node_id: str, relation: str = "depends_on") -> None:
        normalized = normalize_table_reference(str(dep_table))
        if not normalized:
            return
        producer_id = table_producer.get(normalized)
        if producer_id:
            add_edge(producer_id, node_id, relation)
            return
        pending_table_edges.append((normalized, node_id, relation))

    def resolve_pending_table_edges() -> None:
        for dep_table, node_id, relation in pending_table_edges:
            producer_id = table_producer.get(dep_table)
            if producer_id:
                add_edge(producer_id, node_id, relation)

    def normalize_sql_function_name(name: str) -> str:
        return name.strip().lower()

    for seg in input_segments or []:
        name = str(seg.get("name") or "").strip()
        if not name:
            continue
        node_id = f"input:{sanitize_node_id(name)}"
        add_node(node_id, "input", name)
        input_nodes[name.lower()] = node_id

    def is_runtime_udf_name(name: str) -> bool:
        lowered = name.strip().lower()
        return lowered.startswith(
            (
                "agent.",
                "aisystems.",
                "data.",
                "envs.",
                "http.",
                "ranking.",
                "simulators.",
                "string.",
                "zip.",
            )
        )

    def get_or_create_function_reference_node(function_name: str) -> str | None:
        normalized_raw = function_name.strip().lower()
        normalized = normalize_sql_function_name(function_name)
        if not normalized or normalized in SPARK_BUILTIN_FUNCTIONS:
            return None

        if normalized in function_nodes:
            return function_nodes[normalized]

        existing = function_reference_nodes.get(normalized)
        if existing:
            return existing

        if (
            normalized in registered_function_names
            or normalized_raw in UDF_FIXTURE_REQUIREMENTS
            or is_runtime_udf_name(normalized_raw)
        ):
            node_id = f"function_reference:{sanitize_node_id(normalized_raw or normalized)}"
            label = normalized_raw if is_runtime_udf_name(normalized_raw) else normalized
            add_node(node_id, "function_reference", label)
            function_reference_nodes[normalized] = node_id
            return node_id
        return None

    def get_or_create_fixture_node(fixture_id: str) -> str:
        info = callbacks.fixture_info(fixture_id)
        label = info.label if info else fixture_id
        kind = info.kind if info else ""
        if kind:
            label = f"{label} ({kind})"
        node_id = f"fixture:{sanitize_node_id(fixture_id)}"
        add_node(node_id, "fixture", label)
        return node_id

    def get_or_create_image_node(image_id: str) -> str:
        node_id = f"image:{sanitize_node_id(image_id)}"
        add_node(node_id, "image", image_id)
        return node_id

    def get_or_create_fixture_kind_node(fixture_kind: str) -> str:
        node_id = fixture_kind_nodes.get(fixture_kind, "")
        if node_id:
            return node_id
        node_id = f"fixture_kind:{sanitize_node_id(fixture_kind)}"
        add_node(node_id, "fixture_kind", fixture_kind)
        fixture_kind_nodes[fixture_kind] = node_id
        return node_id

    def get_or_create_aisystem_node(aisystem_id: str) -> str:
        info = callbacks.aisystem_info(aisystem_id)
        label = info.label if info else aisystem_id
        node_id = f"aisystem:{sanitize_node_id(aisystem_id)}"
        add_node(node_id, "aisystem", label)
        return node_id

    def get_or_create_secret_node(secret_id: str) -> str:
        info = callbacks.secret_info(secret_id)
        label = info.label if info else secret_id
        node_id = f"secret:{sanitize_node_id(secret_id)}"
        add_node(node_id, "secret", label)
        return node_id

    def sql_snippets(*parts: str) -> Iterable[str]:
        for part in parts:
            text = str(part or "").strip()
            if text:
                yield text

    def add_segment_resources(segment_node_id: str, *sql_texts: str) -> None:
        for sql_text in sql_snippets(*sql_texts):
            dependencies = extract_runtime_dependencies_from_sql(sql_text)
            for fixture_id in sorted(dependencies.fixture_ids):
                add_edge(get_or_create_fixture_node(fixture_id), segment_node_id, "uses_fixture")
            for image_id in sorted(dependencies.image_ids):
                add_edge(get_or_create_image_node(image_id), segment_node_id, "uses_image")
            for aisystem_id in sorted(dependencies.aisystem_ids):
                add_edge(get_or_create_aisystem_node(aisystem_id), segment_node_id, "uses_aisystem")
            for secret_id in sorted(dependencies.secret_ids):
                add_edge(get_or_create_secret_node(secret_id), segment_node_id, "uses_secret")
            add_input_dependencies(segment_node_id, sql_text)

    def add_input_dependencies(segment_node_id: str, sql_text: str) -> None:
        if not input_nodes:
            return
        referenced = extract_identifier_references(sql_text)
        for name, node_id in sorted(input_nodes.items()):
            if name in referenced:
                add_edge(node_id, segment_node_id, "uses_input")

    def add_input_dependency_by_name(segment_node_id: str, value: object) -> None:
        if not input_nodes:
            return
        normalized = str(value or "").strip().lower()
        if not normalized:
            return
        node_id = input_nodes.get(normalized)
        if node_id:
            add_edge(node_id, segment_node_id, "uses_input")

    def add_table_option_pool_dependency(segment_node_id: str, seg: Mapping[str, Any]) -> None:
        raw_options = seg.get("options")
        if not isinstance(raw_options, Mapping):
            return
        add_input_dependency_by_name(segment_node_id, raw_options.get("pool"))

    def add_segment_function_calls(segment_node_id: str, *sql_texts: str) -> None:
        for sql_text in sql_snippets(*sql_texts):
            for called_name, called_args in extract_function_calls(sql_text):
                function_node_id = get_or_create_function_reference_node(called_name)
                if function_node_id:
                    add_edge(function_node_id, segment_node_id, "function_used")

                requirement = UDF_FIXTURE_REQUIREMENTS.get(called_name.strip().lower())
                if not requirement:
                    continue
                raw_fixture_arg_index = requirement.get("fixture_arg_index", 0)
                fixture_kind = str(requirement.get("fixture_kind") or "").strip() or None
                if raw_fixture_arg_index is None:
                    if fixture_kind:
                        add_edge(get_or_create_fixture_kind_node(fixture_kind), segment_node_id, "requires_fixture_kind")
                    continue
                fixture_arg_index = int(raw_fixture_arg_index)
                fixture_arg = called_args[fixture_arg_index] if fixture_arg_index < len(called_args) else None
                fixture_arg_value = literal_string_value(fixture_arg)
                if fixture_arg_value:
                    if fixture_arg_value.startswith("image."):
                        add_edge(
                            get_or_create_image_node(fixture_arg_value),
                            segment_node_id,
                            "uses_image",
                        )
                    elif fixture_arg_value.startswith("fixture."):
                        add_edge(get_or_create_fixture_node(fixture_arg_value), segment_node_id, "uses_fixture")
                elif fixture_kind:
                    add_edge(get_or_create_fixture_kind_node(fixture_kind), segment_node_id, "requires_fixture_kind")

    for index, seg in enumerate(function_segments):
        node_id = f"function:{index}"
        function_name = str(seg["name"])
        add_node(node_id, "function_local", function_name)
        function_nodes[function_name.strip().lower()] = node_id
        source_sql = str(seg.get("source_text") or "")
        add_segment_resources(node_id, source_sql)
        add_segment_function_calls(node_id, source_sql)

    for index, seg in enumerate(load_segments):
        node_id = f"load:{index}"
        table_name = str(seg["table"])
        add_node(node_id, "load", table_name)
        register_table_producer(table_name, node_id)
        add_input_dependency_by_name(node_id, seg.get("source"))
        add_segment_resources(node_id, str(seg.get("source_text") or ""))

    for index, seg in enumerate(table_segments):
        node_id = f"table:{index}"
        table_name = str(seg["table"])
        add_node(node_id, "table", table_name)

        for dep_table in list(seg.get("depends_on") or []):
            add_table_dependency(dep_table, node_id)

        add_table_option_pool_dependency(node_id, seg)
        source_sql = str(seg.get("source_text") or "")
        query_sql = str(seg.get("query_sql") or "")
        add_segment_resources(node_id, source_sql, query_sql)
        add_segment_function_calls(node_id, source_sql, query_sql)
        register_table_producer(table_name, node_id)

    for index, seg in enumerate(save_segments):
        node_id = f"save:{index}"
        table_name = str(seg["table"])
        add_node(node_id, "save", table_name)
        producer_id = table_producer.get(normalize_table_reference(table_name))
        if producer_id:
            add_edge(producer_id, node_id, "save_from_table")
        add_segment_resources(node_id, str(seg.get("source_text") or ""))

    for index, seg in enumerate(publish_segments):
        node_id = f"publish:{index}"
        table_name = str(seg["table"])
        destination = str(seg.get("destination") or "").upper()
        component = normalize_report_component(seg.get("component"))
        node_type = "publish"
        if destination == "REPORTS":
            node_type = f"publish_report_{component or 'other'}"
        elif destination == "DATASET":
            node_type = "publish_dataset"
        add_node(node_id, node_type, table_name)
        producer_id = table_producer.get(normalize_table_reference(table_name))
        if producer_id:
            if destination == "DATASET":
                relation = "publish_dataset"
            elif destination == "REPORTS":
                relation = f"publish_report_{component or 'other'}"
            else:
                relation = "publish"
            add_edge(producer_id, node_id, relation)
        add_segment_resources(node_id, str(seg.get("source_text") or ""))

    annotation_retrieval_by_ref: dict[str, str] = {}
    for index, seg in enumerate(retrieve_annotation_segments):
        node_id = f"retrieve_annotation:{index}"
        table_name = str(seg["table"])
        add_node(node_id, "retrieve_annotation", table_name)
        source_ref = str(seg.get("source_ref") or seg.get("annotation_request_id") or "")
        if source_ref:
            annotation_retrieval_by_ref[source_ref] = node_id
        register_table_producer(table_name, node_id)
        add_segment_resources(node_id, str(seg.get("source_text") or ""))

    for index, seg in enumerate(publish_annotation_segments):
        node_id = f"publish_annotation:{index}"
        table_name = str(seg["table"])
        add_node(node_id, "publish_annotation", table_name)
        producer_id = table_producer.get(normalize_table_reference(table_name))
        if producer_id:
            add_edge(producer_id, node_id, "publish_annotation")
        source_ref = str(seg.get("alias") or seg.get("table") or "")
        retrieval_id = annotation_retrieval_by_ref.get(source_ref)
        if retrieval_id:
            add_edge(node_id, retrieval_id, "annotation_roundtrip")
        add_segment_resources(node_id, str(seg.get("source_text") or ""))

    resolve_pending_table_edges()
    return nodes, edges


def sanitize_node_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in value.strip().lower()).strip("_") or "unknown"


def normalize_table_reference(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    lowered = text.lower()
    if " as " in lowered:
        text = text[: lowered.index(" as ")]
    else:
        parts = text.split()
        if len(parts) > 1:
            text = parts[0]
    return text.strip().strip("`").lower()


def normalize_report_component(value: object) -> str:
    component = str(value or "").strip().lower()
    if component in {"metric", "chart", "issue", "example"}:
        return component
    return ""


def literal_string_value(arg: exp.Expression | None) -> str | None:
    if arg is None:
        return None
    if isinstance(arg, exp.Literal) and arg.is_string:
        raw = str(arg.this)
        return raw.strip() if raw else None
    if isinstance(arg, exp.Identifier) and arg.this:
        return str(arg.this).strip()
    if isinstance(arg, exp.Column):
        col = arg.this
        if isinstance(col, exp.Identifier) and col.this:
            return str(col.this).strip()
    return None


def function_call_name(call: exp.Func) -> str | None:
    parent = call.parent
    if isinstance(parent, exp.Dot) and parent.expression is call:
        namespace = parent.this
        base_name = call.name
        if isinstance(namespace, exp.Identifier) and namespace.this and isinstance(base_name, str) and base_name.strip():
            return f"{str(namespace.this).strip()}.{base_name.strip()}"
    if isinstance(call, exp.Anonymous):
        name = call.name
        if isinstance(name, str) and name.strip():
            return name.strip()
    try:
        sql_name = call.sql_name()
        if isinstance(sql_name, str) and sql_name.strip():
            return sql_name.strip()
    except Exception:
        pass
    this_expr = call.this
    if isinstance(this_expr, exp.Identifier) and this_expr.this:
        return str(this_expr.this).strip()
    if isinstance(this_expr, str) and this_expr.strip():
        return this_expr.strip()
    return None


def extract_function_calls(source_sql: str) -> list[tuple[str, list[exp.Expression]]]:
    text = source_sql.strip()
    if not text:
        return []
    try:
        parsed = sqlglot.parse(text, read=AgentCICDSqlDialect)
    except Exception:
        try:
            parsed = sqlglot.parse(text, read="spark")
        except Exception:
            return []

    calls: list[tuple[str, list[exp.Expression]]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for statement in parsed:
        for call in statement.find_all(exp.Func):
            name = function_call_name(call)
            if not name:
                continue
            args = list(call.expressions or [])
            key = (name.strip().lower(), tuple(arg.sql(dialect="spark") for arg in args))
            if key in seen:
                continue
            seen.add(key)
            calls.append((name, args))
    return calls


def extract_identifier_references(source_sql: str) -> set[str]:
    text = source_sql.strip()
    if not text:
        return set()
    try:
        parsed = sqlglot.parse(text, read=AgentCICDSqlDialect)
    except Exception:
        try:
            parsed = sqlglot.parse(text, read="spark")
        except Exception:
            return set()

    references: set[str] = set()
    for statement in parsed:
        for identifier in statement.find_all(exp.Identifier):
            value = str(identifier.this or "").strip().lower()
            if value:
                references.add(value)
        for column in statement.find_all(exp.Column):
            value = str(column.name or "").strip().lower()
            if value:
                references.add(value)
    return references
