def load_config(environ):
    return {"token": environ.get("SERVICE_TOKEN", ""), "timeout": 30}
