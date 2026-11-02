"""Decimal conversion and the explicitly unresolved half-cent boundary."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_HALF_EVEN, localcontext


class AmbiguousRounding(ValueError):
    """The caller needs an accounting policy before proceeding."""


def parse_amount(text):
    # Invalid Decimal input can set context flags even when it raises.
    # Keep both successful and failing parsing inside a private context.
    with localcontext():
        if not isinstance(text, str):
            raise ValueError("invalid amount")
        try:
            value = Decimal(text.strip())
        except (InvalidOperation, ValueError):
            raise ValueError("invalid amount") from None
        if not value.is_finite():
            raise ValueError("invalid amount")
        if value < 0:
            raise ValueError("invalid amount")
        return value


def round_cents(amount):
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid amount")
    with localcontext() as context:
        context.prec = max(100, len(amount.as_tuple().digits) + abs(amount.adjusted()) + 10)
        cents = amount * 100
        if cents % 1 == Decimal("0.5"):
            upward = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            even = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
            if upward != even:
                raise AmbiguousRounding("half-cent tie requires policy")
        return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
