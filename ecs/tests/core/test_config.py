"""Deployment configuration is data, authoritative, and isolated from shell env."""
from pathlib import Path

import pytest

from ops.config import compose_invocation, read_config


TEMPLATE = Path(__file__).resolve().parents[2] / "config.example.env"


@pytest.fixture
def configuration(tmp_path):
    (tmp_path / "config.env").write_bytes(TEMPLATE.read_bytes())
    return tmp_path


def test_file_is_required_instead_of_implicit_env_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_STATE_DIR", "/tmp/inherited")
    (tmp_path / ".env").write_text("VAULT_STATE_DIR=/tmp/legacy\n")
    with pytest.raises(ValueError, match="config.env"):
        read_config(tmp_path)


def test_valid_file_and_literal_quoted_path(configuration):
    path = configuration / "config.env"
    path.write_text(path.read_text().replace("/var/lib/wechat-vault", '"/tmp/vault state"'))
    assert read_config(configuration)["VAULT_STATE_DIR"] == "/tmp/vault state"


@pytest.mark.parametrize("old,new", [
    ("VAULT_ADMIN_PORT=18080", "VAULT_ADMIN_PORT=9999"),
    ("VAULT_ADMIN_PORT=18080", "VAULT_ADMIN_PORT=65536"),
    ("VAULT_ADMIN_PORT=18080", "VAULT_ADMIN_PORT=private-value-sentinel"),
    ("VAULT_STATE_DIR=/var/lib/wechat-vault", "VAULT_STATE_DIR=/tmp"),
    ("VAULT_STATE_DIR=/var/lib/wechat-vault", "VAULT_STATE_DIR=/tmp/../vault"),
    ("VAULT_STATE_DIR=/var/lib/wechat-vault", "VAULT_STATE_DIR=${private-value-sentinel}"),
    ("VAULT_STATE_DIR=/var/lib/wechat-vault", "VAULT_STATE_DIR=$(private-value-sentinel)"),
    ("VAULT_STATE_DIR=/var/lib/wechat-vault", 'VAULT_STATE_DIR="private-value-sentinel'),
    ("VAULT_PROJECT_NAME=wechat-vault", "VAULT_PROJECT_NAME=INVALID"),
    ("PIP_INDEX_URL=https://pypi.org/simple", "PIP_INDEX_URL=http://example.invalid"),
    ("PIP_INDEX_URL=https://pypi.org/simple", "PIP_INDEX_URL=https://example.invalid/?token=private-value-sentinel"),
    ("VAULT_DOWNLOAD_CONCURRENCY=2", "VAULT_DOWNLOAD_CONCURRENCY=0"),
    ("VAULT_MODEL_SOURCE=huggingface.co", "VAULT_MODEL_SOURCE=example.invalid"),
    ("VAULT_MODEL_SOURCE=huggingface.co", ""),
    ("VAULT_MODEL_SOURCE=huggingface.co", "UNKNOWN_KEY=private-value-sentinel"),
    ("VAULT_MODEL_SOURCE=huggingface.co", "VAULT_MODEL_SOURCE=hf-mirror.com\nVAULT_MODEL_SOURCE=huggingface.co"),
])
def test_bad_configuration_is_rejected_without_echoing_values(configuration, old, new):
    path = configuration / "config.env"
    path.write_text(path.read_text().replace(old, new))
    with pytest.raises(ValueError) as error:
        read_config(configuration)
    assert "private-value-sentinel" not in str(error.value)


def test_compose_uses_one_file_and_ignores_ambient_overrides(configuration):
    values = read_config(configuration)
    inherited = {"VAULT_STATE_DIR": "/tmp/unrelated", "COMPOSE_FILE": "rogue.yaml",
                 "COMPOSE_PROJECT_NAME": "rogue", "PIP_INDEX_URL": "https://wrong.invalid",
                 "DATABASE_URL": "wrong", "PATH": "system-path", "VAULT_UNSUPPORTED": "wrong"}
    command, env = compose_invocation(configuration, values, ["config", "--quiet"], inherited)
    assert command[:4] == ["docker", "compose", "--env-file", str(configuration / "config.env")]
    assert command[-2:] == ["config", "--quiet"]
    assert env["VAULT_STATE_DIR"] == values["VAULT_STATE_DIR"]
    assert env["PIP_INDEX_URL"] == values["PIP_INDEX_URL"]
    assert env["COMPOSE_DISABLE_ENV_FILE"] == "true"
    assert env["PATH"] == "system-path"
    assert not {"COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "DATABASE_URL", "VAULT_UNSUPPORTED"} & env.keys()


@pytest.mark.parametrize("option", ["--env-file=another.env", "-f", "-fother.yaml", "-pother", "--project-directory", "--project-name"])
def test_compose_rejects_alternative_configuration(configuration, option):
    with pytest.raises(ValueError):
        compose_invocation(configuration, read_config(configuration), [option], {})
