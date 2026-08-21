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


def _dummy_hash() -> str:
    """One precomputed hash, used to equalise the timing of a failed login.

    Phase K: `authenticate_user` used to build this fresh on every miss, which
    ran the 390,000-round KDF twice for an unknown username (once to make the
    hash, once to check it) against one pass for a real one. That made an
    unknown-user flood the most expensive request in the application and left
    the timing it was meant to equalise measurably UNequal in the other
    direction. Computing it once, lazily, at first use gives one KDF pass on
    either path -- which is both cheaper and closer to constant.
    """
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("timing-equaliser")
    return _DUMMY_HASH


_DUMMY_HASH = None


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


def demo_usernames() -> List[str]:
    """Accounts explicitly flagged as demo credentials in the user store.

    The flag lives on the RECORD, not on the file path, so copying
    data/users.json somewhere else and pointing AUTH_USERS_FILE at it does not
    launder it into a production-safe account. A real account is one that does
    not carry the flag.
    """
    return sorted(name for name, u in load_users().items() if u.get("demo"))


def validate_production_config() -> List[str]:
    """Configuration problems that must stop a production start. Empty in dev.

    Returns the problems rather than raising so a caller can report all of them
    at once -- being told about the missing secret, restarting, and only then
    being told about the demo accounts is a poor way to learn this.
    """
    if not config.is_production():
        return []

    problems = []
    if not os.environ.get(config.AUTH_SECRET_ENV, "").strip():
        problems.append(
            f"{config.AUTH_SECRET_ENV} is not set. Generate one with: "
            f"python -c \"import secrets;print(secrets.token_urlsafe(48))\"")

    demo = demo_usernames()
    if demo:
        problems.append(
            f"the user store contains demo credentials ({', '.join(demo)}). "
            f"Their passwords are published in this repository and on the sign-in "
            f"screen. Point AUTH_USERS_FILE at a real user store, or replace this "
            f"token issuer with your identity provider.")

    if not load_users():
        problems.append(
            "the user store is empty or unreadable, so nobody could sign in. "
            f"Check AUTH_USERS_FILE or {config.USERS_SEED}.")

    if "*" in config.CORS_ORIGINS:
        problems.append("CORS_ORIGINS contains '*'. Name the origins explicitly.")

    return problems


def enforce_production_config():
    """Refuse to start a production process with an unsafe configuration.

    Deliberately fatal rather than a warning. Every problem this catches is one
    where the app would keep working perfectly and be quietly insecure -- which
    is exactly the class of mistake that survives to production.
    """
    problems = validate_production_config()
    if not problems:
        return
    header = f"Refusing to start with {config.APP_ENV_VAR}='{config.app_env()}':"
    raise RuntimeError("\n".join([header] + [f"  - {p}" for p in problems]))


def is_disabled(user: dict) -> bool:
    """Whether a user record has been deactivated (Phase K).

    Two spellings are honoured because both are the obvious one to reach for,
    and an operator who deactivates an account must not discover that they
    picked the word this code does not read:

        {"disabled": true}      -- the flag this codebase writes
        {"active": false}       -- the flag most user stores already carry

    Anything unparseable reads as DISABLED. Every other default in this file
    fails open for availability (the quota breaker, document persistence); this
    one fails closed, because the question it answers is "should this person
    still be allowed in", and a corrupt record is not a yes.
    """
    if not isinstance(user, dict):
        return True
    if user.get("disabled"):
        return True
    if "active" in user and not user.get("active"):
        return True
    return False


def authenticate_user(username: str, password: str) -> Optional[dict]:
    """The user record, or None. Deliberately makes no distinction between
    "no such user", "wrong password" and "account disabled" to the caller.

    The third one is new in Phase K and is why the password is still checked
    for a disabled account before returning None: answering a disabled account
    faster, or differently, would turn this endpoint into a way to ask which
    of your colleagues has been deactivated.
    """
    user = load_users().get((username or "").strip())
    if user is None:
        # Still run a hash so a missing user and a wrong password take
        # comparable time; a fast 'no' enumerates valid usernames.
        verify_password(password, _dummy_hash())
        return None
    password_ok = verify_password(password, user.get("password_hash", ""))
    if not password_ok:
        return None
    if is_disabled(user):
        return None
    return user


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------
_RUNTIME_SECRET = None


