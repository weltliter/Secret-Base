"""One private ECS configuration file, parsed as data and never executed."""
import os
import re
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
NUMBERS = {
    "VAULT_ADMIN_PORT": (10000, 65535),
    "VAULT_BACKUP_TIMEOUT_SECONDS": (1, None),
    "VAULT_BACKUP_RETENTION_DAYS": (1, None),
    "VAULT_BACKUP_QUOTA_BYTES": (1, None),
    "VAULT_BACKUP_RESERVE_BYTES": (0, None),
    "VAULT_DOWNLOAD_TIMEOUT_SECONDS": (1, None),
    "VAULT_DOWNLOAD_CONCURRENCY": (1, None),
}
KEYS = set(NUMBERS) | {"VAULT_PROJECT_NAME", "VAULT_STATE_DIR", "VAULT_APP_IMAGE",
                       "POSTGRES_IMAGE", "PYTHON_BASE", "PIP_INDEX_URL", "VAULT_MODEL_SOURCE"}


def read_config(root=ROOT):
    path = Path(root) / "config.env"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("Create a regular config.env from config.example.env first.")
    values = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)\s*=(.*)", line)
        if not match or match[1] not in KEYS or match[1] in values:
            raise ValueError(f"Invalid, unknown or duplicate config key at line {number}.")
        key, value = match[1], match[2].strip()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"Invalid quoting for {key}.")
            value = value[1:-1]
        if not value or any(ord(c) < 32 for c in value) or any(c in value for c in '$`"\'\\'):
            raise ValueError(f"Invalid literal value for {key}; interpolation is not supported.")
        values[key] = value
    if missing := KEYS - values.keys():
        raise ValueError("Missing config keys: " + ", ".join(sorted(missing)))
    for key, (minimum, maximum) in NUMBERS.items():
        value = values[key]
        if not re.fullmatch(r"[0-9]+", value) or not minimum <= int(value) <= (maximum or 2**63 - 1):
            raise ValueError(f"Invalid number for {key}.")
        values[key] = str(int(value))
    state = PurePosixPath(values["VAULT_STATE_DIR"])
    if (not state.is_absolute() or ".." in state.parts or ":" in str(state) or
            str(state) in ("/", "/var", "/var/lib", "/opt", "/home", "/tmp")):
        raise ValueError("VAULT_STATE_DIR must be an absolute, project-specific Linux directory.")
    if os.name != "nt":
        target = Path(state)
        if any(p.is_symlink() for p in (target, *target.parents)):
            raise ValueError("VAULT_STATE_DIR must not contain symlinks.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", values["VAULT_PROJECT_NAME"]):
        raise ValueError("Invalid VAULT_PROJECT_NAME.")
    for key in ("VAULT_APP_IMAGE", "POSTGRES_IMAGE", "PYTHON_BASE"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:@_-]*", values[key]):
            raise ValueError(f"Invalid image reference for {key}.")
    index = urlsplit(values["PIP_INDEX_URL"])
    if index.scheme != "https" or not index.hostname or index.username or index.password or index.query or index.fragment:
        raise ValueError("PIP_INDEX_URL must be an HTTPS package source without credentials.")
    if values["VAULT_MODEL_SOURCE"] not in ("huggingface.co", "hf-mirror.com"):
        raise ValueError("Unsupported VAULT_MODEL_SOURCE.")
    return values


def compose_invocation(root, values, arguments, inherited=None):
    # Keep OS/Docker connectivity settings, discard all alternative Compose and
    # application configuration sources. The file is authoritative.
    env = {key: value for key, value in (os.environ if inherited is None else inherited).items()
           if not key.startswith(("VAULT_", "COMPOSE_")) and key not in KEYS and key != "DATABASE_URL"}
    env.update(values)
    env["COMPOSE_DISABLE_ENV_FILE"] = "true"
    reserved = {"--env-file", "--project-directory", "--file", "-f", "--project-name", "-p"}
    if any(arg.split("=", 1)[0] in reserved or
           (arg.startswith(("-f", "-p")) and not arg.startswith("--")) for arg in arguments):
        raise ValueError("Set deployment options in config.env; alternate Compose configuration is not supported.")
    root = Path(root).resolve()
    command = ["docker", "compose", "--env-file", str(root / "config.env"),
               "--project-directory", str(root), "-f", str(root / "compose.yaml"), *arguments]
    return command, env


def main():
    try:
        values = read_config()
        action = sys.argv[1] if len(sys.argv) > 1 else "check"
        if action == "check":
            print("ECS config.env validated; values were not displayed.")
        elif action == "state-dir":
            print(values["VAULT_STATE_DIR"])
        elif action == "model-source":
            print(values["VAULT_MODEL_SOURCE"])
        elif action == "compose":
            command, env = compose_invocation(ROOT, values, sys.argv[2:])
            os.execvpe(command[0], command, env)
        else:
            raise ValueError("Usage: config.py [check|state-dir|model-source|compose ...]")
    except (ValueError, OSError) as exc:
        # Never include configuration values or OS error strings in diagnostics.
        raise SystemExit(str(exc) if isinstance(exc, ValueError) else "Unable to read configuration or run Docker.") from None


if __name__ == "__main__":
    main()
