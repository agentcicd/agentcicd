from __future__ import annotations

from agentcicd.sql.parsing import parser as _parser

for _name, _value in vars(_parser).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

class AgentCICDScriptParserSegmentMixin:
    def _build_create_table_segment(self, expression: CreateTableExpression, index: int) -> SqlSegment:
        table_expr = expression.args["table"]
        phase_type_expr = expression.args["phase_type"]
        batch_size_expr = expression.args.get("batch_size")
        query_expr = expression.args["query"]

        query = query_expr.copy()
        source_functions = [
            self._sql_function_runtime_alias(name)
            for name in self._collect_sql_function_references(query)
        ]
        query = self._rewrite_registered_runtime_functions(query)
        if self._enable_sql_transpile:
            query = transpile_query_expression_with_options(query)

        return SqlSegment(
            segment_id=self._segment_id(index, SqlSegmentType.CREATE_TABLE, self._identifier_name(table_expr)),
            block_type=SqlSegmentType.CREATE_TABLE,
            table=self._identifier_name(table_expr),
            phase_type=self._identifier_name(phase_type_expr),
            batch_size=(
                int(batch_size_expr.this) if batch_size_expr is not None else None
            ),
            statement_exprs=[query],
            source_functions=source_functions,
        )

    def _build_load_segment(self, expression: LoadExpression, index: int) -> SqlSegment:
        table_expr = expression.args["table"]
        path_expr = expression.args["path"]
        options_expr = expression.args.get("options")
        limit_expr = expression.args.get("limit")
        options = self._options_expr_to_dict(options_expr)
        if limit_expr is not None:
            options["limit"] = self._literal_value(limit_expr)
        return SqlSegment(
            segment_id=self._segment_id(index, SqlSegmentType.LOAD_TABLE, self._identifier_name(table_expr)),
            block_type=SqlSegmentType.LOAD_TABLE,
            table=self._identifier_name(table_expr),
            path=self._literal_value(path_expr),
            options=options,
        )

    def _build_save_segment(self, expression: SaveExpression, index: int) -> SqlSegment:
        table_expr = expression.args["table"]
        path_expr = expression.args["path"]
        options_expr = expression.args.get("options")
        return SqlSegment(
            segment_id=self._segment_id(index, SqlSegmentType.EXPORT_TABLE, self._identifier_name(table_expr)),
            block_type=SqlSegmentType.EXPORT_TABLE,
            table=self._identifier_name(table_expr),
            path=self._literal_value(path_expr),
            options=self._options_expr_to_dict(options_expr),
        )

    def _build_publish_segment(self, expression: PublishExpression, index: int) -> SqlSegment:
        table_expr = expression.args["table"]
        return SqlSegment(
            segment_id=self._segment_id(index, SqlSegmentType.PUBLISH_REPORTS, self._identifier_name(table_expr)),
            block_type=SqlSegmentType.PUBLISH_REPORTS,
            table=self._identifier_name(table_expr),
            report_component=self._identifier_name(expression.args["component"]).lower(),
            chart_type=self._literal_value(expression.args.get("chart_type")) if expression.args.get("chart_type") is not None else None,
            report_options={
                str(key).lower(): str(value)
                for key, value in (
                    self._options_expr_to_dict(expression.args.get("report_options"))
                    if expression.args.get("report_options") is not None
                    else {}
                ).items()
                if isinstance(value, str)
            },
        )

    def _build_publish_dataset_segment(self, expression: PublishDatasetExpression, index: int) -> SqlSegment:
        table_expr = expression.args["table"]
        dataset_name_expr = expression.args.get("dataset_name")
        publish_name = self._literal_value(dataset_name_expr) if dataset_name_expr is not None else None
        return SqlSegment(
            segment_id=self._segment_id(index, SqlSegmentType.PUBLISH_DATASET, self._identifier_name(table_expr)),
            block_type=SqlSegmentType.PUBLISH_DATASET,
            table=self._identifier_name(table_expr),
            publish_name=publish_name,
        )

    def _build_publish_annotation_segment(self, expression: PublishAnnotationExpression, index: int) -> SqlSegment:
        table_expr = expression.args["table"]
        queue_name_expr = expression.args["queue_name"]
        alias_expr = expression.args.get("alias")
        queue_name = self._literal_value(queue_name_expr)
        alias = self._identifier_name(alias_expr) if alias_expr is not None else None
        return SqlSegment(
            segment_id=self._segment_id(index, SqlSegmentType.PUBLISH_ANNOTATION, alias or self._identifier_name(table_expr)),
            block_type=SqlSegmentType.PUBLISH_ANNOTATION,
            table=self._identifier_name(table_expr),
            queue_name=queue_name,
            publish_alias=alias,
            options=self._options_expr_to_dict(expression.args.get("options")),
        )

    def _build_retrieve_annotation_segment(self, expression: RetrieveAnnotationExpression, index: int) -> SqlSegment:
        table_expr = expression.args["table"]
        source_ref_expr = expression.args.get("source_ref")
        request_id_expr = expression.args.get("annotation_request_id")
        table_name = self._identifier_name(table_expr)
        request_id = self._literal_value(request_id_expr) if request_id_expr is not None else None
        source_ref = self._identifier_name(source_ref_expr) if source_ref_expr is not None else request_id
        return SqlSegment(
            segment_id=self._segment_id(index, SqlSegmentType.RETRIEVE_ANNOTATION, table_name),
            block_type=SqlSegmentType.RETRIEVE_ANNOTATION,
            table=table_name,
            source_ref=source_ref,
            annotation_request_id=request_id,
        )

    def _validate_publish_reports_contract(self, blocks: List[SqlSegment]) -> None:
        create_segments_by_table = {
            block.table.lower(): block
            for block in blocks
            if block.block_type == SqlSegmentType.CREATE_TABLE
        }
        for block in blocks:
            if block.block_type != SqlSegmentType.PUBLISH_REPORTS or block.report_component != "metric":
                continue
            source = create_segments_by_table.get(block.table.lower())
            if source is None or source.result_expression is None:
                continue
            output_columns = self._query_output_columns(source.result_expression)
            if not output_columns:
                continue
            normalized_columns = {column.lower() for column in output_columns}
            if {"metric", "value"} <= normalized_columns:
                continue
            raise ValueError(
                f"PUBLISH table '{block.table}' must project 'metric' and 'value' columns "
                f"(optional 'tags') before 'PUBLISH ... TO REPORTS WITH (COMPONENT = METRIC)'. Current output columns: "
                f"{output_columns}. If this is a wide summary table, pivot it into score rows first."
            )

    def _query_output_columns(self, expression: exp.Expression) -> Optional[List[str]]:
        if isinstance(expression, exp.Select):
            columns: List[str] = []
            for projection in expression.expressions:
                alias_name = projection.alias_or_name
                if alias_name:
                    columns.append(alias_name)
                    continue
                if isinstance(projection, exp.Star):
                    return None
                body = projection.this if isinstance(projection, exp.Alias) else projection
                if isinstance(body, exp.Column):
                    column_name = body.alias_or_name or body.name
                    if column_name:
                        columns.append(column_name)
                        continue
                return None
            return columns
        if isinstance(expression, exp.Subquery):
            inner = expression.this
            if isinstance(inner, exp.Expression):
                return self._query_output_columns(inner)
            return None
        if isinstance(expression, exp.SetOperation):
            left = expression.this
            if isinstance(left, exp.Expression):
                return self._query_output_columns(left)
            return None
        return None
