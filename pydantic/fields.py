from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    FrozenSet,
    Generator,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Pattern,
    Sequence,
    Set,
    Tuple,
    Type,
    Union,
    cast,
)

from . import errors as errors_
from .class_validators import Validator, make_generic_validator
from .error_wrappers import ErrorWrapper
from .errors import NoneIsNotAllowedError
from .types import Json, JsonWrapper
from .typing import AnyCallable, AnyType, Callable, ForwardRef, display_as_type, is_literal_type, literal_values
from .utils import lenient_issubclass, sequence_like
from .validators import constant_validator, dict_validator, find_validators, validate_json

try:
    from typing_extensions import Literal
except ImportError:
    Literal = None  # type: ignore

Required: Any = Ellipsis
NoneType = type(None)

if TYPE_CHECKING:  # pragma: no cover
    from .class_validators import ValidatorCallable  # noqa: F401
    from .error_wrappers import ErrorList
    from .main import BaseConfig, BaseModel  # noqa: F401
    from .schema import Schema  # noqa: F401
    from .types import ModelOrDc  # noqa: F401

    ValidatorsList = List[ValidatorCallable]
    ValidateReturn = Tuple[Optional[Any], Optional[ErrorList]]
    LocStr = Union[Tuple[Union[int, str], ...], str]


# used to be an enum but changed to int's for small performance improvement as less access overhead
SHAPE_SINGLETON = 1
SHAPE_LIST = 2
SHAPE_SET = 3
SHAPE_MAPPING = 4
SHAPE_TUPLE = 5
SHAPE_TUPLE_ELLIPSIS = 6
SHAPE_SEQUENCE = 7
SHAPE_FROZENSET = 8


