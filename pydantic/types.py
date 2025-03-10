import re
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import new_class
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Dict, List, Optional, Pattern, Type, TypeVar, Union, cast
from uuid import UUID

from . import errors
from .typing import AnyType
from .utils import import_string
from .validators import (
    bytes_validator,
    constr_length_validator,
    constr_strip_whitespace,
    decimal_validator,
    float_validator,
    int_validator,
    number_multiple_validator,
    number_size_validator,
    path_exists_validator,
    path_validator,
    str_validator,
    strict_float_validator,
    strict_int_validator,
    strict_str_validator,
)

try:
    import email_validator
except ImportError:
    email_validator = None

__all__ = [
    'NoneStr',
    'NoneBytes',
    'StrBytes',
    'NoneStrBytes',
    'StrictStr',
    'ConstrainedBytes',
    'conbytes',
    'ConstrainedList',
    'conlist',
    'ConstrainedStr',
    'constr',
    'PyObject',
    'ConstrainedInt',
    'conint',
    'PositiveInt',
    'NegativeInt',
    'ConstrainedFloat',
    'confloat',
    'PositiveFloat',
    'NegativeFloat',
    'ConstrainedDecimal',
    'condecimal',
    'UUID1',
    'UUID3',
    'UUID4',
    'UUID5',
    'FilePath',
    'DirectoryPath',
    'Json',
    'JsonWrapper',
    'SecretStr',
    'SecretBytes',
    'StrictBool',
    'StrictInt',
    'StrictFloat',
    'PaymentCardNumber',
]

NoneStr = Optional[str]
NoneBytes = Optional[bytes]
StrBytes = Union[str, bytes]
NoneStrBytes = Optional[StrBytes]
OptionalInt = Optional[int]
OptionalIntFloat = Union[OptionalInt, float]
OptionalIntFloatDecimal = Union[OptionalIntFloat, Decimal]

if TYPE_CHECKING:  # pragma: no cover
    from .fields import Field
    from .dataclasses import DataclassType  # noqa: F401
    from .main import BaseModel, BaseConfig  # noqa: F401
    from .typing import CallableGenerator

    ModelOrDc = Type[Union['BaseModel', 'DataclassType']]


class ConstrainedBytes(bytes):
    strip_whitespace = False
    min_length: OptionalInt = None
    max_length: OptionalInt = None

    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield bytes_validator
        yield constr_strip_whitespace
        yield constr_length_validator


def conbytes(*, strip_whitespace: bool = False, min_length: int = None, max_length: int = None) -> Type[bytes]:
    # use kwargs then define conf in a dict to aid with IDE type hinting
    namespace = dict(strip_whitespace=strip_whitespace, min_length=min_length, max_length=max_length)
    return type('ConstrainedBytesValue', (ConstrainedBytes,), namespace)


T = TypeVar('T')


# This types superclass should be List[T], but cython chokes on that...
class ConstrainedList(list):  # type: ignore
    # Needed for pydantic to detect that this is a list
    __origin__ = list
    __args__: List[Type[T]]  # type: ignore

    min_items: Optional[int] = None
    max_items: Optional[int] = None
    item_type: Type[T]  # type: ignore

    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield cls.list_length_validator

    @classmethod
    def list_length_validator(cls, v: 'List[T]', field: 'Field', config: 'BaseConfig') -> 'List[T]':
        v_len = len(v)

        if cls.min_items is not None and v_len < cls.min_items:
            raise errors.ListMinLengthError(limit_value=cls.min_items)

        if cls.max_items is not None and v_len > cls.max_items:
            raise errors.ListMaxLengthError(limit_value=cls.max_items)

        return v


def conlist(item_type: Type[T], *, min_items: int = None, max_items: int = None) -> Type[List[T]]:
    # __args__ is needed to conform to typing generics api
    namespace = {'min_items': min_items, 'max_items': max_items, 'item_type': item_type, '__args__': [item_type]}
    # We use new_class to be able to deal with Generic types
    return new_class('ConstrainedListValue', (ConstrainedList,), {}, lambda ns: ns.update(namespace))


