"""OAuth 2.0 bearer-token authentication and scope-based authorization.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT

This is the standard OAuth 2.0 *resource server* pattern: every protected call
carries `Authorization: Bearer <JWT>`, and the API validates the token's
signature, expiry, issuer and scopes before doing anything. That is the same
contract a real identity provider (Auth0, Okta, Entra, Cognito) issues against,
which is the point -- swapping this for a hosted IdP means verifying the token
with the provider's JWKS instead of a local secret, and changing nothing else.
`SCOPES`, the dependencies, and every endpoint stay exactly as they are.

Tokens are minted here through the OAuth 2.0 password grant (RFC 6749 §4.3),
because this case study has to run on one laptop with no external account to
register. That is the one part a production deployment would replace. It is a
standard grant with a standard token shape -- not a bespoke auth protocol.

WHAT IS NOT TRUSTED

The client. Not the Origin header, not CORS, not a hidden form field, and above
all not a `reviewer` name in a JSON body. Identity comes from the signed token
and nowhere else, so a curl request that knows the endpoint still gets a 401.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from typing import List, Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, SecurityScopes

import config

# --------------------------------------------------------------------------
# scopes
#
# Named after the action they permit rather than after a job title, so an
# endpoint reads as "what must you be allowed to do" instead of "who are you".
# Roles are a bundle of these; the endpoints never look at roles.
# --------------------------------------------------------------------------
SCOPES = {
    "invoice:read": "Read runs, audit trails and procurement reference data",
    "invoice:process": "Upload and process invoices (consumes extraction quota)",
    "invoice:review": "Accept or reject invoices held for human review",
    "invoice:admin": "Override the status of any run, including reversals",
}

ROLE_SCOPES = {
    "viewer": ["invoice:read"],
    "analyst": ["invoice:read", "invoice:process"],
    # Reviewing is a separate permission from processing on purpose: approving
    # payment is a different authority from feeding a PDF to an extractor, and
    # an analyst having the first does not imply the second.
    "reviewer": ["invoice:read", "invoice:process", "invoice:review"],
    "admin": ["invoice:read", "invoice:process", "invoice:review", "invoice:admin"],
}


def scopes_for_roles(roles) -> List[str]:
    out = []
    for role in roles or []:
        for s in ROLE_SCOPES.get(role, []):
            if s not in out:
                out.append(s)
    return out


# --------------------------------------------------------------------------
# password hashing
#
# PBKDF2-HMAC-SHA256 from the standard library. Not the strongest KDF available,
# but it is the strongest one that needs no extra dependency, and it is the
# difference between "hashed" and "not hashed", which is the difference that
# actually matters here.
# --------------------------------------------------------------------------
_PBKDF2_ROUNDS = 390_000


def hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return "pbkdf2_sha256$%d$%s$%s" % (
        _PBKDF2_ROUNDS,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time verification. Returns False on anything malformed rather
    than raising -- a corrupt hash must read as "wrong password", not as a 500
    that tells an attacker the account exists."""
    try:
        algo, rounds, salt_b64, hash_b64 = (encoded or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode(),
                                 base64.b64decode(salt_b64), int(rounds))
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:
        return False


# --------------------------------------------------------------------------
# user store
# --------------------------------------------------------------------------

def _users_path() -> str:
    return os.environ.get("AUTH_USERS_FILE") or config.USERS_SEED


def load_users() -> dict:
    """{username: {username, roles, password_hash}}. Read on each call so the
    file can be swapped in a test without reimporting the module."""
    path = _users_path()
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except Exception:
        return {}
    return {r["username"]: r for r in rows if r.get("username")}


def authenticate_user(username: str, password: str) -> Optional[dict]:
    """The user record, or None. Deliberately makes no distinction between
    "no such user" and "wrong password" to the caller."""
    user = load_users().get((username or "").strip())
    if user is None:
        # Still run a hash so a missing user and a wrong password take
        # comparable time; a fast 'no' enumerates valid usernames.
        verify_password(password, hash_password("timing-equaliser"))
        return None
    if not verify_password(password, user.get("password_hash", "")):
        return None
    return user


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------
_RUNTIME_SECRET = None


def signing_secret() -> str:
    """The HMAC secret for access tokens.

    Taken from AUTH_SECRET. When that is unset a random one is generated for the
    life of the process: there is deliberately NO hardcoded fallback, because a
    default secret shipped in a repository is not a secret, and a deployment
    that forgot to set one would be silently signing forgeable tokens. The cost
    is that tokens do not survive a restart, which is the correct trade.
    """
    global _RUNTIME_SECRET
    env = os.environ.get(config.AUTH_SECRET_ENV, "").strip()
    if env:
        return env
    if _RUNTIME_SECRET is None:
        _RUNTIME_SECRET = secrets.token_urlsafe(48)
        print(f"[auth] {config.AUTH_SECRET_ENV} is not set — generated an ephemeral "
              f"signing key. Tokens will stop working when this process restarts.",
              file=sys.stderr)
    return _RUNTIME_SECRET


def create_access_token(user: dict) -> dict:
    now = int(time.time())
    ttl = config.AUTH_TOKEN_TTL_MINUTES * 60
    scopes = scopes_for_roles(user.get("roles"))
    payload = {
        "sub": user["username"],
        "roles": list(user.get("roles") or []),
        "scope": " ".join(scopes),      # RFC 8693 / RFC 9068 spelling
        "iss": config.AUTH_ISSUER,
        "iat": now,
        "exp": now + ttl,
    }
    token = jwt.encode(payload, signing_secret(), algorithm="HS256")
    return {"access_token": token, "token_type": "bearer", "expires_in": ttl,
            "scope": payload["scope"]}


def decode_token(token: str) -> dict:
    """Validated claims, or raise 401. Signature, expiry AND issuer are all
    checked -- verifying the signature alone would accept an expired token, and
    accepting any issuer would accept a token minted for a different system."""
    try:
        return jwt.decode(token, signing_secret(), algorithms=["HS256"],
                          issuer=config.AUTH_ISSUER)
    except jwt.PyJWTError:
        # One message for every failure mode. Telling a caller whether a token
        # was expired, forged or malformed is free reconnaissance.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


# --------------------------------------------------------------------------
# FastAPI dependencies
# --------------------------------------------------------------------------
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token", scopes=SCOPES,
                                     auto_error=False)


class Principal:
    """The authenticated caller. The ONLY source of identity in this API."""

    def __init__(self, claims: dict):
        self.claims = claims
        self.username = claims.get("sub")
        self.roles = list(claims.get("roles") or [])
        self.scopes = (claims.get("scope") or "").split()

    def has(self, scope: str) -> bool:
        return scope in self.scopes

    def __repr__(self):
        return f"<Principal {self.username} scopes={self.scopes}>"


def current_principal(security_scopes: SecurityScopes,
                      token: Optional[str] = Depends(oauth2_scheme)) -> Principal:
    """Authenticate, then authorize against the scopes the endpoint declared.

    The two failures are kept distinct because they mean different things to a
    caller and to an auditor: 401 is "we do not know who you are", 403 is "we
    know exactly who you are and you may not do this".
    """
    header = f'Bearer scope="{security_scopes.scope_str}"' if security_scopes.scopes else "Bearer"

    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Not authenticated",
                            headers={"WWW-Authenticate": header})

    principal = Principal(decode_token(token))

    for scope in security_scopes.scopes:
        if not principal.has(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires the '{scope}' permission",
                headers={"WWW-Authenticate": header},
            )
    return principal
