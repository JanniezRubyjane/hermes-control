"""Render invoices as plain text, including literal identifiers."""


def render_invoice(result):
    lines = []
    for line in result["lines"]:
        lines.append(line["id"] + ": " + line["total"])
    lines.extend([
        "Subtotal: " + result["subtotal"],
        "Tax: " + result["tax"],
        "Total: " + result["total"],
    ])
    return "\n".join(lines) + "\n"
