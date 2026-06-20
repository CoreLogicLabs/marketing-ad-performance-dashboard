"""
app.py
======
Interactive marketing / ad-performance dashboard built with Streamlit + Plotly.

Run locally with::

    streamlit run app.py

The app loads a synthetic 6-month, multi-channel paid-media dataset, lets the
user slice it by date range / channel / campaign in the sidebar, and reacts
live: KPI cards (with period-over-period deltas), time-series and comparison
charts, a sortable campaign table and an auto-generated "Key Takeaways" panel.

All metrics come from ``data_utils`` so the cards, charts and table are always
mutually consistent regardless of the active filters.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import data_utils as du
import generate_data

# --------------------------------------------------------------------------- #
# Theme / constants
# --------------------------------------------------------------------------- #
PRIMARY = "#2563eb"   # blue
ACCENT = "#10b981"    # green

# Stable per-channel colours: the brand blue/green palette plus two accents so
# all four channels stay distinguishable in every chart.
CHANNEL_COLORS = {
    "Google Ads": PRIMARY,
    "Meta Ads": ACCENT,
    "TikTok Ads": "#f59e0b",    # amber
    "LinkedIn Ads": "#8b5cf6",  # violet
}

st.set_page_config(
    page_title="Marketing Performance Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# A little CSS polish: tighter top padding and calmer metric labels.
st.markdown(
    """
    <style>
      .block-container { padding-top: 2.2rem; padding-bottom: 3rem; }
      [data-testid="stMetricLabel"] { opacity: 0.75; }
      [data-testid="stMetricValue"] { font-size: 1.7rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def money(value: float, decimals: int = 0) -> str:
    return f"${value:,.{decimals}f}"


def pct(fraction: float, decimals: int = 2) -> str:
    return f"{fraction * 100:.{decimals}f}%"


def delta_label(fraction: float | None) -> str | None:
    """Format a relative delta as e.g. ``+12.3%`` for ``st.metric``."""
    return None if fraction is None else f"{fraction * 100:+.1f}%"


# --------------------------------------------------------------------------- #
# Data loading (auto-generate on first run / fresh deploy)
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner="Generating sample data...")
def ensure_dataset() -> str:
    """Make sure the CSV exists, generating it if needed. Returns its path."""
    if not du.DEFAULT_DATA_PATH.exists():
        generate_data.generate()
    return str(du.DEFAULT_DATA_PATH)


data_path = ensure_dataset()
data = du.load_data(data_path)

min_date, max_date = du.date_bounds(data)


# --------------------------------------------------------------------------- #
# Sidebar filters
# --------------------------------------------------------------------------- #
st.sidebar.header("🔎 Filters")

date_selection = st.sidebar.date_input(
    "Date range",
    value=(min_date, max_date),
    min_value=min_date,
    max_value=max_date,
    help="Drives the current period; deltas compare against the preceding window of equal length.",
)

# st.date_input returns a single date until both ends are picked.
if isinstance(date_selection, (list, tuple)) and len(date_selection) == 2:
    start_date, end_date = date_selection
else:
    start_date = end_date = (
        date_selection[0] if isinstance(date_selection, (list, tuple)) else date_selection
    )

all_channels = du.list_channels(data)
selected_channels = st.sidebar.multiselect(
    "Channels",
    options=all_channels,
    default=all_channels,
)

# Campaign options follow the selected channels so the list stays relevant.
available_campaigns = du.list_campaigns(data, selected_channels or all_channels)
selected_campaigns = st.sidebar.multiselect(
    "Campaigns",
    options=available_campaigns,
    default=available_campaigns,
)

st.sidebar.markdown("---")
st.sidebar.caption(
    f"Dataset: **{len(data):,}** rows · "
    f"{min_date:%d %b %Y} → {max_date:%d %b %Y}"
)


# --------------------------------------------------------------------------- #
# Apply filters + build current / previous-period frames
# --------------------------------------------------------------------------- #
# Filter on channel/campaign first so the previous-period lookback can reach
# back before the selected start date.
scoped = du.filter_data(
    data,
    channels=selected_channels or all_channels,
    campaigns=selected_campaigns or available_campaigns,
)

current = du.filter_data(scoped, start=start_date, end=end_date)

prev_start, prev_end = du.previous_period(start_date, end_date)
previous = du.filter_data(scoped, start=prev_start, end=prev_end)


# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
st.title("📈 Marketing Performance Dashboard")
st.markdown(
    "Interactive view of a 6-month, multi-channel paid-media account. "
    "Use the sidebar to slice by **date range**, **channel** and **campaign** — "
    "every KPI, chart and table updates live. Deltas compare the selected period "
    "with the immediately preceding window of equal length."
)

if current.empty:
    st.warning("No data matches the current filters. Widen the date range or selections.")
    st.stop()

