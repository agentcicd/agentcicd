# Architecture Overview

`agentcicd.sql` turns recipe SQL into executable evaluation stages. The intended flow is one-way:

```text
surface parser -> IR -> semantics -> lowering -> execution plan -> backend facade -> backend services
```

The parser/surface layer owns syntax and SQLGlot conversion. IR owns immutable statement and expression models. Semantics owns validation, registry resolution, dependency graphs, and type contracts. Lowering owns conversion from IR into executable SQL and cell expressions. The engine compiles and executes plans against an `ExecutionBackend`.

## Backend Artifact Contract

Backends materialize tables, schema sidecars, stage manifests, error summaries, debug streams, publications, and annotation artifacts. Spark-specific path layout, table I/O, stream staging, stage reuse, debug stream emission, and cell wrapping belong under `engine.backends.spark`.

`SparkExecutionBackend` remains the compatibility facade implementing `ExecutionBackend`. New Spark behavior should be added to a focused backend service first and exposed through the facade only when orchestration needs it.

## Runtime Function Contract

Runtime functions are invoked through typed invocation helpers. Transports construct payloads, handle timeouts, and decode responses. Cell value, error, and latency shaping belongs to shared runtime or cell-semantics helpers, not to HTTP transport code or Spark registration wrappers.

Runtime-control arguments, including injected limiter handles, should be represented in one typed model so fixture and remote invocation behavior can be tested without Spark.

## Debug Stream Contract

Debug row streams are durable artifacts, not progress events. Streams expose full wrapped cells for debugging values, errors, and trace metadata. Row limits and object-store mirroring are part of the debug-stream service contract.

User-visible progress events remain small and stable. Structured diagnostics should flow through observability event sinks, while stage manifests and debug streams remain reviewable artifact contracts.