class ConstrainedStr(str):
    strip_whitespace = False
    min_length: OptionalInt = None
    max_length: OptionalInt = None
    curtail_length: OptionalInt = None
    regex: Optional[Pattern[str]] = None
    strict = False

    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield strict_str_validator if cls.strict else str_validator
        yield constr_strip_whitespace
        yield constr_length_validator
        yield cls.validate

    @classmethod
    def validate(cls, value: str) -> str:
        if cls.curtail_length and len(value) > cls.curtail_length:
            value = value[: cls.curtail_length]

        if cls.regex:
            if not cls.regex.match(value):
                raise errors.StrRegexError(pattern=cls.regex.pattern)

        return value


def constr(
    *,
    strip_whitespace: bool = False,
    strict: bool = False,
    min_length: int = None,
    max_length: int = None,
    curtail_length: int = None,
    regex: str = None,
) -> Type[str]:
    # use kwargs then define conf in a dict to aid with IDE type hinting
    namespace = dict(
        strip_whitespace=strip_whitespace,
        strict=strict,
        min_length=min_length,
        max_length=max_length,
        curtail_length=curtail_length,
        regex=regex and re.compile(regex),
    )
    return type('ConstrainedStrValue', (ConstrainedStr,), namespace)


class StrictStr(ConstrainedStr):
    strict = True


class StrictBool(int):
    """
    StrictBool to allow for bools which are not type-coerced.
    """

    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield cls.validate

    @classmethod
    def validate(cls, value: Any) -> bool:
        """
        Ensure that we only allow bools.
        """
        if isinstance(value, bool):
            return value

        raise errors.StrictBoolError()


class PyObject:
    validate_always = True

    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield cls.validate

    @classmethod
    def validate(cls, value: Any) -> Any:
        if isinstance(value, Callable):  # type: ignore
            return value

        try:
            value = str_validator(value)
        except errors.StrError:
            raise errors.PyObjectError(error_message='value is neither a valid import path not a valid callable')

        try:
            return import_string(value)
        except ImportError as e:
            raise errors.PyObjectError(error_message=str(e))


class ConstrainedNumberMeta(type):
    def __new__(cls, name: str, bases: Any, dct: Dict[str, Any]) -> 'ConstrainedInt':
        new_cls = cast('ConstrainedInt', type.__new__(cls, name, bases, dct))

        if new_cls.gt is not None and new_cls.ge is not None:
            raise errors.ConfigError('bounds gt and ge cannot be specified at the same time')
        if new_cls.lt is not None and new_cls.le is not None:
            raise errors.ConfigError('bounds lt and le cannot be specified at the same time')

        return new_cls


class ConstrainedInt(int, metaclass=ConstrainedNumberMeta):
    strict: bool = False
    gt: OptionalInt = None
    ge: OptionalInt = None
    lt: OptionalInt = None
    le: OptionalInt = None
    multiple_of: OptionalInt = None

    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':

        yield strict_int_validator if cls.strict else int_validator
        yield number_size_validator
        yield number_multiple_validator


def conint(
    *, strict: bool = False, gt: int = None, ge: int = None, lt: int = None, le: int = None, multiple_of: int = None
) -> Type[int]:
    # use kwargs then define conf in a dict to aid with IDE type hinting
    namespace = dict(strict=strict, gt=gt, ge=ge, lt=lt, le=le, multiple_of=multiple_of)
    return type('ConstrainedIntValue', (ConstrainedInt,), namespace)


class PositiveInt(ConstrainedInt):
    gt = 0


class NegativeInt(ConstrainedInt):
    lt = 0


class StrictInt(ConstrainedInt):
    strict = True


class ConstrainedFloat(float, metaclass=ConstrainedNumberMeta):
    strict: bool = False
    gt: OptionalIntFloat = None
    ge: OptionalIntFloat = None
    lt: OptionalIntFloat = None
    le: OptionalIntFloat = None
    multiple_of: OptionalIntFloat = None

    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield strict_float_validator if cls.strict else float_validator
        yield number_size_validator
        yield number_multiple_validator


