from types import SimpleNamespace

import pytest

from greenhouse.config import ROLE_ENV, ROLE_SECRETS, ConfigError, load_secrets, project_id, require_env


class FakeSecretClient:
    def __init__(self, values):
        self.values = values
        self.requested = []

    def access_secret_version(self, request):
        secret_id = request["name"].split("/")[3]
        self.requested.append(secret_id)
        if secret_id not in self.values:
            raise PermissionError("denied")
        return SimpleNamespace(payload=SimpleNamespace(data=self.values[secret_id].encode()))


ALL = {
    "SENSORPUSH_USER": "u@x",
    "SENSORPUSH_PASSWORD": "pw\n",
    "IFTTT_KEY": "k",
    "FLASK_SECRET_KEY": "flask",
}


def test_loads_secrets_and_strips_whitespace():
    s = load_secrets(ROLE_SECRETS["worker"], FakeSecretClient(ALL), project="p")
    assert (s["SENSORPUSH_USER"], s["SENSORPUSH_PASSWORD"], s["IFTTT_KEY"]) == ("u@x", "pw", "k")


def test_admin_role_never_reads_the_ifttt_key():
    client = FakeSecretClient(ALL)
    s = load_secrets(ROLE_SECRETS["admin"], client, project="p")
    assert "IFTTT_KEY" not in client.requested
    assert "IFTTT_KEY" not in s


def test_repr_hides_secret_values():
    s = load_secrets(ROLE_SECRETS["worker"], FakeSecretClient(ALL), project="p")
    assert "pw" not in repr(s) and "u@x" not in repr(s)


def test_missing_secret_raises_config_error():
    values = dict(ALL)
    del values["IFTTT_KEY"]
    with pytest.raises(ConfigError, match="IFTTT_KEY"):
        load_secrets(ROLE_SECRETS["worker"], FakeSecretClient(values), project="p")


def test_empty_secret_raises_config_error():
    with pytest.raises(ConfigError, match="empty"):
        load_secrets(["IFTTT_KEY"], FakeSecretClient(ALL | {"IFTTT_KEY": "  "}), project="p")


@pytest.mark.parametrize("role", ["worker", "admin"])
def test_require_env_names_every_missing_variable(monkeypatch, role):
    for name in ROLE_ENV[role]:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError) as excinfo:
        require_env(role)
    assert all(name in str(excinfo.value) for name in ROLE_ENV[role])


def test_missing_project_fails_before_touching_google(monkeypatch):
    monkeypatch.delenv("PROJECT_ID", raising=False)
    with pytest.raises(ConfigError, match="PROJECT_ID"):
        load_secrets(["IFTTT_KEY"])  # no client given: must not try to build one


def test_project_id_required(monkeypatch):
    monkeypatch.delenv("PROJECT_ID", raising=False)
    with pytest.raises(ConfigError):
        project_id()
