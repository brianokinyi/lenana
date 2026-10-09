"""Local, read-only Streamlit tracker. Start with `lenana dashboard`."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlmodel import select

from lenana.config import get_config
from lenana.db import session_scope
from lenana.models import Channel
from lenana.tracker import TrackerData, load_tracker, public_rows, workbook_bytes
from lenana.utils import human_datetime

st.set_page_config(page_title="Lenana · Coin tracker", page_icon="◈", layout="wide")
st.markdown(
    """<style>
.stApp { background: #f7f8f4; }
[data-testid="stMetric"] { background:white; padding:18px; border:1px solid #dce3da;
border-radius:10px; }
h1 { letter-spacing:-1.5px; }
div[data-testid="stTabs"] button { font-weight:600; }
</style>""",
    unsafe_allow_html=True,
)
st.title("Coin tracker")
st.caption("What's being discussed, what's being predicted, and how targets change over time.")
get_config.cache_clear()
cfg = get_config()

with st.sidebar:
    st.header("Your watchlist")
    period = st.selectbox("Published within", ["All saved videos", "7 days", "30 days", "90 days"])
    days = None if period == "All saved videos" else int(period.split()[0])
    with session_scope(cfg) as session:
        channel_rows = session.exec(select(Channel)).all()
        channel_labels = {c.id: c.name or c.handle for c in channel_rows if c.id is not None}
    selected_channels = st.multiselect(
        "Channels", list(channel_labels), format_func=lambda key: channel_labels[key]
    )
    sponsored = st.checkbox("Include sponsored mentions", value=False)
    search = st.text_input("Find a coin", placeholder="BTC, ETH, SOL…").strip().upper()
    st.caption(f"Dates shown in {cfg.settings.display_tz}. Updates automatically every minute.")


def filtered(data: TrackerData) -> TrackerData:
    if not search:
        return data
    for attr in ("leaderboard", "timeline", "targets", "mentions", "prices"):
        setattr(data, attr, [r for r in getattr(data, attr) if search in r["Coin"]])
    return data


@st.fragment(run_every="60s")
def tracker_view() -> None:
    try:
        with session_scope(cfg) as session:
            data = filtered(
                load_tracker(
                    session,
                    cfg,
                    days=days,
                    channels=selected_channels or None,
                    include_sponsored=sponsored,
                )
            )
    except Exception:
        st.error("The tracker database isn't ready. Run `lenana db upgrade` and reopen this page.")
        return
    cols = st.columns(4)
    cols[0].metric("Coins discussed", len(data.leaderboard))
    cols[1].metric("Videos analyzed", data.transcripts)
    cols[2].metric("Numeric targets", sum(t["Target"] is not None for t in data.targets))
    cols[3].metric("Confirmed reached", sum(t["Status"] == "Reached" for t in data.targets))
    st.caption(
        f"Mention scans: {data.scanned}/{data.transcripts} saved transcripts. "
        f"{data.videos} discovered videos match the filters. "
        "Rankings count each coin once per video. Repeated speech doesn't add votes."
    )
    if data.scanned < data.transcripts:
        st.info(
            "Some transcripts have not been scanned for all mentions yet. "
            "Their verified predictions are included. "
            "Run `lenana mentions-backfill` for full coverage."
        )
    st.download_button(
        "Download full spreadsheet",
        workbook_bytes(data, cfg.settings.display_tz),
        file_name="coin-tracker.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    if not data.leaderboard:
        st.info(
            "No coin evidence matches these filters. Broaden the selection or run the pipeline."
        )
        return
    leaderboard, timeline, evidence = st.tabs(
        ["Most mentioned", "Target history", "Source evidence"]
    )
    with leaderboard:
        ranking = pd.DataFrame(public_rows(data.leaderboard))

        def select_ranked_coin() -> None:
            rows = st.session_state["coin_ranking"]["selection"]["rows"]
            if rows and rows[0] < len(data.leaderboard):
                st.session_state["detail_coin"] = data.leaderboard[rows[0]]["Coin"]

        st.dataframe(
            ranking,
            key="coin_ranking",
            on_select=select_ranked_coin,
            selection_mode="single-row",
            hide_index=True,
            width="stretch",
            height=420,
            column_config={
                "Current USD": st.column_config.NumberColumn(format="$%.8f"),
                "Videos mentioning": st.column_config.NumberColumn("Mentions (videos)"),
            },
        )
        st.caption(
            "Weekly change compares distinct videos from the last 7 days to the previous "
            "7 days, within your selected channels and date window."
        )
        st.bar_chart(ranking.head(12).set_index("Coin")[["Videos mentioning"]], color="#28796b")
    with timeline:
        st.caption(
            "Columns show when targets were stated. Every target keeps its channel, "
            "time and stated horizon; conflicting targets are kept separately."
        )
        st.dataframe(
            pd.DataFrame(public_rows(data.timeline)), hide_index=True, width="stretch", height=360
        )
        st.subheader("Every prediction")
        st.dataframe(
            pd.DataFrame(public_rows(data.targets)),
            hide_index=True,
            width="stretch",
            column_config={
                "Source": st.column_config.LinkColumn("Video timestamp", display_text="Watch")
            },
        )
    with evidence:
        st.dataframe(
            pd.DataFrame(public_rows(data.mentions)),
            hide_index=True,
            width="stretch",
            column_config={
                "Source": st.column_config.LinkColumn("Video timestamp", display_text="Watch")
            },
        )
    st.divider()
    coins = [r["Coin"] for r in data.leaderboard]
    if st.session_state.get("detail_coin") not in coins:
        st.session_state["detail_coin"] = coins[0]
    chosen = st.selectbox("Explore a coin", coins, key="detail_coin")
    targets = [t for t in data.targets if t["Coin"] == chosen]
    actual = [p for p in data.prices if p["Coin"] == chosen]
    numeric_usd = [t for t in targets if t["Target"] is not None and t["Currency"].upper() == "USD"]
    st.subheader(f"{chosen} · price and targets")
    if actual or numeric_usd:
        fig = go.Figure()
        if actual:
            frame = pd.DataFrame(actual).sort_values("Date")
            fig.add_trace(
                go.Scatter(
                    x=frame["Date"],
                    y=frame["Price USD"],
                    mode="lines+markers",
                    name="Observed USD price",
                    line={"color": "#28796b"},
                    customdata=[human_datetime(value) for value in frame["Date"]],
                    hovertemplate="%{customdata}<br>USD %{y:,.8f}<extra>Observed price</extra>",
                )
            )
        if numeric_usd:
            fig.add_trace(
                go.Scatter(
                    x=[t["Prediction date"] for t in numeric_usd],
                    y=[t["Target"] for t in numeric_usd],
                    mode="markers",
                    name="Stated USD target",
                    marker={"color": "#e69236", "size": 12, "symbol": "diamond"},
                    customdata=[
                        [
                            t["Channel"],
                            t["Timeframe stated"] or "No deadline",
                            t["Status"],
                            human_datetime(t["Prediction date"]),
                        ]
                        for t in numeric_usd
                    ],
                    hovertemplate="%{customdata[3]}<br>Target USD %{y:,.8f}<br>%{customdata[0]}<br>"
                    "%{customdata[1]}<br>%{customdata[2]}<extra></extra>",
                )
            )
        fig.update_xaxes(tickformat="%d %b %Y", hoverformat="%d %b %Y %I:%M%p")
        fig.update_layout(
            height=380,
            margin={"l": 10, "r": 10, "t": 15, "b": 10},
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            xaxis_title="Date",
            yaxis_title="USD",
            legend={"orientation": "h"},
        )
        st.plotly_chart(fig, width="stretch")
        st.caption(
            "Observed prices are saved samples, not a complete intraday price history. "
            "Target markers show prediction times. Only USD targets are plotted together."
        )
    else:
        st.info("No USD price history or numeric USD targets are saved for this coin yet.")
    if not targets:
        st.info("No target stated. This coin was discussed without a verified price prediction.")
    st.subheader(f"{chosen} · mentions")
    st.dataframe(
        pd.DataFrame(public_rows([m for m in data.mentions if m["Coin"] == chosen])),
        hide_index=True,
        width="stretch",
        column_config={
            "Source": st.column_config.LinkColumn("Video timestamp", display_text="Watch")
        },
    )
    for index, target in enumerate(targets):
        label = (
            f"{human_datetime(target['Prediction date'])} · {target['Channel']} · "
            f"{target['Target'] if target['Target'] is not None else 'No numeric target'} "
            f"{target['Currency']} · {target['Status']}"
        )
        with st.expander(label):
            st.write(f"Speaker: {target['Speaker']} · Direction: {target['Direction']}")
            st.write(f"Timeframe: {target['Timeframe stated'] or 'Not stated'}")
            if target["Deadline"]:
                st.caption(f"Estimated deadline: {human_datetime(target['Deadline'])}")
            st.write(target["Quote"])
            st.link_button("Watch the source quote", target["Source"], key=f"source-{index}")


tracker_view()
