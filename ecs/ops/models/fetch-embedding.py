"""Fetch public pinned model files only. Never called during query processing."""
import hashlib
import os
import sys
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ops.config import read_config


REVISION = "75c43b069aac4d136ba6bc1122f995fedcfd2781"
FILES = {
    "tokenizer.json": "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26",
    "onnx/model_quantized.onnx": "15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc",
}
config = read_config()
destination = Path(config["VAULT_STATE_DIR"]) / "models/bge-small-zh-v1.5"
destination.mkdir(parents=True, exist_ok=True)
for name, expected in FILES.items():
    path = destination / Path(name).name
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
        print(path.name + ": verified")
        continue
    temporary = path.with_suffix(path.suffix + ".download")
    try:
        url = f"https://{config['VAULT_MODEL_SOURCE']}/Xenova/bge-small-zh-v1.5/resolve/{REVISION}/{name}"
        with urlopen(url, timeout=30) as response, temporary.open("wb") as target:
            while block := response.read(1024*1024):
                target.write(block)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != expected:
            raise SystemExit("Model checksum mismatch; existing file was not changed.")
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    print(path.name + ": downloaded and verified")
