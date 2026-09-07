import os

APP_NAME = "LideStudyCenter"


def app_data_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def ensure_runtime_dirs() -> dict:
    root = app_data_dir()
    logs = os.path.join(root, "logs")
    artifacts = os.path.join(root, "artifacts")
    os.makedirs(logs, exist_ok=True)
    os.makedirs(artifacts, exist_ok=True)
    return {
        "root": root,
        "config": os.path.join(root, "gui_config.json"),
        "state": os.path.join(root, "state.json"),
        "logs": logs,
        "artifacts": artifacts,
    }
