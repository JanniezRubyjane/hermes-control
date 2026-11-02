"""Reporting endpoints share the projection and formatting pipeline."""
from ..reporting.summary import report
from ..reporting.text import render, footer
from ..reporting.csv import render_csv

def get_report(events, tenant):
    return report(events, tenant)

def formatted_report(events, tenant, format="text"):
    result = get_report(events, tenant)
    if format == "csv":
        return render_csv(result)
    return render(result) + "\n" + footer(result)
