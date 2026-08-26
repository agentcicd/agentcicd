from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from agentcicd.inspection.local_common import LOCAL_FIXTURE_CALL_PATTERN
from agentcicd.sql.analysis import GraphEdge, GraphNode, build_recipe_dependency_graph
from agentcicd.sql.parsing.segmentation import SQLSegmenter


class LocalRecipeGraphMixin:
    def _recipe_graph(self) -> tuple[list[GraphNode], list[GraphEdge]]:
        return self._build_stable_recipe_graph(self._spec.recipe_sql)

    def _build_stable_recipe_graph(self, source_text: str) -> tuple[list[GraphNode], list[GraphEdge]]:
        segmentation = SQLSegmenter(source_text).segment()
        input_segments = [self._segment_record(item) for item in segmentation.inputs]
        function_segments = [self._segment_record(item) for item in segmentation.functions]
        load_segments = [self._segment_record(item) for item in segmentation.loads]
        table_segments = [self._segment_record(item) for item in segmentation.tables]
        save_segments = [self._segment_record(item) for item in segmentation.saves]
        publish_segments = [self._segment_record(item) for item in segmentation.publishes]
        publish_annotation_segments = [self._segment_record(item) for item in segmentation.publish_annotations]
        retrieve_annotation_segments = [self._segment_record(item) for item in segmentation.retrieve_annotations]
        nodes, edges = build_recipe_dependency_graph(
            segmentation=segmentation,
            input_segments=input_segments,
            function_segments=function_segments,
            load_segments=load_segments,
            table_segments=table_segments,
            save_segments=save_segments,
            publish_segments=publish_segments,
            publish_annotation_segments=publish_annotation_segments,
            retrieve_annotation_segments=retrieve_annotation_segments,
            registered_function_names={f"local.{name}" for name in self._used_fixture_names()},
        )
        nodes, edges = self._stable_recipe_graph(
            nodes=nodes,
            edges=edges,
            function_segments=function_segments,
            load_segments=load_segments,
            table_segments=table_segments,
            save_segments=save_segments,
            publish_segments=publish_segments,
            publish_annotation_segments=publish_annotation_segments,
            retrieve_annotation_segments=retrieve_annotation_segments,
        )
        self._attach_missing_publish_sources(nodes, edges, save_segments, publish_segments, publish_annotation_segments)
        self._attach_secret_input_sources(nodes, edges, source_text)
        return nodes, edges

    def _attach_missing_publish_sources(
        self,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        save_segments: list[dict[str, Any]],
        publish_segments: list[dict[str, Any]],
        publish_annotation_segments: list[dict[str, Any]],
    ) -> None:
        node_ids = {node.id for node in nodes}
        edge_keys = {(edge.from_id, edge.to_id, edge.relation) for edge in edges}

        def add_node_once(node_id: str, node_type: str, label: object) -> None:
            if node_id in node_ids:
                return
            nodes.append(GraphNode(id=node_id, type=node_type, label=str(label or "unknown")))
            node_ids.add(node_id)

        def add_edge_once(from_id: str, to_id: str, relation: str) -> None:
            key = (from_id, to_id, relation)
            if key in edge_keys:
                return
            edges.append(GraphEdge(from_id=from_id, to_id=to_id, relation=relation))
            edge_keys.add(key)

        for segment in save_segments:
            table_name = segment.get("table")
            table_id = self._stable_recipe_item_id("table", table_name)
            save_id = self._stable_recipe_item_id("save", table_name)
            if save_id in node_ids:
                add_node_once(table_id, "table", table_name)
                add_edge_once(table_id, save_id, "save_from_table")

        for index, segment in enumerate(publish_segments):
            table_name = segment.get("table")
            destination = str(segment.get("destination") or "").upper()
            component = str(segment.get("component") or "").lower()
            table_id = self._stable_recipe_item_id("table", table_name)
            publish_id = self._stable_publish_id(table_name, segment.get("destination"), segment.get("component"), index)
            if publish_id not in node_ids:
                continue
            add_node_once(table_id, "table", table_name)
            if destination == "DATASET":
                relation = "publish_dataset"
            elif destination == "REPORTS":
                relation = f"publish_report_{component or 'other'}"
            else:
                relation = "publish"
            add_edge_once(table_id, publish_id, relation)

        for index, segment in enumerate(publish_annotation_segments):
            table_name = segment.get("table")
            table_id = self._stable_recipe_item_id("table", table_name)
            publish_id = self._stable_annotation_publish_id(segment.get("alias") or segment.get("queue_name"), index)
            if publish_id in node_ids:
                add_node_once(table_id, "table", table_name)
                add_edge_once(table_id, publish_id, "publish_annotation")

    def _attach_secret_input_sources(self, nodes: list[GraphNode], edges: list[GraphEdge], source_text: str) -> None:
        node_ids = {node.id for node in nodes}
        for input_name, secret_reference in self._used_secret_input_references(source_text).items():
            secret_id = f"secret:{secret_reference.removeprefix('secret.')}"
            input_id = self._stable_recipe_item_id("input", input_name)
            if secret_id not in node_ids:
                nodes.append(GraphNode(id=secret_id, type="secret", label=secret_reference))
                node_ids.add(secret_id)
            edges.append(GraphEdge(from_id=secret_id, to_id=input_id, relation="provided_to"))

    def _recipe_analysis_payload(self, source_text: str, *, segmentation_key: str) -> dict[str, Any]:
        segmentation = SQLSegmenter(source_text).segment()
        inputs = [self._stable_segment_record("input", item.name, item) for item in segmentation.inputs]
        functions = [self._stable_segment_record("function", item.name, item) for item in segmentation.functions]
        loads = [self._stable_segment_record("load", item.table, item) for item in segmentation.loads]
        tables = [self._stable_segment_record("table", item.table, item) for item in segmentation.tables]
        saves = [self._stable_segment_record("save", item.table, item) for item in segmentation.saves]
        publishes = [
            {
                **self._segment_record(item),
                "id": self._stable_publish_id(item.table, item.destination, item.component, index),
                "source_id": self._stable_recipe_item_id("table", item.table),
            }
            for index, item in enumerate(segmentation.publishes)
        ]
        publish_annotations = [
            {
                **self._segment_record(item),
                "id": self._stable_annotation_publish_id(item.alias or item.queue_name or item.table, index),
                "source_id": self._stable_recipe_item_id("table", item.table),
                "kind": "annotation",
            }
            for index, item in enumerate(segmentation.publish_annotations)
        ]
        retrieves = [
            {
                **self._segment_record(item),
                "id": self._stable_annotation_retrieve_id(item.table, index),
                "target_id": self._stable_recipe_item_id("table", item.table),
            }
            for index, item in enumerate(segmentation.retrieve_annotations)
        ]
        nodes, edges = self._build_stable_recipe_graph(source_text)
        metadata = {
            "total_segments": segmentation.get_segment_count(),
            "total_lines": segmentation.total_lines,
            "has_macros": segmentation.has_macros,
            "macro_placeholders": segmentation.macro_placeholders,
        }
        dependency_edges = [{"from_id": edge.from_id, "to_id": edge.to_id, "relation": edge.relation} for edge in edges]
        payload: dict[str, Any] = {
            "schema_version": "recipe_analysis.v2" if segmentation_key == "analysis" else "recipe_segmentation.v1",
            "valid": True,
            "errors": [],
            "warnings": [],
            "inputs": inputs,
            "functions": functions,
            "loads": loads,
            "tables": tables,
            "saves": saves,
            "publishes": [*publishes, *publish_annotations],
            "retrieves": retrieves,
            "nodes": [{"id": node.id, "type": node.type, "label": node.label} for node in nodes],
            "dependencies": dependency_edges,
            "graph": [{"from": edge.from_id, "to": edge.to_id, "relation": edge.relation} for edge in edges],
            "fixtures": [],
            "metadata": metadata,
        }
        if segmentation_key == "segments":
            payload["publish_annotations"] = publish_annotations
            payload["retrieve_annotations"] = retrieves
        else:
            payload["report"] = {
                "components": publishes,
                "metrics": [item for item in publishes if str(item.get("component") or "").lower() == "metric"],
                "charts": [item for item in publishes if str(item.get("component") or "").lower() == "chart"],
                "issues": [item for item in publishes if str(item.get("component") or "").lower() == "issue"],
                "examples": [item for item in publishes if str(item.get("component") or "").lower() == "example"],
                "datasets": [item for item in publishes if str(item.get("destination") or "").lower() == "dataset"],
                "layout": [],
            }
        return self._redact(payload)

    @classmethod
    def _stable_recipe_graph(
        cls,
        *,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        function_segments: list[dict[str, Any]],
        load_segments: list[dict[str, Any]],
        table_segments: list[dict[str, Any]],
        save_segments: list[dict[str, Any]],
        publish_segments: list[dict[str, Any]],
        publish_annotation_segments: list[dict[str, Any]],
        retrieve_annotation_segments: list[dict[str, Any]],
    ) -> tuple[list[GraphNode], list[GraphEdge]]:
        node_id_map: dict[str, str] = {}
        stable_nodes: list[GraphNode] = []
        seen_nodes: set[str] = set()
        for node in nodes:
            stable_id = cls._stable_graph_node_id(
                node_id=node.id,
                node_type=node.type,
                label=node.label,
                function_segments=function_segments,
                load_segments=load_segments,
                table_segments=table_segments,
                save_segments=save_segments,
                publish_segments=publish_segments,
                publish_annotation_segments=publish_annotation_segments,
                retrieve_annotation_segments=retrieve_annotation_segments,
            )
            node_id_map[node.id] = stable_id
            if stable_id in seen_nodes:
                continue
            seen_nodes.add(stable_id)
            stable_nodes.append(GraphNode(id=stable_id, type=node.type, label=node.label))

        stable_edges: list[GraphEdge] = []
        seen_edges: set[tuple[str, str, str]] = set()
        for edge in edges:
            from_id = node_id_map.get(edge.from_id)
            to_id = node_id_map.get(edge.to_id)
            if not from_id or not to_id:
                continue
            key = (from_id, to_id, edge.relation)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            stable_edges.append(GraphEdge(from_id=from_id, to_id=to_id, relation=edge.relation))
        return stable_nodes, stable_edges

    @classmethod
    def _stable_graph_node_id(
        cls,
        *,
        node_id: str,
        node_type: str,
        label: str,
        function_segments: list[dict[str, Any]],
        load_segments: list[dict[str, Any]],
        table_segments: list[dict[str, Any]],
        save_segments: list[dict[str, Any]],
        publish_segments: list[dict[str, Any]],
        publish_annotation_segments: list[dict[str, Any]],
        retrieve_annotation_segments: list[dict[str, Any]],
    ) -> str:
        prefix, _, raw_index = node_id.partition(":")
        index = int(raw_index) if raw_index.isdigit() else -1
        if prefix == "function" and 0 <= index < len(function_segments):
            return cls._stable_recipe_item_id("function", function_segments[index].get("name"))
        if prefix == "load" and 0 <= index < len(load_segments):
            return cls._stable_recipe_item_id("load", load_segments[index].get("table"))
        if prefix == "table" and 0 <= index < len(table_segments):
            return cls._stable_recipe_item_id("table", table_segments[index].get("table"))
        if prefix == "save" and 0 <= index < len(save_segments):
            return cls._stable_recipe_item_id("save", save_segments[index].get("table"))
        if prefix == "publish" and 0 <= index < len(publish_segments):
            segment = publish_segments[index]
            return cls._stable_publish_id(segment.get("table"), segment.get("destination"), segment.get("component"), index)
        if prefix == "publish_annotation" and 0 <= index < len(publish_annotation_segments):
            segment = publish_annotation_segments[index]
            return cls._stable_annotation_publish_id(segment.get("alias") or segment.get("queue_name"), index)
        if prefix == "retrieve_annotation" and 0 <= index < len(retrieve_annotation_segments):
            return cls._stable_annotation_retrieve_id(retrieve_annotation_segments[index].get("table"), index)
        if node_type == "table":
            return cls._stable_recipe_item_id("table", label)
        if node_type == "load":
            return cls._stable_recipe_item_id("load", label)
        if node_type.startswith("publish_report"):
            return cls._stable_publish_id(label, "reports", node_type.removeprefix("publish_report_"), 0)
        if node_type == "publish_dataset":
            return cls._stable_publish_id(label, "dataset", "", 0)
        return node_id

    @classmethod
    def _stable_recipe_item_id(cls, prefix: str, name: object) -> str:
        return f"{prefix}:{cls._stable_slug(str(name or 'unknown'))}"

    @classmethod
    def _stable_publish_id(cls, table: object, destination: object, component: object, index: int) -> str:
        destination_slug = cls._stable_slug(str(destination or "publish"))
        table_slug = cls._stable_slug(str(table or f"publish_{index + 1}"))
        component_slug = cls._stable_slug(str(component or ""))
        suffix = f":{component_slug}" if component_slug else ""
        return f"publish:{table_slug}:{destination_slug}{suffix}"

    @classmethod
    def _stable_annotation_publish_id(cls, ref: object, index: int) -> str:
        return f"publish:{cls._stable_slug(str(ref or f'annotation_{index + 1}'))}:annotation"

    @classmethod
    def _stable_annotation_retrieve_id(cls, table: object, index: int) -> str:
        return f"retrieve:{cls._stable_slug(str(table or f'annotations_{index + 1}'))}:annotation"

    @staticmethod
    def _stable_slug(value: str) -> str:
        return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in value.strip().lower()).strip("_") or "unknown"

    @staticmethod
    def _segment_record(segment: object) -> dict[str, Any]:
        payload = dict(vars(segment))
        payload["source_text"] = payload.pop("sql_text", "")
        return payload

    @classmethod
    def _stable_segment_record(cls, prefix: str, name: object, segment: object) -> dict[str, Any]:
        return {"id": cls._stable_recipe_item_id(prefix, name), **cls._segment_record(segment)}

    def _used_fixture_names(self) -> set[str]:
        return {match.group(1).lower() for match in LOCAL_FIXTURE_CALL_PATTERN.finditer(self._spec.recipe_sql)}

    def _fixture_function_names(self, source: Path) -> set[str]:
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=source.name)
        except (OSError, SyntaxError):
            return set()
        return {
            node.name.lower()
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")
        }

    def _used_secret_input_references(self, source_text: str | None = None) -> dict[str, str]:
        used_input_names = self._used_input_names(source_text or self._spec.recipe_sql)
        return {
            name: value
            for name, value in self._spec.inputs.input_values.items()
            if name in used_input_names and isinstance(value, str) and value.startswith("secret.")
        }

    def _used_secret_references(self) -> set[str]:
        return set(self._used_secret_input_references().values())

    def _used_input_names(self, source_text: str | None = None) -> set[str]:
        declarations = self._spec.inputs.input_sources
        non_declaration_sql = re.sub(r"(?is)\bDECLARE\s+INPUT\b.*?;", "", source_text or self._spec.recipe_sql)
        return {
            name
            for name in declarations
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", non_declaration_sql)
        }
