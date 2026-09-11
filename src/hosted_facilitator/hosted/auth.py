from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.hashes import SHA256

OIDC_ISSUER_ENV = "OMNICLAW_HOSTED_OIDC_ISSUER"
OIDC_AUDIENCE_ENV = "OMNICLAW_HOSTED_OIDC_AUDIENCE"
OIDC_JWKS_URL_ENV = "OMNICLAW_HOSTED_OIDC_JWKS_URL"
OPENFGA_API_URL_ENV = "OMNICLAW_HOSTED_OPENFGA_API_URL"
OPENFGA_STORE_ID_ENV = "OMNICLAW_HOSTED_OPENFGA_STORE_ID"
OPENFGA_STORE_NAME_ENV = "OMNICLAW_HOSTED_OPENFGA_STORE_NAME"
OPENFGA_AUTHORIZATION_MODEL_ID_ENV = "OMNICLAW_HOSTED_OPENFGA_AUTHORIZATION_MODEL_ID"
OPENFGA_ALLOW_DYNAMIC_MODEL_ID_ENV = "OMNICLAW_HOSTED_OPENFGA_ALLOW_DYNAMIC_MODEL_ID"


class OperationsPermission(StrEnum):
    OPS_OVERVIEW = "ops:overview"
    OPS_PAUSE = "ops:pause"
    OPS_SELLERS = "ops:sellers"
    OPS_SETTLEMENTS = "ops:settlements"
    OPS_MANUAL_REVIEW = "ops:manual_review"


@dataclass(frozen=True)
class AuthorizationObject:
    type: str
    id: str

    def fga_object(self) -> str:
        return f"{self.type}:{self.id}"


OPS_CONSOLE_OBJECT = AuthorizationObject(type="ops_console", id="default")


@dataclass(frozen=True)
class Principal:
    subject: str
    issuer: str = ""
    email: str | None = None
    tenant_id: str | None = None
    claims: Mapping[str, Any] = field(default_factory=dict)

    @property
    def actor(self) -> str:
        return self.subject

    def fga_user(self) -> str:
        return f"user:{self.subject}"


class AuthenticationError(Exception):
    pass


class AuthorizationDeniedError(Exception):
    pass


class AuthorizationUnavailableError(Exception):
    pass


class TokenClaimsVerifier(Protocol):
    def verify(self, token: str) -> Mapping[str, Any]: ...


class OperationsAuthorizer(Protocol):
    hosted_safe: bool

    async def authorize(
        self,
        authorization_header: str | None,
        permission: OperationsPermission,
        obj: AuthorizationObject = OPS_CONSOLE_OBJECT,
    ) -> Principal: ...


class RelationshipAuthorizer(Protocol):
    hosted_safe: bool

    async def check(
        self,
        principal: Principal,
        permission: OperationsPermission,
        obj: AuthorizationObject,
    ) -> bool: ...


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    audiences: tuple[str, ...]
    jwks_url: str
    allowed_algorithms: tuple[str, ...] = ("RS256", "ES256")
    accepted_token_types: tuple[str, ...] = ("access", "Bearer", "at+jwt", "JWT")
    clock_skew_seconds: int = 60

    @classmethod
    def from_env(cls) -> OIDCConfig | None:
        issuer = os.getenv(OIDC_ISSUER_ENV, "").strip()
        audience = os.getenv(OIDC_AUDIENCE_ENV, "").strip()
        jwks_url = os.getenv(OIDC_JWKS_URL_ENV, "").strip()
        if not (issuer or audience or jwks_url):
            return None
        if not issuer or not audience or not jwks_url:
            raise AuthorizationUnavailableError(
                f"{OIDC_ISSUER_ENV}, {OIDC_AUDIENCE_ENV}, and {OIDC_JWKS_URL_ENV} are required together"
            )
        audiences = tuple(part.strip() for part in audience.split(",") if part.strip())
        return cls(issuer=issuer, audiences=audiences, jwks_url=jwks_url)

    def validate(self, *, hosted_mode: str) -> None:
        if not self.issuer.startswith("https://") and hosted_mode not in {"local", "test"}:
            raise AuthorizationUnavailableError(f"{OIDC_ISSUER_ENV} must use https in hosted mode")
        if not self.jwks_url.startswith("https://") and hosted_mode not in {"local", "test"}:
            raise AuthorizationUnavailableError(
                f"{OIDC_JWKS_URL_ENV} must use https in hosted mode"
            )
        if not self.audiences:
            raise AuthorizationUnavailableError(
                f"{OIDC_AUDIENCE_ENV} must include at least one audience"
            )