class Field:
    __slots__ = (
        'type_',
        'sub_fields',
        'key_field',
        'validators',
        'pre_validators',
        'post_validators',
        'default',
        'required',
        'model_config',
        'name',
        'alias',
        'has_alias',
        'schema',
        'validate_always',
        'allow_none',
        'shape',
        'class_validators',
        'parse_json',
    )

    def __init__(
        self,
        *,
        name: str,
        type_: AnyType,
        class_validators: Optional[Dict[str, Validator]],
        model_config: Type['BaseConfig'],
        default: Any = None,
        required: bool = True,
        alias: str = None,
        schema: Optional['Schema'] = None,
    ) -> None:

        self.name: str = name
        self.has_alias: bool = bool(alias)
        self.alias: str = alias or name
        self.type_: type = type_
        self.class_validators = class_validators or {}
        self.default: Any = default
        self.required: bool = required
        self.model_config = model_config
        self.schema: Optional['Schema'] = schema

        self.allow_none: bool = False
        self.validate_always: bool = False
        self.sub_fields: Optional[List[Field]] = None
        self.key_field: Optional[Field] = None
        self.validators: 'ValidatorsList' = []
        self.pre_validators: Optional['ValidatorsList'] = None
        self.post_validators: Optional['ValidatorsList'] = None
        self.parse_json: bool = False
        self.shape: int = SHAPE_SINGLETON
        self.prepare()

    @classmethod
    def infer(
        cls,
        *,
        name: str,
        value: Any,
        annotation: Any,
        class_validators: Optional[Dict[str, Validator]],
        config: Type['BaseConfig'],
    ) -> 'Field':
        schema_from_config = config.get_field_schema(name)
        from .schema import Schema, get_annotation_from_schema  # noqa: F811

        if isinstance(value, Schema):
            schema = value
            value = schema.default
        else:
            schema = Schema(value, **schema_from_config)  # type: ignore
        schema.alias = schema.alias or schema_from_config.get('alias')
        required = value == Required
        annotation = get_annotation_from_schema(annotation, schema)
        return cls(
            name=name,
            type_=annotation,
            alias=schema.alias,
            class_validators=class_validators,
            default=None if required else value,
            required=required,
            model_config=config,
            schema=schema,
        )

    def set_config(self, config: Type['BaseConfig']) -> None:
        self.model_config = config
        schema_from_config = config.get_field_schema(self.name)
        if schema_from_config:
            self.schema = cast('Schema', self.schema)
            self.schema.alias = self.schema.alias or schema_from_config.get('alias')
            self.alias = cast(str, self.schema.alias)

    @property
    def alt_alias(self) -> bool:
        return self.name != self.alias

    def prepare(self) -> None:
        if self.default is not None and self.type_ is None:
            self.type_ = type(self.default)

        if self.type_ is None:
            raise errors_.ConfigError(f'unable to infer type for attribute "{self.name}"')

        if type(self.type_) == ForwardRef:
            # self.type_ is currently a ForwardRef and there's nothing we can do now,
            # user will need to call model.update_forward_refs()
            return

        self.validate_always = getattr(self.type_, 'validate_always', False) or any(
            v.always for v in self.class_validators.values()
        )

        if not self.required and self.default is None:
            self.allow_none = True

        self._type_analysis()
        self._populate_validators()

    def _type_analysis(self) -> None:  # noqa: C901 (ignore complexity)
        # typing interface is horrible, we have to do some ugly checks
        if lenient_issubclass(self.type_, JsonWrapper):
            self.type_ = self.type_.inner_type  # type: ignore
            self.parse_json = True
        elif lenient_issubclass(self.type_, Json):
            self.type_ = Any  # type: ignore
            self.parse_json = True

        if self.type_ is Pattern:
            # python 3.7 only, Pattern is a typing object but without sub fields
            return
        if is_literal_type(self.type_):
            values = literal_values(self.type_)
            if len(values) > 1:
                self.type_ = Union[tuple(Literal[value] for value in values)]
            else:
                return
        origin = getattr(self.type_, '__origin__', None)
        if origin is None:
            # field is not "typing" object eg. Union, Dict, List etc.
            return
        if origin is Callable:
            return
        if origin is Union:
            types_ = []
            for type_ in self.type_.__args__:  # type: ignore
                if type_ is NoneType:  # type: ignore
                    self.required = False
                    self.allow_none = True
                    continue
                types_.append(type_)

            if len(types_) == 1:
                self.type_ = types_[0]
                # re-run to correctly interpret the new self.type_
                self._type_analysis()
            else:
                self.sub_fields = [self._create_sub_type(t, f'{self.name}_{display_as_type(t)}') for t in types_]
            return

        if issubclass(origin, Tuple):  # type: ignore
            self.shape = SHAPE_TUPLE
            self.sub_fields = []
            for i, t in enumerate(self.type_.__args__):  # type: ignore
                if t is Ellipsis:
                    self.type_ = self.type_.__args__[0]  # type: ignore
                    self.shape = SHAPE_TUPLE_ELLIPSIS
                    return
                self.sub_fields.append(self._create_sub_type(t, f'{self.name}_{i}'))
            return

        if issubclass(origin, List):
            # Create self validators
            get_validators = getattr(self.type_, '__get_validators__', None)
            if get_validators:
                self.class_validators.update(
                    {
                        f'list_{i}': Validator(validator, each_item=False, pre=True, always=True, check_fields=False)
                        for i, validator in enumerate(get_validators())
                    }
                )

            self.type_ = self.type_.__args__[0]  # type: ignore
            self.shape = SHAPE_LIST
        elif issubclass(origin, Set):
            self.type_ = self.type_.__args__[0]  # type: ignore
            self.shape = SHAPE_SET
        elif issubclass(origin, FrozenSet):
            self.type_ = self.type_.__args__[0]  # type: ignore
            self.shape = SHAPE_FROZENSET
        elif issubclass(origin, Sequence):
            self.type_ = self.type_.__args__[0]  # type: ignore
            self.shape = SHAPE_SEQUENCE
        elif issubclass(origin, Mapping):
            self.key_field = self._create_sub_type(
                self.type_.__args__[0], 'key_' + self.name, for_keys=True  # type: ignore
            )
            self.type_ = self.type_.__args__[1]  # type: ignore
            self.shape = SHAPE_MAPPING
        elif issubclass(origin, Type):  # type: ignore
            return
        else:
            raise TypeError(f'Fields of type "{origin}" are not supported.')

        # type_ has been refined eg. as the type of a List and sub_fields needs to be populated
        self.sub_fields = [self._create_sub_type(self.type_, '_' + self.name)]

    def _create_sub_type(self, type_: AnyType, name: str, *, for_keys: bool = False) -> 'Field':
        return self.__class__(
            type_=type_,
            name=name,
            class_validators=None if for_keys else {k: v for k, v in self.class_validators.items() if v.each_item},
            model_config=self.model_config,
        )

    def _populate_validators(self) -> None:
        class_validators_ = self.class_validators.values()
        if not self.sub_fields:
            get_validators = getattr(self.type_, '__get_validators__', None)
            v_funcs = (
                *[v.func for v in class_validators_ if v.each_item and v.pre],
                *(get_validators() if get_validators else list(find_validators(self.type_, self.model_config))),
                *[v.func for v in class_validators_ if v.each_item and not v.pre],
            )
            self.validators = self._prep_vals(v_funcs)

        # Add const validator
        self.pre_validators = []
        self.post_validators = []
        if self.schema and self.schema.const:
            self.pre_validators = [make_generic_validator(constant_validator)]

        if class_validators_:
            self.pre_validators += self._prep_vals(v.func for v in class_validators_ if not v.each_item and v.pre)
            self.post_validators = self._prep_vals(v.func for v in class_validators_ if not v.each_item and not v.pre)

        if self.parse_json:
            self.pre_validators.append(make_generic_validator(validate_json))

        self.pre_validators = self.pre_validators or None
        self.post_validators = self.post_validators or None

    @staticmethod
    def _prep_vals(v_funcs: Iterable[AnyCallable]) -> 'ValidatorsList':
        return [make_generic_validator(f) for f in v_funcs if f]

    def validate(
        self, v: Any, values: Dict[str, Any], *, loc: 'LocStr', cls: Optional['ModelOrDc'] = None
    ) -> 'ValidateReturn':

        errors: Optional['ErrorList']
        if self.pre_validators:
            v, errors = self._apply_validators(v, values, loc, cls, self.pre_validators)
            if errors:
                return v, errors

        if v is None:
            if self.allow_none:
                if self.post_validators:
                    return self._apply_validators(v, values, loc, cls, self.post_validators)
                else:
                    return None, None
            else:
                return v, ErrorWrapper(NoneIsNotAllowedError(), loc)

        if self.shape == SHAPE_SINGLETON:
            v, errors = self._validate_singleton(v, values, loc, cls)
        elif self.shape == SHAPE_MAPPING:
            v, errors = self._validate_mapping(v, values, loc, cls)
        elif self.shape == SHAPE_TUPLE:
            v, errors = self._validate_tuple(v, values, loc, cls)
        else:
            #  sequence, list, set, generator, tuple with ellipsis, frozen set
            v, errors = self._validate_sequence_like(v, values, loc, cls)

        if not errors and self.post_validators:
            v, errors = self._apply_validators(v, values, loc, cls, self.post_validators)
        return v, errors

    def _validate_sequence_like(  # noqa: C901 (ignore complexity)
        self, v: Any, values: Dict[str, Any], loc: 'LocStr', cls: Optional['ModelOrDc']
    ) -> 'ValidateReturn':
        """
        Validate sequence-like containers: lists, tuples, sets and generators
        Note that large if-else blocks are necessary to enable Cython
        optimization, which is why we disable the complexity check above.
        """
        if not sequence_like(v):
            e: errors_.PydanticTypeError
            if self.shape == SHAPE_LIST:
                e = errors_.ListError()
            elif self.shape == SHAPE_SET:
                e = errors_.SetError()
            elif self.shape == SHAPE_FROZENSET:
                e = errors_.FrozenSetError()
            else:
                e = errors_.SequenceError()
            return v, ErrorWrapper(e, loc)

        loc = loc if isinstance(loc, tuple) else (loc,)
        result = []
        errors: List[ErrorList] = []
        for i, v_ in enumerate(v):
            v_loc = *loc, i
            r, ee = self._validate_singleton(v_, values, v_loc, cls)
            if ee:
                errors.append(ee)
            else:
                result.append(r)

        if errors:
            return v, errors

        converted: Union[List[Any], Set[Any], FrozenSet[Any], Tuple[Any, ...], Iterator[Any]] = result

        if self.shape == SHAPE_SET:
            converted = set(result)
        elif self.shape == SHAPE_FROZENSET:
            converted = frozenset(result)
        elif self.shape == SHAPE_TUPLE_ELLIPSIS:
            converted = tuple(result)
        elif self.shape == SHAPE_SEQUENCE:
            if isinstance(v, tuple):
                converted = tuple(result)
            elif isinstance(v, set):
                converted = set(result)
            elif isinstance(v, Generator):
                converted = iter(result)
        return converted, None

    def _validate_tuple(
        self, v: Any, values: Dict[str, Any], loc: 'LocStr', cls: Optional['ModelOrDc']
    ) -> 'ValidateReturn':
        e: Optional[Exception] = None
        if not sequence_like(v):
            e = errors_.TupleError()
        else:
            actual_length, expected_length = len(v), len(self.sub_fields)  # type: ignore
            if actual_length != expected_length:
                e = errors_.TupleLengthError(actual_length=actual_length, expected_length=expected_length)

        if e:
            return v, ErrorWrapper(e, loc)

        loc = loc if isinstance(loc, tuple) else (loc,)
        result = []
        errors: List[ErrorList] = []
        for i, (v_, field) in enumerate(zip(v, self.sub_fields)):  # type: ignore
            v_loc = *loc, i
            r, ee = field.validate(v_, values, loc=v_loc, cls=cls)
            if ee:
                errors.append(ee)
            else:
                result.append(r)

        if errors:
            return v, errors
        else:
            return tuple(result), None

    def _validate_mapping(
        self, v: Any, values: Dict[str, Any], loc: 'LocStr', cls: Optional['ModelOrDc']
    ) -> 'ValidateReturn':
        try:
            v_iter = dict_validator(v)
        except TypeError as exc:
            return v, ErrorWrapper(exc, loc)

        loc = loc if isinstance(loc, tuple) else (loc,)
        result, errors = {}, []
        for k, v_ in v_iter.items():
            v_loc = *loc, '__key__'
            key_result, key_errors = self.key_field.validate(k, values, loc=v_loc, cls=cls)  # type: ignore
            if key_errors:
                errors.append(key_errors)
                continue

            v_loc = *loc, k
            value_result, value_errors = self._validate_singleton(v_, values, v_loc, cls)
            if value_errors:
                errors.append(value_errors)
                continue

            result[key_result] = value_result
        if errors:
            return v, errors
        else:
            return result, None

    def _validate_singleton(
        self, v: Any, values: Dict[str, Any], loc: 'LocStr', cls: Optional['ModelOrDc']
    ) -> 'ValidateReturn':
        if self.sub_fields:
            errors = []
            for field in self.sub_fields:
                value, error = field.validate(v, values, loc=loc, cls=cls)
                if error:
                    errors.append(error)
                else:
                    return value, None
            return v, errors
        else:
            return self._apply_validators(v, values, loc, cls, self.validators)

    def _apply_validators(
        self, v: Any, values: Dict[str, Any], loc: 'LocStr', cls: Optional['ModelOrDc'], validators: 'ValidatorsList'
    ) -> 'ValidateReturn':
        for validator in validators:
            try:
                v = validator(cls, v, values, self, self.model_config)
            except (ValueError, TypeError, AssertionError) as exc:
                return v, ErrorWrapper(exc, loc)
        return v, None

    def include_in_schema(self) -> bool:
        """
        False if this is a simple field just allowing None as used in Unions/Optional.
        """
        return self.type_ != NoneType  # type: ignore

    def is_complex(self) -> bool:
        """
        Whether the field is "complex" eg. env variables should be parsed as JSON.
        """
        from .main import BaseModel  # noqa: F811

        return (
            self.shape != SHAPE_SINGLETON
            or lenient_issubclass(self.type_, (BaseModel, list, set, dict))
            or hasattr(self.type_, '__pydantic_model__')  # pydantic dataclass
        )

    def __repr__(self) -> str:
        return f'<Field({self})>'

    def __str__(self) -> str:
        parts = [self.name, 'type=' + display_as_type(self.type_)]

        if self.required:
            parts.append('required')
        else:
            parts.append(f'default={self.default!r}')

        if self.alt_alias:
            parts.append('alias=' + self.alias)
        return ' '.join(parts)
