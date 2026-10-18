from types import SimpleNamespace

import pytest

from greenhouse.config import ConfigError, load_secrets, project_id


class FakeSecretClient:
    def __init__(self, values):
        self.values = values

    def access_secret_version(self, request):
        secret_id = request["name"].split("/")[3]
        if secret_id not in self.values:
            raise PermissionError("denied")
        return SimpleNamespace(payload=SimpleNamespace(data=self.values[secret_id].encode()))


ALL = {"SENSORPUSH_USER": "u@x", "SENSORPUSH_PASSWORD": "pw\n", "IFTTT_KEY": "k"}


def test_loads_all_secrets_and_strips_whitespace():
    s = load_secrets(FakeSecretClient(ALL), project="p")
    assert (s.sensorpush_user, s.sensorpush_password, s.ifttt_key) == ("u@x", "pw", "k")


def test_repr_hides_secret_values():
    s = load_secrets(FakeSecretClient(ALL), project="p")
    assert "pw" not in repr(s) and "'k'" not in repr(s)


def test_missing_secret_raises_config_error():
    values = dict(ALL)
    del values["IFTTT_KEY"]
    with pytest.raises(ConfigError, match="IFTTT_KEY"):
        load_secrets(FakeSecretClient(values), project="p")


def test_empty_secret_raises_config_error():
    with pytest.raises(ConfigError, match="empty"):
        load_secrets(FakeSecretClient(ALL | {"IFTTT_KEY": "  "}), project="p")


def test_project_id_required(monkeypatch):
    monkeypatch.delenv("PROJECT_ID", raising=False)
    with pytest.raises(ConfigError):
        project_id()
