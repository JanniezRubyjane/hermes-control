"""Summarize a materialized import without consuming its source twice."""
from ..domain.tenants import tenants_in
from ..domain.totals import net_delta

def count_rows(rows):
    return sum(1 for _ in rows)

def import_counts(rows):
    rows = list(rows)
    return {"rows": count_rows(rows), "tenants": len(tenants_in(rows)), "net": net_delta(rows)}