class OIDCAuthenticator:
    def __init__(
        self,
        config: OIDCConfig,
        *,
        verifier: TokenClaimsVerifier | None = None,
        clock: Any = time.time,
    ):
        self._config = config
        self._verifier = verifier or JWKSTokenClaimsVerifier(config)
        self._clock = clock

    def authenticate(self, authorization_header: str | None) -> Principal:
        token = _bearer_token(authorization_header)
        claims = dict(self._verifier.verify(token))
        self._validate_claims(claims)
        subject = _required_str_claim(claims, "sub")
        return Principal(
            subject=subject,
            issuer=str(claims.get("iss") or ""),
            email=_optional_str_claim(claims, "email"),
            tenant_id=_optional_str_claim(claims, "org_id")
            or _optional_str_claim(claims, "tenant_id"),
            claims=claims,
        )

    def _validate_claims(self, claims: Mapping[str, Any]) -> None:
        issuer = _required_str_claim(claims, "iss")
        if issuer != self._config.issuer:
            raise AuthenticationError("OIDC issuer is not allowed")
        audiences = _audiences_from_claim(claims.get("aud"))
        if not audiences or not set(audiences).intersection(self._config.audiences):
            raise AuthenticationError("OIDC audience is not allowed")
        now = int(self._clock())
        exp = _required_int_claim(claims, "exp")
        if exp + self._config.clock_skew_seconds < now:
            raise AuthenticationError("OIDC token is expired")
        nbf = _optional_int_claim(claims, "nbf")
        if nbf is not None and nbf - self._config.clock_skew_seconds > now:
            raise AuthenticationError("OIDC token is not yet valid")
        iat = _optional_int_claim(claims, "iat")
        if iat is not None and iat - self._config.clock_skew_seconds > now:
            raise AuthenticationError("OIDC token was issued in the future")
        token_type = _optional_str_claim(claims, "token_use") or _optional_str_claim(claims, "typ")
        if token_type is None:
            raise AuthenticationError("OIDC token type is required")
        if token_type not in self._config.accepted_token_types:
            raise AuthenticationError("OIDC token type is not allowed")


class OIDCAuthorizationAuthorizer:
    def __init__(
        self,
        authenticator: OIDCAuthenticator,
        relationships: RelationshipAuthorizer,
    ):
        self._authenticator = authenticator
        self._relationships = relationships
        self.hosted_safe = bool(getattr(relationships, "hosted_safe", False))

    async def authorize(
        self,
        authorization_header: str | None,
        permission: OperationsPermission,
        obj: AuthorizationObject = OPS_CONSOLE_OBJECT,
    ) -> Principal:
        principal = self._authenticator.authenticate(authorization_header)
        try:
            allowed = await self._relationships.check(principal, permission, obj)
        except Exception as exc:
            raise AuthorizationUnavailableError("authorization check failed") from exc
        if not allowed:
            raise AuthorizationDeniedError("authorization denied")
        return principal


class UnconfiguredOperationsAuthorizer:
    hosted_safe = False

    async def authorize(
        self,
        authorization_header: str | None,
        permission: OperationsPermission,
        obj: AuthorizationObject = OPS_CONSOLE_OBJECT,
    ) -> Principal:
        del authorization_header, permission, obj
        raise AuthorizationUnavailableError(
            "OIDC/OpenFGA operations authorization is not configured"
        )


class StaticRelationshipAuthorizer:
    hosted_safe = False

    def __init__(
        self,
        allowed: Mapping[str, set[OperationsPermission]] | None = None,
        *,
        default_allowed: bool = False,
    ):
        self._allowed = {key: set(value) for key, value in (allowed or {}).items()}
        self._default_allowed = default_allowed

    async def check(
        self,
        principal: Principal,
        permission: OperationsPermission,
        obj: AuthorizationObject,
    ) -> bool:
        del obj
        if self._default_allowed:
            return True
        return permission in self._allowed.get(principal.subject, set())


