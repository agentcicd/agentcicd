from __future__ import annotations

import os
from typing import Any


try:  # pragma: no cover - exercised when pyspark is installed
    from pyspark.sql import SparkSession
except Exception:  # pragma: no cover
    SparkSession = Any  # type: ignore[misc,assignment]


def s3a_endpoint(endpoint: str, *, secure: bool) -> str:
    normalized = endpoint.strip()
    if not normalized or "://" in normalized:
        return normalized
    scheme = "https" if secure else "http"
    return f"{scheme}://{normalized}"


def default_codegen_conf() -> dict[str, str]:
    """Spark codegen defaults that avoid JVM 64KB generated-method failures."""
    defaults = {
        "spark.sql.codegen.wholeStage": os.getenv("AGENTCICD_SPARK_SQL_CODEGEN_WHOLE_STAGE", "false").strip(),
        "spark.sql.codegen.maxFields": os.getenv("AGENTCICD_SPARK_SQL_CODEGEN_MAX_FIELDS", "50").strip(),
        "spark.sql.codegen.methodSplitThreshold": os.getenv(
            "AGENTCICD_SPARK_SQL_CODEGEN_METHOD_SPLIT_THRESHOLD",
            "1024",
        ).strip(),
    }
    return {key: value for key, value in defaults.items() if value}


def build_spark_session(*, app_name: str = "AgentCICDEngine", enable_delta: bool = False):
    if SparkSession is Any:  # pragma: no cover
        raise RuntimeError("pyspark is not installed")
    builder = SparkSession.builder.appName(app_name)
    for key, value in default_codegen_conf().items():
        builder = builder.config(key, value)
    if enable_delta:
        builder = (
            builder
            .config("spark.jars.packages", "io.delta:delta-spark_2.13:4.2.0")
            .config("spark.sql.catalog.spark_catalog", "org.apache.spark.sql.delta.catalog.DeltaCatalog")
            .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        )
    endpoint = os.getenv("AGENTCICD_DP_MINIO_ENDPOINT", "").strip()
    access_key = os.getenv("AGENTCICD_DP_MINIO_ACCESS_KEY", "").strip()
    secret_key = os.getenv("AGENTCICD_DP_MINIO_SECRET_KEY", "").strip()
    secure = os.getenv("AGENTCICD_DP_MINIO_SECURE", "false").strip().lower() in {"1", "true", "yes", "on"}
    if endpoint:
        builder = (
            builder
            .config("spark.hadoop.fs.s3a.endpoint", s3a_endpoint(endpoint, secure=secure))
            .config("spark.hadoop.fs.s3a.path.style.access", "true")
            .config("spark.hadoop.fs.s3a.connection.ssl.enabled", "true" if secure else "false")
            .config("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem")
        )
    if access_key:
        builder = builder.config("spark.hadoop.fs.s3a.access.key", access_key)
    if secret_key:
        builder = builder.config("spark.hadoop.fs.s3a.secret.key", secret_key)
    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel("ERROR")
    return spark