st.caption(
    f"Showing **{start_date:%d %b %Y} → {end_date:%d %b %Y}**  ·  "
    f"vs previous **{prev_start:%d %b %Y} → {prev_end:%d %b %Y}**"
)


# --------------------------------------------------------------------------- #
# KPI cards
# --------------------------------------------------------------------------- #
kpis = du.compute_kpis(current)
deltas = du.compute_deltas(kpis, du.compute_kpis(previous))

st.subheader("Key metrics")
row = st.columns(6)
row[0].metric("Total Spend", money(kpis["spend"]), delta_label(deltas["spend"]), delta_color="off")
row[1].metric("Total Revenue", money(kpis["revenue"]), delta_label(deltas["revenue"]))
row[2].metric("Blended ROAS", f"{kpis['roas']:.2f}x", delta_label(deltas["roas"]))
# For CPA lower is better -> invert delta colouring.
row[3].metric("Avg CPA", money(kpis["cpa"], 2), delta_label(deltas["cpa"]), delta_color="inverse")
row[4].metric("Total Conversions", f"{kpis['conversions']:,.0f}", delta_label(deltas["conversions"]))
row[5].metric("Avg CTR", pct(kpis["ctr"]), delta_label(deltas["ctr"]))

st.markdown("---")


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
def chart_spend_vs_revenue(df: pd.DataFrame) -> go.Figure:
    """Dual-axis daily line chart: spend (left) vs revenue (right)."""
    daily = du.aggregate(df, "date").sort_values("date")
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Scatter(
            x=daily["date"], y=daily["spend"], name="Spend",
            mode="lines", line=dict(color=PRIMARY, width=2),
            hovertemplate="%{x|%d %b %Y}<br>Spend: $%{y:,.0f}<extra></extra>",
        ),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=daily["date"], y=daily["revenue"], name="Revenue",
            mode="lines", line=dict(color=ACCENT, width=2),
            hovertemplate="%{x|%d %b %Y}<br>Revenue: $%{y:,.0f}<extra></extra>",
        ),
        secondary_y=True,
    )
    fig.update_yaxes(title_text="Spend ($)", secondary_y=False, color=PRIMARY)
    fig.update_yaxes(title_text="Revenue ($)", secondary_y=True, color=ACCENT)
    fig.update_layout(
        margin=dict(l=10, r=10, t=10, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        hovermode="x unified",
        height=380,
    )
    return fig


def chart_roas_by_channel(df: pd.DataFrame) -> go.Figure:
    """Bar chart comparing blended ROAS across channels."""
    by_channel = du.aggregate(df, "channel").sort_values("roas", ascending=False)
    fig = px.bar(
        by_channel, x="channel", y="roas", text="roas",
        color="channel", color_discrete_map=CHANNEL_COLORS,
    )
    fig.update_traces(
        texttemplate="%{text:.2f}x", textposition="outside",
        hovertemplate="%{x}<br>ROAS: %{y:.2f}x<extra></extra>",
    )
    fig.update_layout(
        margin=dict(l=10, r=10, t=10, b=10), height=340,
        showlegend=False, xaxis_title=None, yaxis_title="ROAS (x)",
    )
    return fig


def chart_spend_share(df: pd.DataFrame) -> go.Figure:
    """Donut chart of spend distribution across channels."""
    by_channel = du.aggregate(df, "channel")
    fig = px.pie(
        by_channel, names="channel", values="spend", hole=0.55,
        color="channel", color_discrete_map=CHANNEL_COLORS,
    )
    fig.update_traces(
        textposition="inside", textinfo="percent+label",
        hovertemplate="%{label}<br>Spend: $%{value:,.0f} (%{percent})<extra></extra>",
    )
    fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=340, showlegend=False)
    return fig


def chart_cpa_vs_roas(df: pd.DataFrame) -> go.Figure:
    """Bubble scatter: CPA (x) vs ROAS (y), bubble size = spend, per campaign."""
    by_campaign = du.aggregate(df, ["channel", "campaign"])
    by_campaign = by_campaign[by_campaign["spend"] > 0]
    fig = px.scatter(
        by_campaign, x="cpa", y="roas", size="spend", color="channel",
        color_discrete_map=CHANNEL_COLORS, hover_name="campaign",
        size_max=46, custom_data=["channel", "spend", "conversions"],
    )
    fig.update_traces(
        hovertemplate=(
            "<b>%{hovertext}</b> — %{customdata[0]}<br>"
            "CPA: $%{x:,.2f}<br>ROAS: %{y:.2f}x<br>"
            "Spend: $%{customdata[1]:,.0f}<br>Conversions: %{customdata[2]:,.0f}"
            "<extra></extra>"
        )
    )
    fig.update_layout(
        margin=dict(l=10, r=10, t=10, b=10), height=420,
        xaxis_title="CPA ($, lower is better)", yaxis_title="ROAS (x, higher is better)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title=None),
    )
    return fig


