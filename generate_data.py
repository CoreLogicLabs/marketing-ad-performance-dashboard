"""
generate_data.py
================
Generates a realistic synthetic *advertising performance* dataset for the
interactive marketing dashboard.

A single CSV file is produced inside the ``data/`` directory:

* ``ad_campaigns.csv`` -- one row per (date, channel, campaign) with the five
  additive base metrics: impressions, clicks, spend, conversions, revenue.

Design notes
------------
The data is intentionally crafted to look like a real multi-channel paid-media
account so the dashboard has a story to tell:

* **Distinct channel economics.** Google is the balanced workhorse, Meta is
  the mid-tier performer, TikTok buys cheap clicks that convert poorly, and
  LinkedIn is expensive with few but high-value B2B conversions.
* **Campaign archetypes.** Within each channel, bottom-of-funnel campaigns
  (Brand Search, Retargeting) convert far better than top-of-funnel ones
  (Prospecting, Lookalike), which carry the volume.
* **Weekly seasonality** (mid-week peaks, weekend dips), a **mild upward
  trend** across the 6-month window, and **day-to-day noise**.

The generator only ever emits the *additive* base metrics. Every ratio metric
(CTR, CPC, CPA, CVR, ROAS, CPM) is derived downstream in ``data_utils`` from
summed base metrics, so there is a single source of truth and no possibility
of inconsistent aggregates.

Everything is driven by a fixed NumPy seed (42) so the output is fully
reproducible from one run to the next.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
SEED = 42
PERIOD_DAYS = 182  # ~6 months, an exact number of whole weeks

# Base column schema emitted to CSV (all additive).
BASE_COLUMNS = [
    "date",
    "channel",
    "campaign",
    "impressions",
    "clicks",
    "spend",
    "conversions",
    "revenue",
]

# Which campaigns run on each channel. Not every archetype exists everywhere:
# "Brand Search" only makes sense on Google; LinkedIn runs a lean B2B setup.
CHANNEL_CAMPAIGNS: dict[str, list[str]] = {
    "Google Ads":   ["Brand Search", "Retargeting", "Prospecting", "Lookalike"],
    "Meta Ads":     ["Retargeting", "Prospecting", "Lookalike"],
    "TikTok Ads":   ["Retargeting", "Prospecting", "Lookalike"],
    "LinkedIn Ads": ["Retargeting", "Prospecting"],
}

# Per-channel economics expressed at the "Lookalike" baseline. Campaign
# multipliers below scale these into the final per-campaign numbers.
#   impressions -- baseline daily impressions for a Lookalike-sized campaign
#   ctr         -- baseline click-through rate
#   cpc         -- baseline cost per click (USD)
#   cvr         -- baseline conversion rate (conversions / clicks)
#   aov         -- baseline revenue per conversion (USD)
CHANNEL_PROFILE: dict[str, dict[str, float]] = {
    "Google Ads":   {"impressions": 16000, "ctr": 0.0380, "cpc": 1.25, "cvr": 0.050, "aov": 85.0},
    "Meta Ads":     {"impressions": 30000, "ctr": 0.0130, "cpc": 0.80, "cvr": 0.030, "aov": 70.0},
    "TikTok Ads":   {"impressions": 48000, "ctr": 0.0100, "cpc": 0.35, "cvr": 0.014, "aov": 50.0},
    "LinkedIn Ads": {"impressions": 9000,  "ctr": 0.0065, "cpc": 5.50, "cvr": 0.020, "aov": 350.0},
}

# Campaign archetype multipliers applied on top of the channel baseline.
#   vol -- relative impression volume (Prospecting carries the funnel top)
#   ctr -- click-through multiplier (intent-rich campaigns click more)
#   cvr -- conversion multiplier (Brand/Retargeting convert far better)
#   aov -- order-value multiplier (loyal/intent traffic spends a bit more)
CAMPAIGN_PROFILE: dict[str, dict[str, float]] = {
    "Brand Search": {"vol": 0.35, "ctr": 2.2, "cvr": 2.4, "aov": 1.15},
    "Retargeting":  {"vol": 0.55, "ctr": 1.6, "cvr": 1.9, "aov": 1.10},
    "Prospecting":  {"vol": 1.50, "ctr": 0.8, "cvr": 0.7, "aov": 0.95},
    "Lookalike":    {"vol": 1.00, "ctr": 1.0, "cvr": 1.0, "aov": 1.00},
}

# Weekly seasonality multipliers (Mon=0 ... Sun=6): mid-week peaks, weekend dip.
WEEKDAY_FACTOR = np.array([1.04, 1.08, 1.06, 1.02, 0.98, 0.86, 0.90])

DAILY_TREND = 0.0016  # ~+34% compounded growth in impressions across the window

# Noise levels (sigma of the underlying log-normal multipliers).
IMPRESSION_NOISE = 0.14
RATE_NOISE = 0.06   # applied to CTR and CVR
COST_NOISE = 0.10   # applied to CPC and AOV


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _build_dates(end_date: date, days: int) -> list[date]:
    """Return an ascending list of ``days`` dates ending on ``end_date``."""
    start = end_date - timedelta(days=days - 1)
    return [start + timedelta(days=i) for i in range(days)]


def _jitter(rng: np.random.Generator, value: float, sigma: float) -> float:
    """Multiply ``value`` by a log-normal noise factor (mean ~1.0)."""
    return value * float(rng.lognormal(mean=0.0, sigma=sigma))


# --------------------------------------------------------------------------- #
# Data generation
# --------------------------------------------------------------------------- #
def _generate_row(
    rng: np.random.Generator,
    day_index: int,
    day: date,
    channel: str,
    campaign: str,
) -> dict:
    """Generate one (date, channel, campaign) record of base metrics.

    The pipeline is strictly funnel-shaped so the numbers are always internally
    valid (clicks <= impressions, conversions <= clicks):

        impressions -> clicks (binomial @ CTR) -> conversions (binomial @ CVR)
        spend   = clicks * CPC
        revenue = conversions * AOV
    """
    chan = CHANNEL_PROFILE[channel]
    camp = CAMPAIGN_PROFILE[campaign]

    seasonal = WEEKDAY_FACTOR[day.weekday()]
    trend = (1.0 + DAILY_TREND) ** day_index

    # 1) Impressions: baseline * campaign volume * seasonality * trend * noise.
    expected_impressions = chan["impressions"] * camp["vol"] * seasonal * trend
    impressions = int(_jitter(rng, expected_impressions, IMPRESSION_NOISE))
    impressions = max(impressions, 0)

    # 2) Clicks: binomial draw at the effective CTR (channel * campaign * noise).
    ctr_eff = float(np.clip(_jitter(rng, chan["ctr"] * camp["ctr"], RATE_NOISE), 1e-4, 0.95))
    clicks = int(rng.binomial(impressions, ctr_eff)) if impressions > 0 else 0

    # 3) Conversions: binomial draw at the effective CVR.
    cvr_eff = float(np.clip(_jitter(rng, chan["cvr"] * camp["cvr"], RATE_NOISE), 1e-4, 0.95))
    conversions = int(rng.binomial(clicks, cvr_eff)) if clicks > 0 else 0

    # 4) Spend and revenue follow from the realised click / conversion counts.
    cpc_eff = _jitter(rng, chan["cpc"], COST_NOISE)
    aov_eff = _jitter(rng, chan["aov"] * camp["aov"], COST_NOISE)
    spend = round(clicks * cpc_eff, 2)
    revenue = round(conversions * aov_eff, 2)

    return {
        "date": day.isoformat(),
        "channel": channel,
        "campaign": campaign,
        "impressions": impressions,
        "clicks": clicks,
        "spend": spend,
        "conversions": conversions,
        "revenue": revenue,
    }


def generate_campaigns(rng: np.random.Generator, dates: list[date]) -> pd.DataFrame:
    """Generate the full (date x channel x campaign) base-metric dataset."""
    records: list[dict] = []
    for day_index, day in enumerate(dates):
        for channel, campaigns in CHANNEL_CAMPAIGNS.items():
            for campaign in campaigns:
                records.append(_generate_row(rng, day_index, day, channel, campaign))

    df = pd.DataFrame.from_records(records, columns=BASE_COLUMNS)
    return df.sort_values(["date", "channel", "campaign"]).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def generate(
    data_dir: str | Path | None = None,
    end_date: date | None = None,
    days: int = PERIOD_DAYS,
) -> Path:
    """Generate ``ad_campaigns.csv`` and write it to ``data_dir``.

    Returns the path to the written CSV file. ``data_dir`` defaults to a
    ``data/`` folder next to this script so the output location is independent
    of the current working directory (important on Streamlit Cloud).
    """
    rng = np.random.default_rng(SEED)
    end_date = end_date or date.today()
    dates = _build_dates(end_date, days)

    df = generate_campaigns(rng, dates)

    data_dir = Path(data_dir) if data_dir is not None else Path(__file__).resolve().parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    out_path = data_dir / "ad_campaigns.csv"
    df.to_csv(out_path, index=False)

    print(
        f"  - ad_campaigns.csv : {len(df):,} rows "
        f"({dates[0].isoformat()} -> {dates[-1].isoformat()})"
    )
    print(
        f"    channels={df['channel'].nunique()}  "
        f"campaigns={df['campaign'].nunique()}  "
        f"total spend=${df['spend'].sum():,.0f}  "
        f"total revenue=${df['revenue'].sum():,.0f}"
    )
    return out_path


if __name__ == "__main__":
    print("Generating synthetic ad-campaign data...")
    generate()
    print("Done.")
