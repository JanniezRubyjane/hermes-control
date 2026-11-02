def normalize_event(event):
    return {key:event[key] for key in ("tenant","id","account","delta")}
