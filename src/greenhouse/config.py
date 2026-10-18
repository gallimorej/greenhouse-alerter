"""Secrets, loaded once at startup.

A missing project or secret raises ConfigError, so a misconfigured service fails at boot instead of on
the first request.
"""

import os
from dataclasses import dataclass, field

SECRET_IDS = ("SENSORPUSH_USER", "SENSORPUSH_PASSWORD", "IFTTT_KEY")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Secrets:
    sensorpush_user: str = field(repr=False)
    sensorpush_password: str = field(repr=False)
    ifttt_key: str = field(repr=False)


def project_id() -> str:
    value = os.environ.get("PROJECT_ID")
    if not value:
        raise ConfigError("PROJECT_ID is not set")
    return value


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


def load_secrets(client=None, project: str | None = None) -> Secrets:
    if client is None:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
    project = project or project_id()
    values = {secret_id: read_secret(client, project, secret_id) for secret_id in SECRET_IDS}
    return Secrets(
        sensorpush_user=values["SENSORPUSH_USER"],
        sensorpush_password=values["SENSORPUSH_PASSWORD"],
        ifttt_key=values["IFTTT_KEY"],
    )
