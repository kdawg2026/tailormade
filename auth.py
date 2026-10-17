"""Email + password accounts with server-side sessions.

Passwords are hashed with scrypt (standard library). Session cookies carry a
random token; only its SHA-256 is stored, so a leaked database cannot be
replayed as live logins. Repeated wrong passwords lock the account briefly.

Each network may create one account, ever, and VPN, proxy, Tor, and hosting
networks may create none, so the free tier can't be farmed with new accounts.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request

from db import connect

SESSION_COOKIE = "tm_session"
SESSION_DAYS = 30
MIN_PASSWORD = 10
MAX_PASSWORD = 256  # scrypt cost grows with input; refuse absurd lengths
LOCK_AFTER_FAILURES = 5
LOCK_MINUTES = 15

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Compared against when the email is unknown, so a login takes the same time
# whether or not the account exists.
_DUMMY_HASH = None


class AuthError(Exception):
    """A message safe to show the user."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    _, n, r, p, salt, digest = stored.split("$")
    candidate = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p)
    )
    return hmac.compare_digest(candidate, bytes.fromhex(digest))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise AuthError("Enter a valid email address.")
    return email


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Use at least {MIN_PASSWORD} characters for your password.")
    if len(password) > MAX_PASSWORD:
        raise AuthError(f"Use at most {MAX_PASSWORD} characters for your password.")


PROXYCHECK_URL = "https://proxycheck.io/v3/"
PROXYCHECK_TIMEOUT_SECONDS = 5
# Any one of these blocks a signup. "anonymous" alone is not used: proxycheck
# sets it for some ordinary business networks.
BLOCKING_DETECTIONS = ("vpn", "proxy", "tor", "hosting", "compromised")
NETWORK_UNVERIFIED = "We couldn't verify your network right now. Try again in a few minutes."


def network_key(ip: str) -> str:
    """Keyed hash of the client's network: the IPv4 address, or the IPv6 /64
    (one household's whole range, which a client can rotate through freely).

    HMAC with a server secret, because a plain hash of the 4 billion IPv4
    addresses can be reversed by brute force.
    """
    address = ipaddress.ip_address(ip)
    if address.version == 6:
        network = str(ipaddress.ip_network(f"{address}/64", strict=False))
    else:
        network = str(address)
    secret = os.environ.get("IP_HASH_SECRET", "")
    if not secret and os.environ.get("VERCEL"):
        raise RuntimeError("IP_HASH_SECRET is not set")
    return hmac.new(secret.encode(), network.encode(), hashlib.sha256).hexdigest()


def check_network(ip: str) -> None:
    """Refuse signups from VPN, proxy, Tor, and hosting networks.

    Fails closed: if proxycheck can't answer (outage, quota), signup is refused.
    Loopback and private addresses are skipped only off Vercel, for local dev;
    on Vercel the client address comes from Vercel's own header and is public.
    """
    address = ipaddress.ip_address(ip)
    if not os.environ.get("VERCEL") and (address.is_loopback or address.is_private):
        return
    query = {"key": os.environ["PROXYCHECK_API_KEY"]} if os.environ.get("PROXYCHECK_API_KEY") else {}
    url = PROXYCHECK_URL + urllib.parse.quote(str(address))
    if query:
        url += "?" + urllib.parse.urlencode(query)
    try:
        with urllib.request.urlopen(url, timeout=PROXYCHECK_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise AuthError(NETWORK_UNVERIFIED)
    detections = (payload.get(str(address)) or {}).get("detections")
    if payload.get("status") not in ("ok", "warning") or not isinstance(detections, dict):
        raise AuthError(NETWORK_UNVERIFIED)
    if any(detections.get(flag) for flag in BLOCKING_DETECTIONS):
        raise AuthError(
            "New accounts can't be created over a VPN, proxy, Tor, or hosting network. "
            "Turn it off and try again."
        )


def _new_session(conn, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) "
        f"VALUES (%s, %s, now() + interval '{SESSION_DAYS} days')",
        (_token_hash(token), user_id),
    )
    return token


