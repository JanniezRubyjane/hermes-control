"""Round extended lines, then tax; isolate all Decimal operations."""
from decimal import Decimal, localcontext

from .money import parse_amount, round_cents


def calculate(lines, tax_rate="0"):
    with localcontext() as context:
        context.prec = 100
        rate = parse_amount(tax_rate)
        seen = set()
        items = []
        subtotal = Decimal("0.00")
        for line in lines:
            identifier = line.get("id")
            quantity = line.get("qty")
            if (not isinstance(identifier, str) or not identifier
                    or type(quantity) is not int or quantity <= 0):
                raise ValueError("invalid line")
            if identifier in seen:
                raise ValueError("duplicate line id")
            seen.add(identifier)
            amount = parse_amount(line["amount"])
            extended = round_cents(amount * quantity)
            subtotal += extended
            items.append({"id": identifier, "total": format(extended, ".2f")})
        tax = round_cents(subtotal * rate)
        total = subtotal + tax
        return {
            "lines": items,
            "subtotal": format(subtotal, ".2f"),
            "tax": format(tax, ".2f"),
            "total": format(total, ".2f"),
        }
