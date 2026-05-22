# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import jwt
from jwt import algorithms
from jwt import (
    DecodeError,
    ExpiredSignatureError,
    ImmatureSignatureError,
    InvalidAlgorithmError,
    InvalidAudienceError,
    InvalidIssuedAtError,
    InvalidIssuerError,
    InvalidSignatureError,
    InvalidTokenError,
    MissingRequiredClaimError,
    PyJWK,
    PyJWKClient,
    PyJWKClientError,
)
from jwt.algorithms import HMACAlgorithm, NoneAlgorithm

KUBERNETES_SERVICE_HOST = "https://kubernetes.default.svc"
KUBERNETES_CA_CERT_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
KUBERNETES_TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
SIGNING_KEY_CACHE_MAX_SIZE = 32
SIGNING_KEY_CACHE_TTL_SECONDS = 3600
PUBLIC_KEY_ALGORITHMS: tuple[str, ...] = tuple(
    name
    for name, algorithm in algorithms.get_default_algorithms().items()
    if not isinstance(algorithm, HMACAlgorithm | NoneAlgorithm)
)

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when a bearer token cannot be validated against any role."""


@dataclass(frozen=True)
class RoleConfig:
    name: str
    audience: str
    issuer: str
    jwks_uri: str | None = None
    claims: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "RoleConfig":
        name = _required_string(data, "name", context="role")
        audience = _required_string(data, "audience", context=f"role '{name}'")
        issuer = _required_string(data, "issuer", context=f"role '{name}'")
        jwks_uri = _optional_string(
            data, "jwks-uri", context=f"role '{name}'", alias="jwks_uri"
        )

        raw_claims = data.get("claims", {})
        if not isinstance(raw_claims, dict):
            raise ValueError(f"role '{name}' claims must be a table")
        claims: dict[str, str] = {}
        for key, value in raw_claims.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"role '{name}' claim names must be non-empty strings")
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"role '{name}' claim '{key}' must be a non-empty string"
                )
            claims[key] = value
        return cls(
            name=name,
            audience=audience,
            issuer=issuer,
            jwks_uri=jwks_uri,
            claims=claims,
        )


@dataclass(frozen=True)
class ValidatedClaims:
    role: str
    claims: dict[str, Any]

    @property
    def subject(self) -> str:
        sub = self.claims.get("sub")
        return sub if isinstance(sub, str) else ""


@dataclass(frozen=True)
class _CachedSigningKey:
    key: PyJWK
    expires_at: float


class _SigningKeyCache:
    def __init__(
        self,
        *,
        max_size: int = SIGNING_KEY_CACHE_MAX_SIZE,
        ttl_seconds: int = SIGNING_KEY_CACHE_TTL_SECONDS,
    ) -> None:
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[tuple[str, str, str], _CachedSigningKey] = (
            OrderedDict()
        )

    def get(self, cache_key: tuple[str, str, str]) -> PyJWK | None:
        now = time.monotonic()
        entry = self._entries.get(cache_key)
        if entry is None:
            return None
        if entry.expires_at <= now:
            del self._entries[cache_key]
            return None
        self._entries.move_to_end(cache_key)
        return entry.key

    def set(self, cache_key: tuple[str, str, str], signing_key: PyJWK) -> None:
        if self._max_size <= 0:
            return
        self._entries[cache_key] = _CachedSigningKey(
            key=signing_key,
            expires_at=time.monotonic() + self._ttl_seconds,
        )
        self._entries.move_to_end(cache_key)
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)


class TokenValidator:
    def __init__(self, roles: list[RoleConfig]) -> None:
        if not roles:
            raise ValueError("TokenValidator requires at least one role")
        seen: set[str] = set()
        roles_by_issuer: dict[str, list[RoleConfig]] = {}
        for role in roles:
            if role.name in seen:
                raise ValueError(f"duplicate role '{role.name}'")
            seen.add(role.name)
            roles_by_issuer.setdefault(role.issuer, []).append(role)
        self._roles = list(roles)
        self._roles_by_issuer = roles_by_issuer
        self._clients: dict[str, PyJWKClient] = {
            issuer: PyJWKClient(_jwks_uri_for_issuer(issuer_roles), cache_keys=False)
            for issuer, issuer_roles in roles_by_issuer.items()
        }
        self._signing_key_cache = _SigningKeyCache()

    def validate(self, bearer_token: str) -> ValidatedClaims:
        if not bearer_token:
            raise AuthError("empty bearer token")
        last_error: Exception | None = None
        token_summary = _token_summary(bearer_token)
        issuer = _unverified_issuer(bearer_token)
        if issuer not in self._roles_by_issuer:
            raise AuthError(
                f"token did not include a configured issuer: {token_summary}"
            )
        roles = self._roles_by_issuer[issuer]
        client = self._clients[issuer]
        try:
            signing_key = self._signing_key_for_token(
                client=client,
                issuer=issuer,
                bearer_token=bearer_token,
            )
        except PyJWKClientError as exc:
            logger.warning(
                "Bearer token rejected for issuer '%s': %s (%s)",
                issuer,
                _auth_failure_reason(exc),
                token_summary,
            )
            raise AuthError(
                f"token did not validate against configured issuer '{issuer}': {exc}"
            ) from exc

        for role in roles:
            try:
                claims = jwt.decode(
                    bearer_token,
                    key=signing_key.key,
                    algorithms=list(PUBLIC_KEY_ALGORITHMS),
                    audience=role.audience,
                    issuer=role.issuer,
                    options={"require": ["exp", "iat", "iss", "aud"]},
                )
            except (InvalidTokenError, PyJWKClientError) as exc:
                last_error = exc
                logger.warning(
                    "Bearer token rejected for role '%s': %s (%s)",
                    role.name,
                    _auth_failure_reason(exc),
                    token_summary,
                )
                continue
            claim_mismatches = _claim_mismatches(claims, role.claims)
            if claim_mismatches:
                last_error = AuthError(
                    f"required claims for role '{role.name}' do not match"
                )
                logger.warning(
                    "Bearer token rejected for role '%s': required claims do not match: %s (%s)",
                    role.name,
                    ", ".join(claim_mismatches),
                    token_summary,
                )
                continue
            logger.info(
                "Bearer token accepted for role '%s' (%s)", role.name, token_summary
            )
            return ValidatedClaims(role=role.name, claims=claims)
        raise AuthError(
            f"token did not validate against any configured role: {last_error}"
        )

    def _signing_key_for_token(
        self, *, client: PyJWKClient, issuer: str, bearer_token: str
    ) -> PyJWK:
        cache_key = _signing_key_cache_key(issuer, bearer_token)
        if cache_key is not None:
            cached_key = self._signing_key_cache.get(cache_key)
            if cached_key is not None:
                return cached_key

        signing_key = client.get_signing_key_from_jwt(bearer_token)
        if cache_key is not None:
            self._signing_key_cache.set(cache_key, signing_key)
        return signing_key


def extract_bearer_token(authorization_header: str | None) -> str:
    if authorization_header is None:
        raise AuthError("missing Authorization header")
    if not authorization_header.startswith("Bearer "):
        raise AuthError("expected a Bearer token")
    token = authorization_header[len("Bearer ") :].strip()
    if not token:
        raise AuthError("empty bearer token")
    return token


def _claim_mismatches(
    token_claims: dict[str, Any], required: dict[str, str]
) -> list[str]:
    mismatches = []
    for key, expected in required.items():
        actual = token_claims.get(key)
        if actual != expected:
            mismatches.append(
                f"{key} expected {_preview(expected)} got {_preview(actual)}"
            )
    return mismatches


def _token_summary(token: str) -> str:
    try:
        header = jwt.get_unverified_header(token)
    except DecodeError:
        header = {}
    try:
        claims = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
                "verify_aud": False,
                "verify_iss": False,
            },
        )
    except DecodeError:
        claims = {}
    parts = [
        f"alg={_preview(header.get('alg'))}",
        f"kid={_preview(header.get('kid'))}",
        f"iss={_preview(claims.get('iss'))}",
        f"aud={_preview(claims.get('aud'))}",
        f"sub={_preview(claims.get('sub'))}",
    ]
    return "token " + " ".join(parts)


def _unverified_issuer(token: str) -> str | None:
    try:
        claims = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
                "verify_aud": False,
                "verify_iss": False,
            },
        )
    except InvalidTokenError:
        return None
    issuer = claims.get("iss")
    return issuer if isinstance(issuer, str) else None


def _signing_key_cache_key(issuer: str, token: str) -> tuple[str, str, str] | None:
    try:
        header = jwt.get_unverified_header(token)
    except DecodeError:
        return None
    algorithm = header.get("alg")
    key_id = header.get("kid")
    if not isinstance(algorithm, str) or not isinstance(key_id, str):
        return None
    return issuer, key_id, algorithm


def _auth_failure_reason(exc: Exception) -> str:
    if isinstance(exc, PyJWKClientError):
        return f"signing key lookup failed: {exc}"
    if isinstance(exc, InvalidAudienceError):
        return f"audience validation failed: {exc}"
    if isinstance(exc, InvalidIssuerError):
        return f"issuer validation failed: {exc}"
    if isinstance(exc, InvalidSignatureError):
        return f"signature validation failed: {exc}"
    if isinstance(exc, ExpiredSignatureError):
        return f"token is expired: {exc}"
    if isinstance(exc, ImmatureSignatureError):
        return f"token is not yet valid: {exc}"
    if isinstance(exc, InvalidIssuedAtError):
        return f"issued-at validation failed: {exc}"
    if isinstance(exc, MissingRequiredClaimError):
        return f"required claim is missing: {exc}"
    if isinstance(exc, InvalidAlgorithmError):
        return f"algorithm validation failed: {exc}"
    return f"token validation failed: {exc}"


def _preview(value: object, *, limit: int = 80) -> str:
    text = repr(value)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _jwks_uri_for_role(role: RoleConfig) -> str:
    if role.jwks_uri is not None:
        return role.jwks_uri

    openid_configuration_url = (
        f"{role.issuer.rstrip('/')}/.well-known/openid-configuration"
    )
    try:
        response = _get_openid_configuration(role.issuer, openid_configuration_url)
        response.raise_for_status()
        openid_configuration = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError(
            "failed to discover JWKS URI for "
            f"role '{role.name}' from '{openid_configuration_url}': {exc}"
        ) from exc

    if not isinstance(openid_configuration, dict):
        raise ValueError(
            f"OpenID configuration for role '{role.name}' must be a JSON object"
        )

    jwks_uri = openid_configuration.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri.strip():
        raise ValueError(
            f"OpenID configuration for role '{role.name}' must include jwks_uri"
        )
    return jwks_uri


def _jwks_uri_for_issuer(roles: list[RoleConfig]) -> str:
    explicit_uris = {role.jwks_uri for role in roles if role.jwks_uri is not None}
    if len(explicit_uris) > 1:
        raise ValueError(
            f"roles for issuer '{roles[0].issuer}' must use the same jwks-uri"
        )
    if explicit_uris:
        return next(iter(explicit_uris))
    return _jwks_uri_for_role(roles[0])


def _get_openid_configuration(issuer: str, url: str) -> httpx.Response:
    kwargs: dict[str, Any] = {"timeout": 10.0}
    if issuer == KUBERNETES_SERVICE_HOST:
        kwargs.update(_kubernetes_request_options())
    return httpx.get(url, **kwargs)


def _kubernetes_request_options() -> dict[str, Any]:
    options: dict[str, Any] = {}
    if KUBERNETES_CA_CERT_PATH.exists():
        options["verify"] = KUBERNETES_CA_CERT_PATH
    if KUBERNETES_TOKEN_PATH.exists():
        token = KUBERNETES_TOKEN_PATH.read_text().strip()
        if token:
            options["headers"] = {"Authorization": f"Bearer {token}"}
    return options


def _required_string(
    data: dict[str, Any], key: str, *, context: str, alias: str | None = None
) -> str:
    value = data.get(key)
    if value is None and alias is not None:
        value = data.get(alias)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} {key} must be a non-empty string")
    return value


def _optional_string(
    data: dict[str, Any], key: str, *, context: str, alias: str | None = None
) -> str | None:
    value = data.get(key)
    if value is None and alias is not None:
        value = data.get(alias)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} {key} must be a non-empty string")
    return value