class OpenFGAHttpAuthorizer:
    hosted_safe = True

    def __init__(
        self,
        *,
        api_url: str,
        store_id: str,
        authorization_model_id: str,
        api_token: str | None = None,
        timeout: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._api_url = api_url.rstrip("/")
        self._store_id = store_id
        self._authorization_model_id = authorization_model_id
        self._api_token = api_token
        self._timeout = timeout
        self._transport = transport

    @classmethod
    def from_env(cls, *, hosted_mode: str) -> OpenFGAHttpAuthorizer | None:
        api_url = os.getenv(OPENFGA_API_URL_ENV, "").strip()
        store_id = os.getenv(OPENFGA_STORE_ID_ENV, "").strip()
        store_name = os.getenv(OPENFGA_STORE_NAME_ENV, "").strip()
        model_id = os.getenv(OPENFGA_AUTHORIZATION_MODEL_ID_ENV, "").strip()
        allow_dynamic_model = _bool_env(OPENFGA_ALLOW_DYNAMIC_MODEL_ID_ENV)
        api_token = os.getenv("OMNICLAW_HOSTED_OPENFGA_API_TOKEN")
        if not (api_url or store_id or store_name or model_id):
            return None
        if not api_url:
            raise AuthorizationUnavailableError(
                f"{OPENFGA_API_URL_ENV}, {OPENFGA_STORE_ID_ENV}, and "
                f"{OPENFGA_AUTHORIZATION_MODEL_ID_ENV} are required together"
            )
        if not api_url.startswith("https://") and hosted_mode not in {"local", "test"}:
            raise AuthorizationUnavailableError(
                f"{OPENFGA_API_URL_ENV} must use https in hosted mode"
            )
        if not store_id or not model_id:
            if hosted_mode not in {"local", "test"}:
                raise AuthorizationUnavailableError(
                    f"{OPENFGA_STORE_ID_ENV} and {OPENFGA_AUTHORIZATION_MODEL_ID_ENV} are required "
                    "outside local/test mode"
                )
            if not store_name or not allow_dynamic_model:
                raise AuthorizationUnavailableError(
                    f"{OPENFGA_STORE_ID_ENV} and {OPENFGA_AUTHORIZATION_MODEL_ID_ENV} are required, "
                    f"or local/test must set {OPENFGA_STORE_NAME_ENV} and "
                    f"{OPENFGA_ALLOW_DYNAMIC_MODEL_ID_ENV}=true"
                )
            store_id, model_id = _resolve_local_openfga_ids(
                api_url=api_url,
                store_name=store_name,
                api_token=api_token,
            )
        return cls(
            api_url=api_url,
            store_id=store_id,
            authorization_model_id=model_id,
            api_token=api_token,
        )

    async def check(
        self,
        principal: Principal,
        permission: OperationsPermission,
        obj: AuthorizationObject,
    ) -> bool:
        headers = {"Content-Type": "application/json"}
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
        payload = {
            "authorization_model_id": self._authorization_model_id,
            "tuple_key": {
                "user": principal.fga_user(),
                "relation": "",
                "object": obj.fga_object(),
            },
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                for relation in _permission_relations(permission):
                    payload["tuple_key"]["relation"] = relation
                    response = await client.post(
                        f"{self._api_url}/stores/{self._store_id}/check",
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    body = response.json()
                    if bool(body.get("allowed")):
                        return True
        except Exception as exc:
            raise AuthorizationUnavailableError("OpenFGA check unavailable") from exc
        return False


class JWKSTokenClaimsVerifier:
    def __init__(
        self,
        config: OIDCConfig,
        *,
        timeout: float = 2.0,
        cache_ttl_seconds: int = 300,
        clock: Any = time.time,
    ):
        self._config = config
        self._timeout = timeout
        self._cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock
        self._jwks: Mapping[str, Any] | None = None
        self._jwks_expires_at = 0.0

    def verify(self, token: str) -> Mapping[str, Any]:
        header, payload, signing_input, signature = _split_jwt(token)
        algorithm = str(header.get("alg") or "")
        if algorithm not in self._config.allowed_algorithms:
            raise AuthenticationError("OIDC token algorithm is not allowed")
        key_id = str(header.get("kid") or "")
        if not key_id:
            raise AuthenticationError("OIDC token key id is required")
        jwk = self._jwk_for_key_id(key_id)
        _verify_signature(algorithm, jwk, signing_input, signature)
        claims = dict(payload)
        if isinstance(header.get("typ"), str):
            claims.setdefault("typ", header["typ"])
        return claims

    def _jwk_for_key_id(self, key_id: str) -> Mapping[str, Any]:
        jwks = self._load_jwks()
        keys = jwks.get("keys")
        if not isinstance(keys, list):
            raise AuthenticationError("OIDC JWKS is malformed")
        for jwk in keys:
            if isinstance(jwk, dict) and jwk.get("kid") == key_id:
                return jwk
        raise AuthenticationError("OIDC signing key is unknown")

    def _load_jwks(self) -> Mapping[str, Any]:
        now = float(self._clock())
        if self._jwks is not None and now < self._jwks_expires_at:
            return self._jwks
        try:
            response = httpx.get(self._config.jwks_url, timeout=self._timeout)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise AuthorizationUnavailableError("OIDC JWKS unavailable") from exc
        if not isinstance(body, dict):
            raise AuthenticationError("OIDC JWKS is malformed")
        self._jwks = body
        self._jwks_expires_at = now + self._cache_ttl_seconds
        return body


def operations_authorizer_from_env(
    *,
    hosted_mode: str,
) -> OperationsAuthorizer:
    oidc_config = OIDCConfig.from_env()
    if oidc_config is None:
        if hosted_mode in {"local", "test"}:
            return UnconfiguredOperationsAuthorizer()
        raise AuthorizationUnavailableError(
            f"{OIDC_ISSUER_ENV}, {OIDC_AUDIENCE_ENV}, and {OIDC_JWKS_URL_ENV} are required "
            "for hosted operations control-plane auth"
        )
    oidc_config.validate(hosted_mode=hosted_mode)
    relationships = OpenFGAHttpAuthorizer.from_env(hosted_mode=hosted_mode)
    if relationships is None:
        raise AuthorizationUnavailableError(
            "OpenFGA authorization config is required when OIDC is enabled"
        )
    return OIDCAuthorizationAuthorizer(
        authenticator=OIDCAuthenticator(oidc_config),
        relationships=relationships,
    )


def _bearer_token(authorization_header: str | None) -> str:
    value = authorization_header or ""
    prefix = "Bearer "
    if not value.startswith(prefix):
        raise AuthenticationError("Bearer token is required")
    token = value[len(prefix) :].strip()
    if not token:
        raise AuthenticationError("Bearer token is required")
    return token


def _required_str_claim(claims: Mapping[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value:
        raise AuthenticationError(f"OIDC claim {name} is required")
    return value


def _optional_str_claim(claims: Mapping[str, Any], name: str) -> str | None:
    value = claims.get(name)
    return value if isinstance(value, str) and value else None


def _required_int_claim(claims: Mapping[str, Any], name: str) -> int:
    value = claims.get(name)
    if not isinstance(value, int):
        raise AuthenticationError(f"OIDC claim {name} is required")
    return value


def _optional_int_claim(claims: Mapping[str, Any], name: str) -> int | None:
    value = claims.get(name)
    return value if isinstance(value, int) else None


def _audiences_from_claim(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _permission_relations(permission: OperationsPermission) -> tuple[str, ...]:
    if permission == OperationsPermission.OPS_OVERVIEW:
        return ("support_operator", "auditor", "ops_admin", "security_admin")
    if permission == OperationsPermission.OPS_PAUSE:
        return ("ops_admin", "security_admin")
    if permission == OperationsPermission.OPS_SELLERS:
        return ("ops_admin", "security_admin")
    if permission == OperationsPermission.OPS_SETTLEMENTS:
        return ("settlement_operator", "ops_admin", "security_admin")
    if permission == OperationsPermission.OPS_MANUAL_REVIEW:
        return ("manual_reviewer", "security_admin")
    raise AuthorizationDeniedError("unsupported permission")


def _resolve_local_openfga_ids(
    *,
    api_url: str,
    store_name: str,
    api_token: str | None = None,
    timeout: float = 2.0,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            stores_response = client.get(f"{api_url.rstrip('/')}/stores", headers=headers)
            stores_response.raise_for_status()
            stores = stores_response.json().get("stores", [])
            store_id = ""
            for store in stores:
                if isinstance(store, dict) and store.get("name") == store_name:
                    store_id = str(store.get("id") or "")
                    break
            if not store_id:
                raise AuthorizationUnavailableError("OpenFGA local store is not seeded")
            models_response = client.get(
                f"{api_url.rstrip('/')}/stores/{store_id}/authorization-models",
                headers=headers,
            )
            models_response.raise_for_status()
            models = models_response.json().get("authorization_models", [])
            model_id = ""
            if models:
                latest = sorted(
                    (model for model in models if isinstance(model, dict)),
                    key=lambda model: str(model.get("created_at") or ""),
                    reverse=True,
                )[0]
                model_id = str(latest.get("id") or "")
            if not model_id:
                raise AuthorizationUnavailableError(
                    "OpenFGA local authorization model is not seeded"
                )
            return store_id, model_id
    except AuthorizationUnavailableError:
        raise
    except Exception as exc:
        raise AuthorizationUnavailableError("OpenFGA local discovery unavailable") from exc


def _bool_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _split_jwt(token: str) -> tuple[Mapping[str, Any], Mapping[str, Any], bytes, bytes]:
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthenticationError("OIDC token is malformed")
    header = _json_b64url_decode(parts[0])
    payload = _json_b64url_decode(parts[1])
    signature = _b64url_decode(parts[2])
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise AuthenticationError("OIDC token is malformed")
    return header, payload, f"{parts[0]}.{parts[1]}".encode(), signature


def _json_b64url_decode(value: str) -> Any:
    try:
        return json.loads(_b64url_decode(value))
    except Exception as exc:
        raise AuthenticationError("OIDC token is malformed") from exc


def _b64url_decode(value: str) -> bytes:
    padding_length = (-len(value)) % 4
    try:
        return base64.urlsafe_b64decode(value + ("=" * padding_length))
    except Exception as exc:
        raise AuthenticationError("OIDC token is malformed") from exc


def _verify_signature(
    algorithm: str,
    jwk: Mapping[str, Any],
    signing_input: bytes,
    signature: bytes,
) -> None:
    try:
        if algorithm == "RS256":
            key = rsa.RSAPublicNumbers(
                e=_jwk_int(jwk, "e"),
                n=_jwk_int(jwk, "n"),
            ).public_key()
            key.verify(signature, signing_input, padding.PKCS1v15(), SHA256())
            return
        if algorithm == "ES256":
            key = ec.EllipticCurvePublicNumbers(
                x=_jwk_int(jwk, "x"),
                y=_jwk_int(jwk, "y"),
                curve=ec.SECP256R1(),
            ).public_key()
            key.verify(_es256_der_signature(signature), signing_input, ec.ECDSA(SHA256()))
            return
    except InvalidSignature as exc:
        raise AuthenticationError("OIDC token signature is invalid") from exc
    except Exception as exc:
        raise AuthenticationError("OIDC signing key is invalid") from exc
    raise AuthenticationError("OIDC token algorithm is not supported")


def _jwk_int(jwk: Mapping[str, Any], name: str) -> int:
    value = jwk.get(name)
    if not isinstance(value, str) or not value:
        raise AuthenticationError("OIDC signing key is invalid")
    return int.from_bytes(_b64url_decode(value), "big")


def _es256_der_signature(signature: bytes) -> bytes:
    if len(signature) != 64:
        raise AuthenticationError("OIDC token signature is invalid")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    return encode_dss_signature(r, s)


def es256_raw_signature(der_signature: bytes) -> bytes:
    r, s = decode_dss_signature(der_signature)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")
