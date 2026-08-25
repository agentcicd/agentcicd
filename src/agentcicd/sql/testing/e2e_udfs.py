from __future__ import annotations

import json

from agentcicd.sql.runtime.udf_compat.function import BatchFunction
from agentcicd.sql.runtime.udf_compat.types import FType, FloatType, StringType
from agentcicd.sql.runtime.udf_compat.udf import Param, Udf


class NormalizeTextFunction(BatchFunction):
    def transform(self, values):
        normalized = []
        for value in values:
            if value is not None and "NORMALIZE_FAIL" in str(value):
                raise ValueError("normalize failure requested")
            normalized.append(" ".join(str(value or "").strip().lower().split()))
        return normalized


class NormalizeTextUdf(Udf, name="normalize_text"):
    def input_schema(self):
        return (StringType(),)

    def input_args(self):
        return ("text",)

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def function(self):
        return NormalizeTextFunction


class ExtractJsonScoreFunction(BatchFunction):
    def transform(self, values):
        output = []
        for value in values:
            payload = json.loads(value)
            if "score" not in payload:
                raise ValueError("score is missing")
            output.append(float(payload["score"]))
        return output


class ExtractJsonScoreUdf(Udf, name="extract_json_score"):
    def input_schema(self):
        return (StringType(),)

    def input_args(self):
        return ("text",)

    def output_schema(self):
        return FloatType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def function(self):
        return ExtractJsonScoreFunction


class ControlledEchoFunction(BatchFunction):
    def transform(self, values):
        return [f"echo:{value}" for value in values]


class ControlledEchoUdf(Udf, name="controlled_echo"):
    def input_schema(self):
        return (StringType(),)

    def input_args(self):
        return ("text",)

    def signature(self):
        return (
            Param("text", required=True, type_sql="STRING"),
            Param("limiter", required=False, type_sql="RATELIMIT"),
        )

    def output_schema(self):
        return StringType()

    def ftype(self):
        return FType.BATCH_FUNCTION

    def function(self):
        return ControlledEchoFunction