def signing_secret() -> str:
    """The HMAC secret for access tokens.

    Taken from AUTH_SECRET. There is deliberately NO hardcoded fallback, because
    a default secret shipped in a repository is not a secret and a deployment
    that forgot to set one would be silently signing forgeable tokens.

    In DEVELOPMENT an ephemeral per-process key is generated instead, so the
    case study runs with no setup; the cost is that tokens die on restart, which
    is the right trade for a laptop.

    In PRODUCTION that fallback does not exist. An ephemeral key is not merely
    inconvenient there -- it silently invalidates every session on each restart
    and differs between workers, so tokens minted by one are rejected by
    another. Startup refuses first (`validate_production_config`), and this is
    the second gate in case the process was started some other way.
    """
    global _RUNTIME_SECRET
    env = os.environ.get(config.AUTH_SECRET_ENV, "").strip()
    if env:
        return env
    if config.is_production():
        raise RuntimeError(
            f"{config.AUTH_SECRET_ENV} must be set when {config.APP_ENV_VAR} is "
            f"'{config.app_env()}'. Refusing to sign tokens with an ephemeral key.")
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


def apply_account_state(principal: Principal, header: str = "Bearer") -> Principal:
    """Re-check a decoded token against the LIVE user store (Phase K).

    THE PROBLEM THIS SOLVES. A JWT is a snapshot: it carries the roles and
    scopes the account held at the moment it was minted, and it is then
    believed, unexamined, until it expires -- `AUTH_TOKEN_TTL_MINUTES`, eight
    hours by default. So before this existed, an account that was deactivated
    or demoted kept every permission it had at sign-in for the rest of that
    window, and there was no way to cut it short except rotating AUTH_SECRET,
    which signs everybody out. An offboarded AP clerk could keep approving
    invoices for the rest of the working day.

    THE CHECK, and it is deliberately two separate things:

      * A DISABLED account is refused outright -- 401, the same wording every
        other token failure gets.
      * A live account's scopes are INTERSECTED with what its CURRENT roles
        grant, so a demotion (reviewer -> viewer) takes effect on the very next
        request. A token can therefore never carry more authority than the
        account behind it holds right now; it can only carry less.

    `load_users()` already reads the store on every call, so this costs one
    small file read per request and no new state, no denylist, no session
    table -- which is what keeps it inside the existing architecture rather
    than being a second authentication system.

    THE RESIDUAL GAP, STATED PLAINLY: a username with NO record in the store
    is passed through unchanged rather than refused. That is not an oversight
    and it is not free. This module is built so the token issuer can be
    replaced by a real identity provider without touching anything else (see
    the file docstring); an IdP-minted principal legitimately has no local
    record, so treating "absent" as "revoked" would break the one migration
    path this design exists to keep open. The operational consequence is the
    documented instruction: to revoke access, DISABLE the record, do not
    delete it. Deleting still leaves the outstanding token valid until it
    expires.
    """
    user = load_users().get(principal.username or "")
    if user is None:
        return principal

    if is_disabled(user):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired credentials",
            headers={"WWW-Authenticate": header},
        )

    granted = set(scopes_for_roles(user.get("roles")))
    principal.scopes = [s for s in principal.scopes if s in granted]
    return principal


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

    # Decode, THEN re-check against the live account. A valid signature proves
    # the token was minted here; it does not prove the account still exists in
    # the state it was minted for.
    principal = apply_account_state(Principal(decode_token(token)), header)

    for scope in security_scopes.scopes:
        if not principal.has(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires the '{scope}' permission",
                headers={"WWW-Authenticate": header},
            )
    return principal