def confloat(
    *,
    strict: bool = False,
    gt: float = None,
    ge: float = None,
    lt: float = None,
    le: float = None,
    multiple_of: float = None,
) -> Type[float]:
    # use kwargs then define conf in a dict to aid with IDE type hinting
    namespace = dict(strict=strict, gt=gt, ge=ge, lt=lt, le=le, multiple_of=multiple_of)
    return type('ConstrainedFloatValue', (ConstrainedFloat,), namespace)


class PositiveFloat(ConstrainedFloat):
    gt = 0


class NegativeFloat(ConstrainedFloat):
    lt = 0


class StrictFloat(ConstrainedFloat):
    strict = True


class ConstrainedDecimal(Decimal, metaclass=ConstrainedNumberMeta):
    gt: OptionalIntFloatDecimal = None
    ge: OptionalIntFloatDecimal = None
    lt: OptionalIntFloatDecimal = None
    le: OptionalIntFloatDecimal = None
    max_digits: OptionalInt = None
    decimal_places: OptionalInt = None
    multiple_of: OptionalIntFloatDecimal = None

    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield decimal_validator
        yield number_size_validator
        yield number_multiple_validator
        yield cls.validate

    @classmethod
    def validate(cls, value: Decimal) -> Decimal:
        digit_tuple, exponent = value.as_tuple()[1:]
        if exponent in {'F', 'n', 'N'}:
            raise errors.DecimalIsNotFiniteError()

        if exponent >= 0:
            # A positive exponent adds that many trailing zeros.
            digits = len(digit_tuple) + exponent
            decimals = 0
        else:
            # If the absolute value of the negative exponent is larger than the
            # number of digits, then it's the same as the number of digits,
            # because it'll consume all of the digits in digit_tuple and then
            # add abs(exponent) - len(digit_tuple) leading zeros after the
            # decimal point.
            if abs(exponent) > len(digit_tuple):
                digits = decimals = abs(exponent)
            else:
                digits = len(digit_tuple)
                decimals = abs(exponent)
        whole_digits = digits - decimals

        if cls.max_digits is not None and digits > cls.max_digits:
            raise errors.DecimalMaxDigitsError(max_digits=cls.max_digits)

        if cls.decimal_places is not None and decimals > cls.decimal_places:
            raise errors.DecimalMaxPlacesError(decimal_places=cls.decimal_places)

        if cls.max_digits is not None and cls.decimal_places is not None:
            expected = cls.max_digits - cls.decimal_places
            if whole_digits > expected:
                raise errors.DecimalWholeDigitsError(whole_digits=expected)

        return value


def condecimal(
    *,
    gt: Decimal = None,
    ge: Decimal = None,
    lt: Decimal = None,
    le: Decimal = None,
    max_digits: int = None,
    decimal_places: int = None,
    multiple_of: Decimal = None,
) -> Type[Decimal]:
    # use kwargs then define conf in a dict to aid with IDE type hinting
    namespace = dict(
        gt=gt, ge=ge, lt=lt, le=le, max_digits=max_digits, decimal_places=decimal_places, multiple_of=multiple_of
    )
    return type('ConstrainedDecimalValue', (ConstrainedDecimal,), namespace)


class UUID1(UUID):
    _required_version = 1


class UUID3(UUID):
    _required_version = 3


class UUID4(UUID):
    _required_version = 4


class UUID5(UUID):
    _required_version = 5


class FilePath(Path):
    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield path_validator
        yield path_exists_validator
        yield cls.validate

    @classmethod
    def validate(cls, value: Path) -> Path:
        if not value.is_file():
            raise errors.PathNotAFileError(path=value)

        return value


class DirectoryPath(Path):
    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield path_validator
        yield path_exists_validator
        yield cls.validate

    @classmethod
    def validate(cls, value: Path) -> Path:
        if not value.is_dir():
            raise errors.PathNotADirectoryError(path=value)

        return value


class JsonWrapper:
    pass


class JsonMeta(type):
    def __getitem__(self, t: AnyType) -> Type[JsonWrapper]:
        return type('JsonWrapperValue', (JsonWrapper,), {'inner_type': t})


class Json(metaclass=JsonMeta):
    pass


