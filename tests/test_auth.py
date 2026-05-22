# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWK
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from starlette.testclient import TestClient

from relcoord.app import create_app
from relcoord.auth import (
    SIGNING_KEY_CACHE_TTL_SECONDS,
    AuthError,
    RoleConfig,
    TokenValidator,
    _SigningKeyCache,
    extract_bearer_token,
)
from relcoord.in_memory_store import InMemoryImageInfoStore


@pytest.fixture(scope="module")
def rsa_key_pair() -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(scope="module")
def private_pem(rsa_key_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]) -> str:
    private_key, _ = rsa_key_pair
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture(scope="module")
def signing_key(
    rsa_key_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
) -> PyJWK:
    _, public_key = rsa_key_pair
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(public_key))
    return PyJWK(jwk_dict, algorithm="RS256")


def _make_token(
    private_pem: str,
    *,
    algorithm: str = "RS256",
    issuer: str = "https://issuer.example.com",
    audience: str = "relcoord",
    subject: str = "system:serviceaccount:default:default",
    extra: dict[str, object] | None = None,
    expires_in: int = 300,
    key_id: str | None = None,
) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + expires_in,
        "sub": subject,
    }
    if extra:
        payload.update(extra)
    headers = {"kid": key_id} if key_id is not None else None
    return jwt.encode(payload, private_pem, algorithm=algorithm, headers=headers)


def _role(
    *,
    name: str = "default",
    audience: str = "relcoord",
    issuer: str = "https://issuer.example.com",
    claims: dict[str, str] | None = None,
) -> RoleConfig:
    return RoleConfig(
        name=name,
        audience=audience,
        issuer=issuer,
        jwks_uri="https://issuer.example.com/.well-known/jwks.json",
        claims=claims or {"sub": "system:serviceaccount:default:default"},
    )


