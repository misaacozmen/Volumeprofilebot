from __future__ import annotations

import pytest

from broker_identity_env import BrokerIdentityError, read_account_login


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "0", "-1", "+1", "１２３", str(2**63)],
)
def test_invalid_account_login_is_rejected_before_connection(raw: str | None) -> None:
    environment = {} if raw is None else {"XM_MT5_ACCOUNT_LOGIN": raw}
    with pytest.raises(BrokerIdentityError):
        read_account_login(environment)


def test_valid_account_login_is_trimmed_and_transferred() -> None:
    assert read_account_login({"XM_MT5_ACCOUNT_LOGIN": "  9223372036854775807  "}) == 2**63 - 1
