from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def project_path(value, default="."):
    raw = str(value or default).strip() or default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def output_dir(cfg):
    return project_path((cfg.get("output") or {}).get("directory"), "sessions")


def runtime_dir(cfg):
    return project_path((cfg.get("runtime") or {}).get("directory"), "runtime")


def runtime_file(cfg, filename):
    directory = runtime_dir(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / filename
