from __future__ import annotations

ERROR_ARRAY_SQL_TYPE = (
    "ARRAY<STRUCT<code:STRING,message:STRING,source:STRING,path:STRING,"
    "recoverable:BOOLEAN,cause_code:STRING,cause_message:STRING,details:MAP<STRING,STRING>>>"
)

FIXTURE_TRACE_SQL_TYPE = (
    "STRUCT<schema_version:STRING,call_id:STRING,parent_call_id:STRING,trace_id:STRING,"
    "span_id:STRING,parent_span_id:STRING,function_name:STRING,runtime_alias:STRING,backend:STRING,"
    "fixture_id:STRING,image_id:STRING,execution_runtime:STRING,status:STRING,"
    "duration_ms:BIGINT,cache_hit:BOOLEAN,limiter_key:STRING,max_in_flight:BIGINT,pool_name:STRING,"
    "pool_kind:STRING,pool_node_id:STRING,http_status:BIGINT,error_code:STRING,error_message:STRING,"
    "error_type:STRING,artifact_count:BIGINT,summary:STRING,top_error:STRING,span_count:BIGINT,"
    "error_count:BIGINT,trace_summary_path:STRING,trace_spans_path:STRING>"
)
