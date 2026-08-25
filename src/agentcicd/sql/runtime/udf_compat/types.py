from __future__ import annotations
import abc
import enum
import json
import traceback
from typing import TYPE_CHECKING, ClassVar, Dict, List, Type, Union, TypeVar, Any, Optional, Sequence

from pydantic import BaseModel, ValidationError

# Lazy import pyspark types - only needed at Spark execution time
if TYPE_CHECKING:
    from pyspark.sql import VariantVal
    from pyspark.sql.types import DataType as SparkDataType


class Err(BaseModel):
    name: str    
    message: str
    stacktrace: Optional[List[str]] = None

    @staticmethod
    def from_exception(e: Exception):
        return Err(
            name=type(e).__name__,
            message=str(e),
            stacktrace=traceback.format_exception(type(e), e, e.__traceback__)
        )


Scalar = Union[str, int, float, bytes, bool, Err]
NullableScalar = Union[Scalar, None]

Json = Union[
    Dict[str, "Json"],  # JSON object
    List["Json"],       # JSON array
    NullableScalar,     # JSON scalar    
]

Record = Dict[str, Json]  # A record is a dictionary with string keys and JSON values
SimpleRecord = Dict[str, NullableScalar]  # A simple record with only scalar values

T = TypeVar("T")


class DTypeSingleton(type):
    """Metaclass for DataType"""

    _instances: ClassVar[Dict[Type["DTypeSingleton"], "DTypeSingleton"]] = {}

    def __call__(cls: Type[T], *args: Any, **kwargs: Any) -> T:
        if cls not in cls._instances:  # type: ignore[attr-defined]
            cls._instances[cls] = super(  # type: ignore[misc, attr-defined]
                DTypeSingleton, cls  # type: ignore
            ).__call__(*args, **kwargs)
        return cls._instances[cls]  # type: ignore[attr-defined]    


def _get_spark_types() -> Dict[str, type["SparkDataType"]]:
    """Lazy import of pyspark types - only called when spark_interface() is used."""
    from pyspark.sql.types import (
        IntegerType as SparkIntegerType,
        FloatType as SparkFloatType,
        StringType as SparkStringType,
        VariantType as SparkVariantType,
        BooleanType as SparkBooleanType,
        ArrayType as SparkArrayType,
        MapType as SparkMapType,
        BinaryType as SparkBinaryType,
    )
    return {
        'int': SparkIntegerType,
        'float': SparkFloatType,
        'string': SparkStringType,
        'variant': SparkVariantType,
        'boolean': SparkBooleanType,
        'array': SparkArrayType,
        'map': SparkMapType,
        'binary': SparkBinaryType,
    }


class DType:
    def from_internal(self, obj: Any) -> Any:
        return obj

    def to_internal(self, obj: Any) -> Any:
        return obj

    def __repr__(self) -> str:
        return self.__class__.__name__ + "()"

    def __hash__(self) -> int:
        return hash(str(self))

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, self.__class__) and self.__dict__ == other.__dict__

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)

    @abc.abstractmethod
    def spark_interface(self) -> type["SparkDataType"]:
        pass


class IntType(DType, metaclass=DTypeSingleton):
    def spark_interface(self):
        return _get_spark_types()['int']

class FloatType(DType, metaclass=DTypeSingleton):
    def spark_interface(self):
        return _get_spark_types()['float']

class StringType(DType, metaclass=DTypeSingleton):
    def spark_interface(self):
        return _get_spark_types()['string']

class BooleanType(DType, metaclass=DTypeSingleton):
    def spark_interface(self):
        return _get_spark_types()['boolean']

class ArrayType(DType, metaclass=DTypeSingleton):
    def spark_interface(self):
        return _get_spark_types()['array']

class MapType(DType, metaclass=DTypeSingleton):
    def spark_interface(self):
        return _get_spark_types()['map']

class BinaryType(DType, metaclass=DTypeSingleton):
    def spark_interface(self):
        return _get_spark_types()['binary']

class JsonType(DType, metaclass=DTypeSingleton):
    def spark_interface(self):
        return _get_spark_types()['variant']

    def from_internal(self, obj: Json) -> Any:
        from pyspark.sql import VariantVal
        return VariantVal.parseJson(json.dumps(obj))

    def to_internal(self, obj: Any) -> Json:
        return json.loads(obj.toJson())

class JsonEncodedPydanticType(JsonType):

    def __init__(self, klass: Type[BaseModel]) -> None:
        super().__init__()
        self._klass = klass

    def from_internal(self, obj: BaseModel) -> Any:
        from pyspark.sql import VariantVal
        return VariantVal.parseJson(obj.model_dump_json())

    def to_internal(self, obj: Any) -> BaseModel:
        json_obj = obj.toJson()
        try:
            return self._klass.model_validate_json(json_obj)
        except ValidationError:
            return Err.model_validate(json_obj)


class FunctionType(DType):
    """SQL-visible function handle type.

    Function values are passed to Spark UDFs as opaque string handles. The SQL
    layer owns signature validation before lowering the handle.
    """

    def __init__(
        self,
        arguments: Sequence[tuple[str, str]] = (),
        return_type_sql: str = "ANY",
    ) -> None:
        self.arguments = tuple((str(name), str(type_sql)) for name, type_sql in arguments)
        self.return_type_sql = str(return_type_sql)

    def spark_interface(self):
        return _get_spark_types()['string']


class FType(enum.Enum):
    BATCH_FUNCTION = "BATCH_FUNCTION"
    ROW_EXPLODE_FUNCTION = "ROW_EXPLODE_FUNCTION"
    AGGREGATE_FUNCTION = "AGGREGATE_FUNCTION"


class AgentCICDModel(BaseModel):
    pass
