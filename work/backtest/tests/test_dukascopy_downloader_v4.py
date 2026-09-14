from __future__ import annotations

from argparse import Namespace
import subprocess

import pandas as pd
import pytest

from scripts import download_dukascopy as downloader


def _args(tmp_path) -> Namespace:
    return Namespace(
        from_date="2025-03-07",
        to_date="2025-03-08",
        chunk_days=20,
        provenance_root=tmp_path / "provenance",
        raw_dir=tmp_path / "derived",
        price_type="bid",
        session_context_hours=0,
        request_pause_seconds=0.0,
    )


def test_failed_one_day_chunk_aborts_without_derived_publication(tmp_path, monkeypatch) -> None:
    args = _args(tmp_path)

    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["locked-downloader"])

    monkeypatch.setattr(downloader, "run_dukascopy_cli", fail)
    with pytest.raises(SystemExit, match="derived publication aborted"):
        downloader.download_m1(args, "usatechidxusd")

    assert not args.raw_dir.exists()
    assert list((args.provenance_root / "failed").glob("*.json"))


def test_locked_raw_bytes_produce_identical_3m_and_5m_hashes(tmp_path) -> None:
    raw = tmp_path / "locked.csv"
    rows = []
    start = pd.Timestamp("2025-03-07T14:30:00Z")
    for offset in range(15):
        rows.append(
            {
                "timestamp": int((start + pd.Timedelta(minutes=offset)).timestamp() * 1000),
                "open": 100 + offset,
                "high": 101 + offset,
                "low": 99 + offset,
                "close": 100.5 + offset,
                "volume": 1,
            }
        )
    pd.DataFrame(rows).to_csv(raw, index=False)

    for timeframe in ("3m", "5m"):
        first = downloader.resample_ohlcv(downloader.normalize_download(raw), timeframe)
        second = downloader.resample_ohlcv(downloader.normalize_download(raw), timeframe)
        assert downloader.canonical_frame_hash(first) == downloader.canonical_frame_hash(second)
