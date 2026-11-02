"""Zero stock is retained and can be highlighted in a report."""
def zero_accounts(report):
    result = []
    for row in report["accounts"]:
        if row["balance"] == 0:
            result.append(row["account"])
    return result

def has_zero_stock(report):
    return bool(zero_accounts(report))
