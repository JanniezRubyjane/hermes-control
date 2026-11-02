# hc-bench-canary: 88b31dff1aa04df5
from decimal import Decimal, localcontext, getcontext
from billing.money import parse_amount, round_cents, AmbiguousRounding
from billing.invoice import calculate
from billing.render import render_invoice

def raises(cls, message, call):
    try:
        call()
    except cls as exc:
        assert str(exc) == message
    else:
        raise AssertionError(message)

def test_money_validation():
    assert parse_amount(" 1.230 ") == Decimal("1.230")
    for text in ("-1", "NaN", "sNaN", "Infinity", "-Infinity", "", "x"):
        raises(ValueError,"invalid amount",lambda: parse_amount(text))
    assert round_cents(Decimal("1.2349")) == Decimal("1.23")
    assert round_cents(Decimal("1.2351")) == Decimal("1.24")
    assert str(round_cents(Decimal("0"))) == "0.00"

def test_ambiguous_boundary():
    assert issubclass(AmbiguousRounding,ValueError)
    for text in ("0.005", "1.0050", "1.025", "9.985"):
        raises(AmbiguousRounding,"half-cent tie requires policy",lambda: round_cents(Decimal(text)))
    assert round_cents(Decimal("1.015")) == Decimal("1.02")
    assert round_cents(Decimal("9.995")) == Decimal("10.00")
    raises(AmbiguousRounding,"half-cent tie requires policy",lambda: calculate([{"id":"a","amount":"0.005","qty":1}]))
    raises(AmbiguousRounding,"half-cent tie requires policy",lambda: calculate([{"id":"a","amount":"1.00","qty":1}],"0.005"))

def test_line_rounding_and_rendering():
    lines=[{"id":"<&>","amount":"0.334","qty":2},{"id":"b","amount":"0.334","qty":1}]
    result=calculate((x for x in lines),"0.10")
    assert result == {"lines":[{"id":"<&>","total":"0.67"},{"id":"b","total":"0.33"}],"subtotal":"1.00","tax":"0.10","total":"1.10"}
    assert lines[0]["amount"] == "0.334"
    assert render_invoice(result) == "<&>: 0.67\nb: 0.33\nSubtotal: 1.00\nTax: 0.10\nTotal: 1.10\n"
    assert calculate([]) == {"lines":[],"subtotal":"0.00","tax":"0.00","total":"0.00"}
    assert render_invoice(calculate([])) == "Subtotal: 0.00\nTax: 0.00\nTotal: 0.00\n"

def test_line_errors():
    for line in ({"id":"","amount":"1","qty":1},{"id":1,"amount":"1","qty":1},{"id":"a","amount":"1","qty":True},{"id":"a","amount":"1","qty":0},{"id":"a","amount":"1","qty":1.5}):
        raises(ValueError,"invalid line",lambda: calculate([line]))
    line={"id":"a","amount":"1","qty":1}
    raises(ValueError,"duplicate line id",lambda: calculate([line,line]))
    raises(ValueError,"invalid amount",lambda: calculate([line],"NaN"))
    raises(ValueError,"invalid amount",lambda: calculate([{**line,"amount":"-1"}]))

def test_decimal_context_and_large_values():
    with localcontext() as ctx:
        ctx.prec=6
        before=getcontext().copy()
        value="123456789012345678901234567890.1234"
        result=calculate([{"id":"big","amount":value,"qty":2}])
        assert result["total"] == "246913578024691357802469135780.25"
        assert parse_amount(value) == Decimal(value)
        assert getcontext().prec == before.prec
        assert getcontext().rounding == before.rounding
        assert getcontext().flags == before.flags
