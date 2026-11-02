from .money import parse_amount, round_cents

def calculate(lines, tax_rate="0"):
    items=[{"id":line["id"], "total":format(round_cents(parse_amount(line["amount"])*line["qty"]),".2f")} for line in lines]
    subtotal=sum(parse_amount(line["total"]) for line in items)
    tax=round_cents(subtotal*parse_amount(tax_rate))
    return {"lines":items,"subtotal":format(subtotal,".2f"),"tax":format(tax,".2f"),"total":format(subtotal+tax,".2f")}
