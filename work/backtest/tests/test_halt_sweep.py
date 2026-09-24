from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import base64
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from backtest.live.audit_ledger import AuditLedger, canonical_json
from backtest.live.halt import HaltController, HaltError, broker_response_is_success, clear_halt_episode, close_request, emergency_flatten_cycle


def test_halt_is_persistent_and_flatten_claim_is_restart_safe(tmp_path) -> None:
    controller = HaltController(tmp_path)
    episode = controller.trigger("fatal")['halt_episode_id']
    assert episode == controller.episode_id()
    calls: list[object] = []
    result = emergency_flatten_cycle(
        controller,
        lambda: {"actions": [{"position": 42}]},
        lambda action: calls.append(action) or {"ok": True, "retcode": 10009},
    )
    assert result["state"] == "FLATTEN_ATTEMPTED"
    assert len(calls) == 1
    again = emergency_flatten_cycle(
        controller,
        lambda: {"actions": [{"position": 42}]},
        lambda action: calls.append(action) or {"ok": True, "retcode": 10009},
    )
    assert again["state"] == "FLATTEN_ATTEMPTED"
    assert len(calls) == 2


def test_success_retcode_is_not_closed_until_exact_ticket_is_absent(tmp_path) -> None:
    controller = HaltController(tmp_path)
    episode = controller.trigger("fatal")["halt_episode_id"]
    result = emergency_flatten_cycle(
        controller,
        lambda: {"actions": [{"kind": "CANCEL", "order": 77}]},
        lambda _: {"ok": True, "retcode": 10009},
    )
    assert result["state"] == "FLATTEN_ATTEMPTED"
    attempt = next((tmp_path / "flatten_attempts").glob(f"{episode}.*.attempt.json"))
    assert json.loads(attempt.read_text(encoding="utf-8"))["state"] == "ACK"


def test_halt_reconciles_acknowledged_ticket_to_closed_after_restart(tmp_path) -> None:
    controller = HaltController(tmp_path)
    episode = controller.trigger("fatal")["halt_episode_id"]
    first = emergency_flatten_cycle(
        controller,
        lambda: {"actions": [{"kind": "CANCEL", "order": 77}]},
        lambda _: {"ok": True, "retcode": 10009},
    )
    assert first["state"] == "FLATTEN_ATTEMPTED"
    # A fresh broker read proves that the acknowledged ticket is now absent.
    closed = emergency_flatten_cycle(controller, lambda: {"actions": []}, lambda _: None)
    assert closed["state"] == "FLATTEN_CLOSED"
    attempt = next((tmp_path / "flatten_attempts").glob(f"{episode}.*.attempt.json"))
    payload = json.loads(attempt.read_text(encoding="utf-8"))
    assert payload["state"] == "CLOSED"
    assert payload["state_history"] == ["DISCOVERED", "CLAIMED", "WRITE_ATTEMPTED", "ACK", "RECONCILED_ABSENT", "CLOSED"]


def test_read_error_releases_only_orphan_claim_and_does_not_write(tmp_path) -> None:
    controller = HaltController(tmp_path)
    controller.trigger("fatal")
    calls: list[object] = []
    result = emergency_flatten_cycle(controller, lambda: {"ok": False, "status": "error"}, lambda x: calls.append(x))
    assert result["state"] == "READ_UNKNOWN_NO_SEND"
    assert calls == []
    assert not controller.side_effect_attempted()


