"""Plain report text and a concise status footer."""
from .counts import activity_counts
from .maximum import max_balance

def render(report):
    return "\n".join(f"{row['account']}: {row['balance']}" for row in report["accounts"])

def footer(report):
    counts = activity_counts(report)
    return f"{counts['active']} active; {counts['zero']} zero; max {max_balance(report)}"
