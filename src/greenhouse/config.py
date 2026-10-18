"""Environment and secrets, loaded once at startup.

A missing setting or secret raises ConfigError, so a misconfigured service fails at boot instead of on
the first request. Each role loads only the secrets it uses: the admin service never reads the IFTTT key.
"""

import os
from collections.abc import Iterable, Mapping

ROLE_SECRETS = {
    "worker": ("SENSORPUSH_USER", "SENSORPUSH_PASSWORD", "IFTTT_KEY"),
    "admin": ("SENSORPUSH_USER", "SENSORPUSH_PASSWORD", "FLASK_SECRET_KEY"),
}


ROLE_ENV = {
    "worker": (
        "PROJECT_ID", "FIRESTORE_DATABASE", "WORKER_AUDIENCE", "SCHEDULER_SERVICE_ACCOUNT", "ALERT_POLICIES",
    ),
    "admin": ("PROJECT_ID", "FIRESTORE_DATABASE", "IAP_AUDIENCE", "ADMIN_EMAILS", "ALERT_POLICIES"),
}


class ConfigError(Exception):
    pass


def require_env(role: str) -> None:
    missing = [name for name in ROLE_ENV[role] if not os.environ.get(name)]
    if missing:
        raise ConfigError(f"missing environment for {role}: {', '.join(missing)}")


class Secrets(Mapping):
    """A read-only mapping whose repr never shows values."""

    def __init__(self, values: dict[str, str]):
        self._values = dict(values)

    def __getitem__(self, key):
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return f"Secrets({sorted(self._values)})"


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def project_id() -> str:
    return env("PROJECT_ID")


def alert_policies() -> tuple[str, ...]:
    """The alert policies a season pause snoozes: comma-separated full resource names."""
    return tuple(p.strip() for p in env("ALERT_POLICIES").split(",") if p.strip())


def read_secret(client, project: str, secret_id: str) -> str:
    name = f"projects/{project}/secrets/{secret_id}/versions/latest"
    try:
        response = client.access_secret_version(request={"name": name})
    except Exception as exc:
        raise ConfigError(f"Could not read secret {secret_id}: {type(exc).__name__}") from exc
    value = response.payload.data.decode("utf-8").strip()
    if not value:
        raise ConfigError(f"Secret {secret_id} is empty")
    return value


def load_secrets(secret_ids: Iterable[str], client=None, project: str | None = None) -> Secrets:
    project = project or project_id()
    if client is None:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
    return Secrets({secret_id: read_secret(client, project, secret_id) for secret_id in secret_ids})
