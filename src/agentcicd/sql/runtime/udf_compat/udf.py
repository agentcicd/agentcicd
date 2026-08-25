import abc
import inspect
from dataclasses import dataclass
from typing import Callable, Tuple, Optional
from .attrs import callable_attr
from .function import Function
from .types import DType, FType


_MISSING = object()


@dataclass(frozen=True)
class Param:
    name: str
    required: bool = True
    type_sql: str = "ANY"
    default_value: object = _MISSING



class Udf:
    _udf_name: Optional[str] = None

    def __init_subclass__(cls, name: Optional[str] = None, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._udf_name = name


    @abc.abstractmethod
    def input_schema(self) -> Tuple[DType, ...]:
        """Return the input schema as a tuple of values."""
        pass

    @abc.abstractmethod
    def input_args(self) -> Tuple[str, ...]:
        """Return SQL-visible argument names for the UDF."""
        pass

    def signature(self) -> Tuple[Param, ...]:
        """Return the authoritative structured SQL-visible argument contract for the UDF."""
        arg_names = tuple(str(item) for item in self.input_args())
        defaults = self._defaulted_argument_names()
        return tuple(
            Param(name=arg_name, required=arg_name.lower() not in defaults)
            for arg_name in arg_names
        )

    def metadata(self) -> dict[str, object]:
        """Return optional registry metadata for validation and runtime planning."""
        return {}

    @abc.abstractmethod
    def output_schema(self) -> DType:
        """Return the output schema as a DType."""
        pass

    @abc.abstractmethod
    def ftype(self) -> FType:
        """Return the function type."""
        pass

    @abc.abstractmethod
    def function(self) -> Callable[..., Function]:
        pass

    def _defaulted_argument_names(self) -> set[str]:
        defaults = callable_attr(self, "defaults")
        if callable(defaults):
            raw_defaults = defaults() or {}
            if isinstance(raw_defaults, dict):
                return {str(key).lower() for key in raw_defaults.keys()}

        try:
            factory = self.function()
            function_object = factory()
            transform = callable_attr(function_object, "transform")
            if transform is None:
                return set()
            signature = inspect.signature(transform)
        except Exception:
            return set()

        defaulted: set[str] = set()
        for name, parameter in signature.parameters.items():
            if name == "self":
                continue
            if parameter.default is not inspect._empty:
                defaulted.add(name.lower())
        return defaulted