st.subheader("Spend vs revenue over time")
st.plotly_chart(chart_spend_vs_revenue(current), width="stretch")

left, right = st.columns(2)
with left:
    st.subheader("ROAS by channel")
    st.plotly_chart(chart_roas_by_channel(current), width="stretch")
with right:
    st.subheader("Spend distribution")
    st.plotly_chart(chart_spend_share(current), width="stretch")

st.subheader("Efficiency map — CPA vs ROAS")
st.caption("Each bubble is a campaign; bubble size is spend. Top-left is the sweet spot: low CPA, high ROAS.")
st.plotly_chart(chart_cpa_vs_roas(current), width="stretch")


# --------------------------------------------------------------------------- #
# Campaign performance table
# --------------------------------------------------------------------------- #
st.subheader("Campaign performance")
st.caption("Click any column header to sort. Metrics are computed from summed base values.")

table = du.aggregate(current, ["channel", "campaign"]).sort_values("spend", ascending=False)
table = table[
    ["channel", "campaign"] + du.BASE_METRICS + ["ctr", "cpc", "cpm", "cvr", "cpa", "roas"]
].rename(
    columns={
        "channel": "Channel", "campaign": "Campaign", "impressions": "Impressions",
        "clicks": "Clicks", "spend": "Spend", "conversions": "Conversions",
        "revenue": "Revenue", "ctr": "CTR", "cpc": "CPC", "cpm": "CPM",
        "cvr": "CVR", "cpa": "CPA", "roas": "ROAS",
    }
)

styled = table.style.format(
    {
        "Impressions": "{:,.0f}", "Clicks": "{:,.0f}", "Conversions": "{:,.0f}",
        "Spend": "${:,.0f}", "Revenue": "${:,.0f}",
        "CTR": "{:.2%}", "CVR": "{:.2%}",
        "CPC": "${:,.2f}", "CPM": "${:,.2f}", "CPA": "${:,.2f}",
        "ROAS": "{:.2f}x",
    }
).background_gradient(subset=["ROAS"], cmap="Greens")

st.dataframe(styled, width="stretch", hide_index=True)


# --------------------------------------------------------------------------- #
# Key takeaways (auto-generated from the filtered data)
# --------------------------------------------------------------------------- #
def build_takeaways(df: pd.DataFrame) -> list[str]:
    """Derive plain-language insights from the current selection."""
    insights: list[str] = []

    by_channel = du.aggregate(df, "channel")
    # Only judge efficiency on channels with meaningful spend.
    material = by_channel[by_channel["spend"] >= by_channel["spend"].max() * 0.05]
    if material.empty:
        material = by_channel

    best = material.loc[material["roas"].idxmax()]
    worst = material.loc[material["roas"].idxmin()]
    insights.append(
        f"🏆 **{best['channel']}** is the strongest channel at **{best['roas']:.2f}x ROAS** "
        f"(${best['revenue']:,.0f} revenue on ${best['spend']:,.0f} spend)."
    )
    if worst["channel"] != best["channel"]:
        insights.append(
            f"⚠️ **{worst['channel']}** lags at **{worst['roas']:.2f}x ROAS** "
            f"and **${worst['cpa']:,.0f} CPA** — the first place to tighten "
            f"targeting or reallocate budget."
        )

    by_campaign = du.aggregate(df, ["channel", "campaign"])
    material_camp = by_campaign[by_campaign["spend"] >= by_campaign["spend"].max() * 0.05]
    if not material_camp.empty:
        top = material_camp.loc[material_camp["roas"].idxmax()]
        insights.append(
            f"💡 Most efficient campaign: **{top['campaign']} · {top['channel']}** "
            f"at **{top['roas']:.2f}x ROAS** and **${top['cpa']:,.0f} CPA**."
        )

    # Budget-reallocation hint: what worst-channel spend would return at best ROAS.
    if worst["channel"] != best["channel"] and worst["spend"] > 0:
        uplift = worst["spend"] * (best["roas"] - worst["roas"])
        if uplift > 0:
            insights.append(
                f"📊 Shifting **{worst['channel']}**'s ${worst['spend']:,.0f} spend toward "
                f"**{best['channel']}**-level efficiency could unlock roughly "
                f"**${uplift:,.0f}** in incremental revenue."
            )

    totals = du.compute_kpis(df)
    insights.append(
        f"📈 Blended account ROAS is **{totals['roas']:.2f}x** with a **{totals['ctr'] * 100:.2f}%** "
        f"CTR and **${totals['cpa']:,.0f}** average CPA across the selection."
    )
    return insights


st.subheader("Key takeaways")
for line in build_takeaways(current):
    st.markdown(f"- {line}")

st.markdown("---")
st.caption(
    "Synthetic data for portfolio demonstration · Built with Streamlit & Plotly · "
    "All ratio metrics derive from a single source of truth in `data_utils.py`."
)