def test_concurrent_flatteners_have_one_claim_and_crashed_claim_is_recovered(tmp_path) -> None:
    controller = HaltController(tmp_path)
    episode = controller.trigger("fatal")["halt_episode_id"]
    calls: list[object] = []
    entered = threading.Event()
    release = threading.Event()

    def broker_write(action: object) -> dict[str, object]:
        calls.append(action)
        entered.set()
        assert release.wait(5)
        return {"ok": True, "retcode": 10009}

    def flatten() -> dict[str, object]:
        return emergency_flatten_cycle(
            controller,
            lambda: {"actions": [{"kind": "CANCEL", "order": 88}]},
            broker_write,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(flatten)
        assert entered.wait(5)
        second = pool.submit(flatten)
        second_result = second.result(timeout=5)
        release.set()
        results = [first.result(timeout=5), second_result]
    assert len(calls) == 1
    assert {result["state"] for result in results} == {"FLATTEN_ATTEMPTED", "FLATTEN_LATCHED"}

    # A process crash can leave only a CLAIMED marker; no broker write occurred,
    # so a later invocation may safely reclaim it.
    controller2 = HaltController(tmp_path / "crash")
    episode2 = controller2.trigger("fatal")["halt_episode_id"]
    claim = controller2.flatten_attempt_root / f"episode.{episode2}.99.claim.json"
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text(json.dumps({"state": "CLAIMED", "owner": "999999:1", "owner_token": "crashed-token"}), encoding="utf-8")
    recovered = emergency_flatten_cycle(
        controller2,
        lambda: {"actions": [{"kind": "CANCEL", "order": 99}]},
        lambda action: {"ok": True, "retcode": 10009},
    )
    assert recovered["state"] == "FLATTEN_ATTEMPTED"


@pytest.mark.parametrize("kind", ["cycle", "ticket"])
def test_two_process_stale_claim_takeover_cannot_delete_new_owner_claim(tmp_path, kind: str) -> None:
    controller = HaltController(tmp_path)
    episode = controller.trigger("fatal")["halt_episode_id"]
    stale_token = "stale-owner-token"
    if kind == "cycle":
        claim = controller.flatten_attempt_root / f"episode.{episode}.cycle.claim.json"
        payload = {"state": "CLAIMED", "owner": "999999999:1", "owner_token": stale_token}
    else:
        claim = controller.flatten_attempt_root / f"episode.{episode}.88.claim.json"
        payload = {
            "schema_version": 2,
            "halt_episode_id": episode,
            "ticket": "88",
            "state": "CLAIMED",
            "state_history": ["DISCOVERED", "CLAIMED"],
            "side_effect_attempted": False,
            "owner": "999999999:1",
            "owner_token": stale_token,
        }
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text(json.dumps(payload), encoding="utf-8")

    probe = Path(__file__).resolve().parents[1] / "scripts" / "halt_claim_race_probe.py"
    sync_root = tmp_path / "sync"
    workers = []
    for index in range(2):
        workers.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(probe),
                    "--root", str(tmp_path),
                    "--episode", episode,
                    "--kind", kind,
                    "--sync-root", str(sync_root),
                    "--index", str(index),
                ],
                    cwd=str(probe.parent.parent),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=0x08000000 if os.name == "nt" else 0,
                )
    )
    new_token: str | None = None
    try:
        deadline = time.monotonic() + 15
        while any(worker.poll() is None for worker in workers) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert all(worker.returncode == 0 for worker in workers), [worker.stderr.read() for worker in workers]
        tokens = [json.loads((sync_root / f"ready-{index}.json").read_text(encoding="utf-8"))["token"] for index in range(2)]
        assert sorted(token is None for token in tokens) == [False, True]
        new_token = next(token for token in tokens if token is not None)
        saved = json.loads(claim.read_text(encoding="utf-8"))
        assert saved["owner_token"] == new_token
        if kind == "cycle":
            controller.release_flatten_cycle(episode, owner_token=stale_token)
        else:
            controller.release_claim(episode, ticket=88, owner_token=stale_token)
        assert claim.exists()
        assert json.loads(claim.read_text(encoding="utf-8"))["owner_token"] == new_token
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                deadline = time.monotonic() + 3
                while worker.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
    if new_token is not None:
        if kind == "cycle":
            controller.release_flatten_cycle(episode, owner_token=new_token)
        else:
            controller.release_claim(episode, ticket=88, owner_token=new_token)
    assert not claim.exists()


def test_close_requires_exact_position_ticket_and_error_envelopes_fail() -> None:
    assert close_request(7, symbol="US100Cash") == {"position": 7, "symbol": "US100Cash"}
    assert not broker_response_is_success({"ok": False, "result": {"retcode": 10009}}, accepted_retcodes={10009})
    assert not broker_response_is_success({"ok": True, "retcode": 10006}, accepted_retcodes={10009})


def test_fake_recovery_booleans_cannot_clear_canonical_halt_episode(tmp_path) -> None:
    controller = HaltController(tmp_path)
    episode = controller.trigger("fatal")["halt_episode_id"]
    ledger = AuditLedger(tmp_path / "orders" / "idempotency.sqlite3")
    with pytest.raises(HaltError, match="signature"):
        clear_halt_episode(
            controller,
            recovery_record={"authorized": True, "signature_valid": True, "halt_episode_id": episode},
            trusted_public_key=b"not-a-key",
            expected_campaign="campaign", expected_account="account", expected_policy_hash="a" * 64,
            read_orders=lambda: (), read_positions=lambda: (), audit_ledger=ledger,
        )
    assert controller.read()["halt_episode_id"] == episode
    ledger.close()


def test_signed_exact_recovery_clears_db_episode_only_after_audit_anchor(tmp_path) -> None:
    controller = HaltController(tmp_path)
    episode = controller.trigger("fatal")["halt_episode_id"]
    anchored: list[tuple[str, str]] = []
    ledger = AuditLedger(
        tmp_path / "orders" / "idempotency.sqlite3",
        anchor=lambda digest, event_id: anchored.append((digest, event_id)),
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    payload = {
        "halt_episode_id": episode, "campaign_id": "campaign", "account_key": "account",
        "nonce": "nonce-1", "expires_at_utc": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "recovery_policy_hash": "a" * 64,
    }
    signature = key.sign(canonical_json(payload).encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    record = {"payload": payload, "algorithm": "RSA-SHA256-PKCS1v15", "signature_b64": base64.b64encode(signature).decode("ascii")}
    public_key = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    result = clear_halt_episode(
        controller, recovery_record=record, trusted_public_key=public_key,
        expected_campaign="campaign", expected_account="account", expected_policy_hash="a" * 64,
        read_orders=lambda: (), read_positions=lambda: (), audit_ledger=ledger,
    )
    assert result["state"] == "CLEARED"
    assert anchored
    assert controller.read() is None
    connection = controller._db()
    assert connection.execute("SELECT state FROM halt_episodes WHERE halt_episode_id=?", (episode,)).fetchone() == ("CLEARED",)
    connection.close()
    ledger.close()
