from enum import Enum


class TableFormat(str, Enum):
    PARQUET = "parquet"
    DELTA = "delta"
