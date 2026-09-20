import pytest

from powershell_contract import powershell_harness


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_harness_preserves_powershell_line_continuations(newline):
    script = newline.join([
        "Write-Output `",
        "    -InputObject 'CONTINUATION_PASS'",
        "",
    ])
    result = powershell_harness(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CONTINUATION_PASS"