class SecretStr:
    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield str_validator
        yield cls.validate

    @classmethod
    def validate(cls, value: str) -> 'SecretStr':
        return cls(value)

    def __init__(self, value: str):
        self._secret_value = value

    def __repr__(self) -> str:
        return "SecretStr('**********')" if self._secret_value else "SecretStr('')"

    def __str__(self) -> str:
        return self.__repr__()

    def display(self) -> str:
        return '**********' if self._secret_value else ''

    def get_secret_value(self) -> str:
        return self._secret_value


class SecretBytes:
    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield bytes_validator
        yield cls.validate

    @classmethod
    def validate(cls, value: bytes) -> 'SecretBytes':
        return cls(value)

    def __init__(self, value: bytes):
        self._secret_value = value

    def __repr__(self) -> str:
        return "SecretBytes(b'**********')" if self._secret_value else "SecretBytes(b'')"

    def __str__(self) -> str:
        return self.__repr__()

    def display(self) -> str:
        return '**********' if self._secret_value else ''

    def get_secret_value(self) -> bytes:
        return self._secret_value


class PaymentCardBrand(Enum):
    amex = 'American Express'
    mastercard = 'Mastercard'
    visa = 'Visa'
    other = 'other'

    def __str__(self) -> str:
        return self.value


class PaymentCardNumber(str):
    """
    Based on: https://en.wikipedia.org/wiki/Payment_card_number
    """

    strip_whitespace: ClassVar[bool] = True
    min_length: ClassVar[int] = 12
    max_length: ClassVar[int] = 19
    bin: str
    last4: str
    brand: PaymentCardBrand

    def __init__(self, card_number: str):
        self.bin = card_number[:6]
        self.last4 = card_number[-4:]
        self.brand = self._get_brand(card_number)

    @classmethod
    def __get_validators__(cls) -> 'CallableGenerator':
        yield str_validator
        yield constr_strip_whitespace
        yield constr_length_validator
        yield cls.validate_digits
        yield cls.validate_luhn_check_digit
        yield cls
        yield cls.validate_length_for_brand

    @property
    def masked(self) -> str:
        num_masked = len(self) - 10  # len(bin) + len(last4) == 10
        return f'{self.bin}{"*" * num_masked}{self.last4}'

    @classmethod
    def validate_digits(cls, card_number: str) -> str:
        if not card_number.isdigit():
            raise errors.NotDigitError
        return card_number

    @classmethod
    def validate_luhn_check_digit(cls, card_number: str) -> str:
        """
        Based on: https://en.wikipedia.org/wiki/Luhn_algorithm
        """
        sum_ = int(card_number[-1])
        length = len(card_number)
        parity = length % 2
        for i in range(length - 1):
            digit = int(card_number[i])
            if i % 2 == parity:
                digit *= 2
            sum_ += digit
        valid = sum_ % 10 == 0
        if not valid:
            raise errors.LuhnValidationError
        return card_number

    @classmethod
    def validate_length_for_brand(cls, card_number: 'PaymentCardNumber') -> 'PaymentCardNumber':
        """
        Validate length based on BIN for major brands:
        https://en.wikipedia.org/wiki/Payment_card_number#Issuer_identification_number_(IIN)
        """
        required_length: Optional[int] = None
        if card_number.brand is (PaymentCardBrand.visa or PaymentCardBrand.mastercard):
            required_length = 16
            valid = len(card_number) == required_length
        elif card_number.brand is PaymentCardBrand.amex:
            required_length = 15
            valid = len(card_number) == required_length
        else:
            valid = True
        if not valid:
            raise errors.InvalidLengthForBrand(brand=card_number.brand, required_length=required_length)
        return card_number

    @staticmethod
    def _get_brand(card_number: str) -> PaymentCardBrand:
        if card_number[0] == '4':
            brand = PaymentCardBrand.visa
        elif 51 <= int(card_number[:2]) <= 55:
            brand = PaymentCardBrand.mastercard
        elif card_number[:2] in {'34', '37'}:
            brand = PaymentCardBrand.amex
        else:
            brand = PaymentCardBrand.other
        return brand
