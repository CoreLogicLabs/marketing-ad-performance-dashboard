"""
data_utils.py
=============
Data loading, filtering and metric calculation for the marketing dashboard.

**Single source of truth.** Every ratio metric in the dashboard (CTR, CPC, CPA,
CVR, ROAS, CPM) is computed in exactly one place -- :func:`_derived_from_sums`
-- from *summed* additive base metrics. KPI cards, per-channel bars and the
campaign table therefore can never disagree with one another: a channel's CTR
is always ``sum(clicks) / sum(impressions)`` for that channel, and the blended
CTR is the same formula over the whole selection. Ratios are never averaged
from row-level ratios (which would silently weight small days the same as big
ones and produce inconsistent totals).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# data_utils is only imported by the Streamlit app, so depending on streamlit
# for its caching primitives is safe.
import streamlit as st

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
DEFAULT_DATA_PATH = Path(__file__).resolve().parent / "data" / "ad_campaigns.csv"

# Additive base metrics. These are the only quantities that may be summed.
BASE_METRICS = ["impressions", "clicks", "spend", "conversions", "revenue"]

# Derived (ratio) metrics, in display order.
DERIVED_METRICS = ["ctr", "cpc", "cpm", "cvr", "cpa", "roas"]


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def load_data(path: str | Path = DEFAULT_DATA_PATH) -> pd.DataFrame:
    """Load the campaign CSV into a DataFrame with parsed dates.

    The result is cached by Streamlit, so the file is read from disk only once
    per session regardless of how many times filters change.
    """
    df = pd.read_csv(path, parse_dates=["date"])
    df["date"] = df["date"].dt.normalize()
    return df.sort_values(["date", "channel", "campaign"]).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Metric calculation (single source of truth)
# --------------------------------------------------------------------------- #
def safe_divide(numerator, denominator):
    """Divide ``numerator`` by ``denominator``, yielding 0 where the
    denominator is 0 (instead of NaN / inf).

    Accepts either pandas Series (vectorised, for grouped frames) or plain
    scalars (for whole-selection KPIs) and returns the matching type.
    """
    if isinstance(denominator, pd.Series):
        denom = denominator.where(denominator != 0, np.nan)
        return (numerator / denom).fillna(0.0)
    if not denominator:  # 0, 0.0 or NaN
        return 0.0
    return numerator / denominator


def _derived_from_sums(impressions, clicks, spend, conversions, revenue) -> dict:
    """Compute every ratio metric from already-summed base metrics.

    This is the ONLY place the ratio formulas live. ``*_from_sums`` arguments
    may be scalars or Series; the return values match.
    """
    return {
        "ctr": safe_divide(clicks, impressions),          # click-through rate
        "cpc": safe_divide(spend, clicks),                # cost per click
        "cpm": safe_divide(spend, impressions) * 1000.0,  # cost per 1k impressions
        "cvr": safe_divide(conversions, clicks),          # conversion rate
        "cpa": safe_divide(spend, conversions),           # cost per acquisition
        "roas": safe_divide(revenue, spend),              # return on ad spend
    }


def attach_derived_metrics(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``frame`` with the six derived metric columns added.

    ``frame`` must already contain the summed :data:`BASE_METRICS` columns
    (e.g. the output of a ``groupby().sum()``).
    """
    out = frame.copy()
    derived = _derived_from_sums(
        out["impressions"], out["clicks"], out["spend"],
        out["conversions"], out["revenue"],
    )
    for name, values in derived.items():
        out[name] = values
    return out


def aggregate(df: pd.DataFrame, by: str | list[str]) -> pd.DataFrame:
    """Group ``df`` by ``by``, sum the base metrics and attach derived metrics.

    Used for every chart and table: per-channel, per-campaign, per-day, etc.
    """
    grouped = df.groupby(by, as_index=False)[BASE_METRICS].sum()
    return attach_derived_metrics(grouped)


def compute_kpis(df: pd.DataFrame) -> dict[str, float]:
    """Return whole-selection KPIs: summed base metrics + derived ratios.

    The derived values come from the same :func:`_derived_from_sums` used for
    grouped frames, guaranteeing the KPI cards agree with the charts.
    """
    sums = {metric: float(df[metric].sum()) for metric in BASE_METRICS}
    derived = _derived_from_sums(**sums)
    return {**sums, **derived}


def compute_deltas(current: dict[str, float], previous: dict[str, float]) -> dict[str, float | None]:
    """Relative change of each KPI vs the previous period.

    Returns a fraction (e.g. ``0.123`` for +12.3%), or ``None`` when there is
    no previous-period baseline to compare against.
    """
    deltas: dict[str, float | None] = {}
    for key, value in current.items():
        base = previous.get(key, 0.0)
        deltas[key] = (value - base) / base if base else None
    return deltas


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #
def date_bounds(df: pd.DataFrame) -> tuple[date, date]:
    """Return the (min, max) calendar dates present in ``df``."""
    return df["date"].min().date(), df["date"].max().date()


def list_channels(df: pd.DataFrame) -> list[str]:
    """All channels present, sorted for stable widget ordering."""
    return sorted(df["channel"].unique().tolist())


def list_campaigns(df: pd.DataFrame, channels: list[str] | None = None) -> list[str]:
    """All campaigns present, optionally restricted to ``channels``."""
    subset = df[df["channel"].isin(channels)] if channels else df
    return sorted(subset["campaign"].unique().tolist())


def filter_data(
    df: pd.DataFrame,
    start: date | None = None,
    end: date | None = None,
    channels: list[str] | None = None,
    campaigns: list[str] | None = None,
) -> pd.DataFrame:
    """Filter ``df`` by an (inclusive) date range, channels and campaigns.

    Any argument left as ``None`` (or an empty list) is treated as "no filter"
    on that dimension.
    """
    mask = pd.Series(True, index=df.index)
    if start is not None:
        mask &= df["date"] >= pd.Timestamp(start)
    if end is not None:
        mask &= df["date"] <= pd.Timestamp(end)
    if channels:
        mask &= df["channel"].isin(channels)
    if campaigns:
        mask &= df["campaign"].isin(campaigns)
    return df[mask].copy()


def previous_period(start: date, end: date) -> tuple[date, date]:
    """Return the immediately preceding window of equal length.

    For a selection of ``N`` days ending on ``end``, the comparison window is
    the ``N`` days ending the day before ``start``.
    """
    span = (end - start).days  # inclusive length is span + 1 days
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span)
    return prev_start, prev_end
