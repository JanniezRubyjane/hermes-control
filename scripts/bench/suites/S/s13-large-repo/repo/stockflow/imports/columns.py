"""The storage wire format has four required fields."""
def required_columns():
    return ("tenant", "id", "account", "delta")

def missing_columns(row):
    missing = []
    for name in required_columns():
        if name not in row:
            missing.append(name)
    return missing
