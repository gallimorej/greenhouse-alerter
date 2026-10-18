"""Request authentication for both services.

- Worker: Cloud Scheduler sends a Google-signed OIDC token. Cloud Run IAM has already checked that the
  caller holds run.invoker; this checks the audience and the exact scheduler identity as well.
- Admin: IAP signs every request it forwards with ``X-Goog-IAP-JWT-Assertion``. Verifying it means a
  request that somehow bypassed IAP is rejected, and gives the user's email for the audit log, which
  is then checked against an allowlist.

Verifiers are injected into the app, so tests use fakes. There's no "auth off" switch to misconfigure.
"""

from dataclasses import dataclass
from typing import Protocol

IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


class AuthError(Exception):
    pass


class Verifier(Protocol):
    def __call__(self, token: str) -> str:
        """Return the verified caller's email, or raise AuthError."""


@dataclass(frozen=True)
class SchedulerVerifier:
    audience: str
    service_account: str

    def __call__(self, token: str) -> str:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        try:
            claims = id_token.verify_oauth2_token(token, google_requests.Request(), audience=self.audience)
        except ValueError as exc:
            raise AuthError(f"invalid OIDC token: {exc}") from None
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise AuthError("unexpected issuer")
        if not claims.get("email_verified") or claims.get("email") != self.service_account:
            raise AuthError("caller is not the scheduler service account")
        return claims["email"]


@dataclass(frozen=True)
class IapVerifier:
    audience: str
    allowed_emails: frozenset[str]

    def __call__(self, token: str) -> str:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        try:
            claims = id_token.verify_token(
                token, google_requests.Request(), audience=self.audience, certs_url=IAP_CERTS_URL
            )
        except ValueError as exc:
            raise AuthError(f"invalid IAP assertion: {exc}") from None
        if claims.get("iss") != IAP_ISSUER:
            raise AuthError("unexpected issuer")
        email = (claims.get("email") or "").lower()
        if email not in self.allowed_emails:
            raise AuthError("user is not on the admin allowlist")
        return email


def parse_allowlist(value: str) -> frozenset[str]:
    return frozenset(e.strip().lower() for e in value.split(",") if e.strip())
