def seconds_to_minutes(seconds):
    return seconds / 60

def format_duration(seconds):
    if seconds < 0:
        raise ValueError("negative duration")
    hours, remaining = divmod(seconds, 3600)
    minutes, seconds = divmod(remaining, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"