def signup(email: str, password: str, ip: str) -> str:
    """Create an account and return a session token."""
    email = normalize_email(email)
    check_password_rules(password)
    net = network_key(ip)
    with connect() as conn:
        taken = conn.execute(
            "SELECT 1 FROM signup_networks WHERE net_hash = %s", (net,)
        ).fetchone()
        if taken:
            raise AuthError(
                "An account has already been created from this network. Log in to that account instead."
            )
        check_network(ip)
        row = conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (%s, %s) "
            "ON CONFLICT (email) DO NOTHING RETURNING id",
            (email, hash_password(password)),
        ).fetchone()
        if row is None:
            raise AuthError("An account with that email already exists. Log in instead.")
        # The unique key settles two simultaneous signups from one network:
        # the second insert finds the row and its whole transaction rolls back.
        claimed = conn.execute(
            "INSERT INTO signup_networks (net_hash) VALUES (%s) ON CONFLICT DO NOTHING",
            (net,),
        ).rowcount
        if not claimed:
            raise AuthError(
                "An account has already been created from this network. Log in to that account instead."
            )
        return _new_session(conn, row["id"])


def _check_password(conn, user: dict, password: str, wrong: str) -> None:
    """Raise AuthError unless password matches; wrong guesses count toward a lock.

    user must be a row locked FOR UPDATE with id, password_hash, and locked.
    """
    if user["locked"]:
        raise AuthError(f"Too many wrong passwords. Try again in {LOCK_MINUTES} minutes.")
    if len(password) > MAX_PASSWORD or not verify_password(password, user["password_hash"]):
        conn.execute(
            # The count restarts once a lock is applied.
            f"UPDATE users SET locked_until = CASE WHEN failed_logins + 1 >= {LOCK_AFTER_FAILURES} "
            f"THEN now() + interval '{LOCK_MINUTES} minutes' END, "
            f"failed_logins = CASE WHEN failed_logins + 1 >= {LOCK_AFTER_FAILURES} "
            "THEN 0 ELSE failed_logins + 1 END WHERE id = %s",
            (user["id"],),
        )
        conn.commit()  # keep the failure count even though we raise
        raise AuthError(wrong)
    conn.execute(
        "UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = %s", (user["id"],)
    )


_LOCKED_USER = (
    "SELECT id, password_hash, locked_until > now() AS locked FROM users WHERE {} = %s FOR UPDATE"
)


def login(email: str, password: str) -> str:
    global _DUMMY_HASH
    try:
        email = normalize_email(email)
    except AuthError:
        raise AuthError("Wrong email or password.")
    with connect() as conn:
        user = conn.execute(_LOCKED_USER.format("email"), (email,)).fetchone()
        if user is None:
            _DUMMY_HASH = _DUMMY_HASH or hash_password(secrets.token_hex(8))
            verify_password(password[:MAX_PASSWORD], _DUMMY_HASH)
            raise AuthError("Wrong email or password.")
        _check_password(conn, user, password, "Wrong email or password.")
        return _new_session(conn, user["id"])


def confirm_password(user_id: int, password: str) -> None:
    """Re-check the signed-in user's password before a sensitive action."""
    with connect() as conn:
        user = conn.execute(_LOCKED_USER.format("id"), (user_id,)).fetchone()
        _check_password(conn, user, password, "That password is not correct.")


def change_password(user_id: int, current: str, new: str, keep_token: str) -> None:
    """Set a new password and sign out every other session."""
    check_password_rules(new)
    with connect() as conn:
        user = conn.execute(_LOCKED_USER.format("id"), (user_id,)).fetchone()
        _check_password(conn, user, current, "Your current password is not correct.")
        conn.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(new), user_id)
        )
        conn.execute(
            "DELETE FROM sessions WHERE user_id = %s AND token_hash <> %s",
            (user_id, _token_hash(keep_token)),
        )


def delete_user(user_id: int) -> None:
    """Remove the account; sessions, usage, and purchases cascade with it."""
    with connect() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))


def logout(token: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_token_hash(token),))


def user_for_session(token: str | None) -> dict | None:
    if not token:
        return None
    with connect() as conn:
        return conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = %s AND s.expires_at > now()",
            (_token_hash(token),),
        ).fetchone()
