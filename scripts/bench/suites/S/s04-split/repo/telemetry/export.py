import csv
import io
def names(samples):
    return sorted({sample["sensor"] for sample in samples})
