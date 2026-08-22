from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class VolumeProfile:
    vah: float
    val: float
    poc: float
    total_volume: float


def compute_volume_profile(frame: pd.DataFrame, rows: int = 1000, value_area_pct: float = 0.70) -> VolumeProfile | None:
    if frame.empty:
        return None

    low = float(frame["low"].min())
    high = float(frame["high"].max())
    total_volume = float(frame["volume"].sum())
    if high <= low or total_volume <= 0:
        return None

    edges = np.linspace(low, high, rows + 1)
    volumes = np.zeros(rows, dtype=float)

    for candle in frame.itertuples(index=False):
        candle_low = float(candle.low)
        candle_high = float(candle.high)
        candle_volume = float(candle.volume)
        if candle_high <= candle_low or candle_volume <= 0:
            continue
        start = max(0, np.searchsorted(edges, candle_low, side="right") - 1)
        end = min(rows - 1, np.searchsorted(edges, candle_high, side="left"))
        touched = max(1, end - start + 1)
        volumes[start : end + 1] += candle_volume / touched

    if volumes.sum() <= 0:
        return None

    poc_index = int(np.argmax(volumes))
    selected = {poc_index}
    selected_volume = volumes[poc_index]
    target_volume = volumes.sum() * value_area_pct
    low_index = high_index = poc_index

    while selected_volume < target_volume and (low_index > 0 or high_index < rows - 1):
        below_volume = volumes[low_index - 1] if low_index > 0 else -1
        above_volume = volumes[high_index + 1] if high_index < rows - 1 else -1
        if above_volume >= below_volume and high_index < rows - 1:
            high_index += 1
            selected.add(high_index)
            selected_volume += volumes[high_index]
        elif low_index > 0:
            low_index -= 1
            selected.add(low_index)
            selected_volume += volumes[low_index]
        else:
            break

    centers = (edges[:-1] + edges[1:]) / 2
    return VolumeProfile(
        vah=float(centers[max(selected)]),
        val=float(centers[min(selected)]),
        poc=float(centers[poc_index]),
        total_volume=float(volumes.sum()),
    )
