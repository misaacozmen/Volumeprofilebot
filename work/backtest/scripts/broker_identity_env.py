"""Read and validate the broker account login from the process environment."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping


ACCOUNT_LOGIN_ENV = "XM_MT5_ACCOUNT_LOGIN"
MAX_SIGNED_64 = 2**63 - 1
_ASCII_DECIMAL = re.compile(r"^[0-9]+$")


class BrokerIdentityError(ValueError):
    """Raised when the required broker account identity is unusable."""


def read_account_login(
    environ: Mapping[str, str] | None = None,
    *,
    variable: str = ACCOUNT_LOGIN_ENV,
) -> int:
    """Return a positive signed-64-bit account login from *environ*.

    The value is deliberately stricter than Python's integer parser: only
    ASCII decimal digits are accepted after trimming surrounding whitespace.
    Signs, empty values, zero, Unicode numerals, and overflow are rejected.
    """

    source = os.environ if environ is None else environ
    raw = source.get(variable)
    if raw is None:
        raise BrokerIdentityError(f"{variable} is required before MT5 initialization.")
    value = raw.strip()
    if not value or _ASCII_DECIMAL.fullmatch(value) is None:
        raise BrokerIdentityError(
            f"{variable} must contain only positive ASCII decimal digits."
        )
    try:
        login = int(value, 10)
    except ValueError as exc:  # pragma: no cover - guarded by the regex
        raise BrokerIdentityError(f"{variable} is not a valid account login.") from exc
    if login <= 0 or login > MAX_SIGNED_64:
        raise BrokerIdentityError(
            f"{variable} must be in the positive signed 64-bit range."
        )
    return login