def _make_validator(roles: list[RoleConfig], signing_key: PyJWK) -> TokenValidator:
    """Build a TokenValidator with JWKS fetching mocked out."""
    with patch("relcoord.auth.PyJWKClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.get_signing_key_from_jwt.return_value = signing_key
        mock_cls.return_value = mock_client
        return TokenValidator(roles)


def test_extract_bearer_token_rejects_missing_header() -> None:
    with pytest.raises(AuthError):
        extract_bearer_token(None)


def test_extract_bearer_token_requires_bearer_scheme() -> None:
    with pytest.raises(AuthError):
        extract_bearer_token("Basic abc")


def test_extract_bearer_token_rejects_empty() -> None:
    with pytest.raises(AuthError):
        extract_bearer_token("Bearer    ")


def test_validator_accepts_token(private_pem: str, signing_key: PyJWK) -> None:
    validator = _make_validator([_role()], signing_key)
    token = _make_token(private_pem)

    claims = validator.validate(token)

    assert claims.role == "default"
    assert claims.subject == "system:serviceaccount:default:default"


def test_validator_ignores_configured_algorithms() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    signing_key = PyJWK(json.loads(ECAlgorithm.to_jwk(private_key.public_key())))
    role = RoleConfig.from_mapping(
        {
            "name": "default",
            "audience": "relcoord",
            "issuer": "https://issuer.example.com",
            "jwks-uri": "https://issuer.example.com/.well-known/jwks.json",
            "algorithms": ["RS256"],
            "claims": {"sub": "system:serviceaccount:default:default"},
        }
    )
    validator = _make_validator([role], signing_key)

    claims = validator.validate(_make_token(private_pem, algorithm="ES256"))

    assert claims.role == "default"


def test_validator_discovers_jwks_uri_from_issuer(signing_key: PyJWK) -> None:
    role = RoleConfig(
        name="default",
        audience="relcoord",
        issuer="https://issuer.example.com/",
    )
    with (
        patch("relcoord.auth.httpx.get") as mock_get,
        patch("relcoord.auth.PyJWKClient") as mock_jwks_client,
    ):
        mock_get.return_value = httpx.Response(
            200,
            json={"jwks_uri": "https://issuer.example.com/keys"},
            request=httpx.Request(
                "GET",
                "https://issuer.example.com/.well-known/openid-configuration",
            ),
        )
        mock_client = MagicMock()
        mock_client.get_signing_key_from_jwt.return_value = signing_key
        mock_jwks_client.return_value = mock_client

        TokenValidator([role])

    mock_get.assert_called_once_with(
        "https://issuer.example.com/.well-known/openid-configuration",
        timeout=10.0,
    )
    mock_jwks_client.assert_called_once_with(
        "https://issuer.example.com/keys", cache_keys=False
    )


def test_validator_builds_one_jwks_client_per_issuer(signing_key: PyJWK) -> None:
    with patch("relcoord.auth.PyJWKClient") as mock_jwks_client:
        mock_client = MagicMock()
        mock_client.get_signing_key_from_jwt.return_value = signing_key
        mock_jwks_client.return_value = mock_client

        TokenValidator(
            [
                _role(name="a", claims={"sub": "no-match"}),
                _role(
                    name="b",
                    claims={"sub": "system:serviceaccount:default:default"},
                ),
            ]
        )

    mock_jwks_client.assert_called_once_with(
        "https://issuer.example.com/.well-known/jwks.json", cache_keys=False
    )


def test_validator_uses_only_client_for_token_issuer(
    private_pem: str, signing_key: PyJWK
) -> None:
    issuer_a_client = MagicMock()
    issuer_b_client = MagicMock()
    issuer_b_client.get_signing_key_from_jwt.return_value = signing_key
    token = _make_token(private_pem, issuer="https://issuer-b.example.com")

    with patch("relcoord.auth.PyJWKClient") as mock_jwks_client:
        mock_jwks_client.side_effect = [issuer_a_client, issuer_b_client]
        validator = TokenValidator(
            [
                RoleConfig(
                    name="a",
                    audience="relcoord",
                    issuer="https://issuer-a.example.com",
                    jwks_uri="https://issuer-a.example.com/keys",
                    claims={"sub": "system:serviceaccount:default:default"},
                ),
                RoleConfig(
                    name="b",
                    audience="relcoord",
                    issuer="https://issuer-b.example.com",
                    jwks_uri="https://issuer-b.example.com/keys",
                    claims={"sub": "system:serviceaccount:default:default"},
                ),
            ]
        )

    claims = validator.validate(token)

    assert claims.role == "b"
    issuer_a_client.get_signing_key_from_jwt.assert_not_called()
    issuer_b_client.get_signing_key_from_jwt.assert_called_once_with(token)


def test_validator_reuses_cached_signing_key_for_token_key_id(
    private_pem: str, signing_key: PyJWK
) -> None:
    token = _make_token(private_pem, key_id="key-1")
    with patch("relcoord.auth.PyJWKClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.get_signing_key_from_jwt.return_value = signing_key
        mock_cls.return_value = mock_client
        validator = TokenValidator([_role()])

    validator.validate(token)
    validator.validate(token)

    mock_client.get_signing_key_from_jwt.assert_called_once_with(token)


def test_signing_key_cache_expires_after_ttl(signing_key: PyJWK) -> None:
    cache = _SigningKeyCache(max_size=2, ttl_seconds=SIGNING_KEY_CACHE_TTL_SECONDS)

    with patch("relcoord.auth.time.monotonic", return_value=10.0):
        cache.set(("issuer", "key-1", "RS256"), signing_key)

    with patch("relcoord.auth.time.monotonic", return_value=3609.0):
        assert cache.get(("issuer", "key-1", "RS256")) is signing_key

    with patch("relcoord.auth.time.monotonic", return_value=3611.0):
        assert cache.get(("issuer", "key-1", "RS256")) is None


def test_signing_key_cache_evicts_least_recently_used(signing_key: PyJWK) -> None:
    cache = _SigningKeyCache(max_size=2)
    first = ("issuer", "key-1", "RS256")
    second = ("issuer", "key-2", "RS256")
    third = ("issuer", "key-3", "RS256")

    cache.set(first, signing_key)
    cache.set(second, signing_key)
    assert cache.get(first) is signing_key

    cache.set(third, signing_key)

    assert cache.get(first) is signing_key
    assert cache.get(second) is None
    assert cache.get(third) is signing_key


def test_validator_uses_kubernetes_service_account_for_issuer_discovery(
    tmp_path: Path, signing_key: PyJWK
) -> None:
    ca_cert = tmp_path / "ca.crt"
    token = tmp_path / "token"
    ca_cert.write_text("ca certificate")
    token.write_text("local-kubernetes-token\n")
    role = RoleConfig(
        name="default",
        audience="relcoord",
        issuer="https://kubernetes.default.svc",
    )

    with (
        patch("relcoord.auth.KUBERNETES_CA_CERT_PATH", ca_cert),
        patch("relcoord.auth.KUBERNETES_TOKEN_PATH", token),
        patch("relcoord.auth.httpx.get") as mock_get,
        patch("relcoord.auth.PyJWKClient") as mock_jwks_client,
    ):
        mock_get.return_value = httpx.Response(
            200,
            json={"jwks_uri": "https://kubernetes.default.svc/openid/v1/jwks"},
            request=httpx.Request(
                "GET",
                "https://kubernetes.default.svc/.well-known/openid-configuration",
            ),
        )
        mock_client = MagicMock()
        mock_client.get_signing_key_from_jwt.return_value = signing_key
        mock_jwks_client.return_value = mock_client

        TokenValidator([role])

    mock_get.assert_called_once_with(
        "https://kubernetes.default.svc/.well-known/openid-configuration",
        timeout=10.0,
        verify=ca_cert,
        headers={"Authorization": "Bearer local-kubernetes-token"},
    )


def test_validator_rejects_wrong_audience(private_pem: str, signing_key: PyJWK) -> None:
    validator = _make_validator([_role()], signing_key)
    token = _make_token(private_pem, audience="other")

    with pytest.raises(AuthError):
        validator.validate(token)


def test_validator_logs_wrong_audience(
    private_pem: str, signing_key: PyJWK, caplog: pytest.LogCaptureFixture
) -> None:
    validator = _make_validator([_role()], signing_key)
    token = _make_token(private_pem, audience="other")

    with caplog.at_level(logging.WARNING, logger="relcoord.auth"):
        with pytest.raises(AuthError):
            validator.validate(token)

    assert "audience validation failed" in caplog.text
    assert "aud='other'" in caplog.text


def test_validator_logs_signature_validation_failure(
    signing_key: PyJWK, caplog: pytest.LogCaptureFixture
) -> None:
    other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_private_pem = other_private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    validator = _make_validator([_role()], signing_key)

    with caplog.at_level(logging.WARNING, logger="relcoord.auth"):
        with pytest.raises(AuthError):
            validator.validate(_make_token(other_private_pem))

    assert "signature validation failed" in caplog.text


def test_validator_rejects_when_required_claim_missing(
    private_pem: str, signing_key: PyJWK
) -> None:
    validator = _make_validator([_role()], signing_key)
    token = _make_token(private_pem, subject="someone-else")

    with pytest.raises(AuthError):
        validator.validate(token)


def test_validator_tries_each_role(private_pem: str, signing_key: PyJWK) -> None:
    role_a = _role(name="a", claims={"sub": "no-match"})
    role_b = _role(name="b", claims={"sub": "system:serviceaccount:default:default"})
    validator = _make_validator([role_a, role_b], signing_key)

    claims = validator.validate(_make_token(private_pem))

    assert claims.role == "b"


def test_write_endpoint_requires_bearer_token(signing_key: PyJWK) -> None:
    validator = _make_validator([_role()], signing_key)
    client = TestClient(create_app(InMemoryImageInfoStore(), validator))

    response = client.post(
        "/v1/image-versions",
        json={"image": "registry.example.com/team/api", "version": "1.2.3"},
    )

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_write_endpoint_accepts_valid_bearer_token(
    private_pem: str, signing_key: PyJWK
) -> None:
    validator = _make_validator([_role()], signing_key)
    client = TestClient(create_app(InMemoryImageInfoStore(), validator))
    token = _make_token(private_pem)

    response = client.post(
        "/v1/image-versions",
        json={"image": "registry.example.com/team/api", "version": "1.2.3"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201


def test_change_endpoint_requires_bearer_token(signing_key: PyJWK) -> None:
    validator = _make_validator([_role()], signing_key)
    client = TestClient(create_app(InMemoryImageInfoStore(), validator))

    response = client.post(
        "/v1/change",
        json={"repo": "acme/api", "commit": "abc123"},
    )

    assert response.status_code == 401


def test_read_endpoints_do_not_require_token(signing_key: PyJWK) -> None:
    validator = _make_validator([_role()], signing_key)
    client = TestClient(create_app(InMemoryImageInfoStore(), validator))

    health = client.get("/healthz")
    latest = client.post(
        "/v1/images/latest",
        json={"images": ["registry.example.com/team/api"]},
    )

    assert health.status_code == 200
    assert latest.status_code == 200
