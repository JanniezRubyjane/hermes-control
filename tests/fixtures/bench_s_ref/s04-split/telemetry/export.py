import csv
import io
def names(samples):
    return sorted({sample["sensor"] for sample in samples})

def to_csv(samples):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["sensor", "value"])
    for sample in samples:
        writer.writerow([sample["sensor"], sample["value"]])
    return output.getvalue()
