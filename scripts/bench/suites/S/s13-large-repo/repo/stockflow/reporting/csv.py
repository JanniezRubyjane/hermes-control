"""CSV output quotes opaque account names, including commas and newlines."""
import csv
from io import StringIO

def csv_rows(report):
    return [(row["account"], row["balance"]) for row in report["accounts"]]

def render_csv(report):
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("account", "balance"))
    writer.writerows(csv_rows(report))
    return output.getvalue()
