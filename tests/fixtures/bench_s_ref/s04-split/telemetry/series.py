def mean(values):
    values = list(values)
    return sum(values) / len(values) if values else None

def moving_average(values, window):
    if window <= 0:
        raise ValueError("window must be positive")
    values = list(values)
    return [sum(values[i:i + window]) / window for i in range(len(values) - window + 1)]
