from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telltale.pipeline.signals import build_signal_report
from telltale.storage.models import (
    JobPosting,
    PostingClassification,
    WeeklyBrief,
)
from telltale.storage.session import get_session

st.set_page_config(page_title="TELLTALE", layout="wide")

# Must match the version the pipeline classifies at (PROMPT_VERSION in
# .github/workflows/weekly.yml and VERSION_PROFILES in telltale/llm/client.py).
PROMPT_VERSION = "v3"


@st.cache_data(ttl=300)
def load_postings(prompt_version: str) -> pd.DataFrame:
    session = get_session()
    try:
        rows = session.execute(
            select(
                JobPosting.id,
                JobPosting.company_slug,
                JobPosting.title,
                JobPosting.location,
                JobPosting.url,
                JobPosting.is_open,
                JobPosting.first_seen_at,
                PostingClassification.function_category,
                PostingClassification.seniority_level,
                PostingClassification.rationale,
            )
            .join(
                PostingClassification,
                PostingClassification.posting_id == JobPosting.id,
            )
            .where(PostingClassification.prompt_version == prompt_version)
        ).all()
    finally:
        session.close()
    return pd.DataFrame(rows, columns=[
        "id", "company", "title", "location", "url", "is_open",
        "first_seen", "function", "seniority", "rationale",
    ])


@st.cache_data(ttl=300)
def load_briefs() -> list[dict]:
    session = get_session()
    try:
        rows = session.execute(
            select(WeeklyBrief).order_by(WeeklyBrief.week_start.desc())
        ).scalars().all()
        return [
            {
                "week_start": b.week_start,
                "generated_at": b.generated_at,
                "markdown": b.brief_markdown,
            }
            for b in rows
        ]
    finally:
        session.close()


@st.cache_data(ttl=300)
def load_signals(prompt_version: str) -> dict:
    return build_signal_report(prompt_version=prompt_version).to_dict()


df = load_postings(PROMPT_VERSION)

st.title("TELLTALE")
st.caption("Hiring signals across Indian fintech")

if df.empty:
    st.warning(
        f"No classifications found at prompt_version={PROMPT_VERSION!r}. "
        "Run `telltale scrape` then `telltale classify`."
    )
    st.stop()

open_df = df[df["is_open"]]
signals = load_signals(PROMPT_VERSION)

view = st.sidebar.radio(
    "View", ["Overview", "By company", "By function", "Past briefs"]
)
st.sidebar.metric("Open postings", len(open_df))
st.sidebar.metric("Companies", open_df["company"].nunique())
if signals.get("cold_start"):
    st.sidebar.info("Cold start: one week of data, no comparisons available.")


# ---------------------------------------------------------------- Overview
if view == "Overview":
    st.subheader("Function mix by company")
    st.caption("Share of each company's open roles, so larger companies do not dominate.")

    mix = (
        open_df.groupby(["company", "function"]).size().reset_index(name="count")
    )
    totals = mix.groupby("company")["count"].transform("sum")
    mix["share"] = mix["count"] / totals * 100

    order = open_df["company"].value_counts().index.tolist()
    fig = px.bar(
        mix,
        x="company",
        y="share",
        color="function",
        category_orders={"company": order},
        labels={"share": "share of open roles (%)", "company": "", "function": "function"},
        height=460,
    )
    fig.update_layout(barmode="stack", legend_title_text="", margin=dict(t=10))
    st.plotly_chart(fig, width='stretch')

    left, right = st.columns(2)

    with left:
        st.subheader("Sector-wide function mix")
        sector = pd.DataFrame(signals["sector_wide"])
        st.dataframe(
            sector[["function", "open_now", "share_of_sector_pct", "net"]].rename(
                columns={
                    "open_now": "open",
                    "share_of_sector_pct": "share %",
                    "net": "net this week",
                }
            ),
            hide_index=True,
            width='stretch',
        )

    with right:
        st.subheader("Biggest movers")
        moves = pd.DataFrame(signals["moves_this_week"])
        if moves.empty:
            st.info("No opens or closes recorded in this week's window.")
        elif signals.get("cold_start"):
            st.info(
                "Cold start: every posting is counted as opened in this first "
                "snapshot, so movement is not yet meaningful. Showing the largest "
                "initial entries."
            )
            st.dataframe(
                moves.nlargest(10, "opened")[["company", "function", "opened"]],
                hide_index=True,
                width='stretch',
            )
        else:
            moves["abs_net"] = moves["net"].abs()
            st.dataframe(
                moves.nlargest(10, "abs_net")[
                    ["company", "function", "opened", "closed", "net"]
                ],
                hide_index=True,
                width='stretch',
            )


# ------------------------------------------------------------- By company
elif view == "By company":
    company = st.selectbox("Company", sorted(open_df["company"].unique()))
    sub = open_df[open_df["company"] == company]

    c1, c2, c3 = st.columns(3)
    c1.metric("Open roles", len(sub))
    c2.metric("Functions", sub["function"].nunique())
    top = sub["function"].value_counts()
    c3.metric("Largest function", top.index[0], f"{top.iloc[0] / len(sub):.0%}")

    left, right = st.columns(2)
    with left:
        st.subheader("Function mix")
        fn = sub["function"].value_counts().reset_index()
        fn.columns = ["function", "count"]
        fig = px.bar(fn, x="count", y="function", orientation="h", height=380)
        fig.update_layout(yaxis={"categoryorder": "total ascending"}, margin=dict(t=10))
        st.plotly_chart(fig, width='stretch')

    with right:
        st.subheader("Seniority mix")
        order = ["Intern", "Junior", "Mid", "Senior", "Staff/Principal", "Leadership"]
        sen = sub["seniority"].value_counts().reindex(order).fillna(0).reset_index()
        sen.columns = ["seniority", "count"]
        fig = px.bar(sen, x="seniority", y="count", height=380)
        fig.update_layout(margin=dict(t=10))
        st.plotly_chart(fig, width='stretch')

    st.subheader("Open roles")
    st.dataframe(
        sub[["title", "function", "seniority", "location"]].sort_values("function"),
        hide_index=True,
        width='stretch',
    )


# ------------------------------------------------------------ By function
elif view == "By function":
    function = st.selectbox("Function", sorted(open_df["function"].unique()))
    sub = open_df[open_df["function"] == function]

    c1, c2 = st.columns(2)
    c1.metric("Open roles", len(sub))
    c2.metric("Share of all open roles", f"{len(sub) / len(open_df):.1%}")

    st.subheader("Which companies are hiring for this")
    by_co = sub["company"].value_counts().reset_index()
    by_co.columns = ["company", "count"]
    by_co["share of that company's roles"] = by_co["company"].map(
        lambda c: f"{len(sub[sub['company'] == c]) / len(open_df[open_df['company'] == c]):.0%}"
    )
    left, right = st.columns([2, 3])
    with left:
        st.dataframe(by_co, hide_index=True, width='stretch')
    with right:
        fig = px.bar(by_co, x="company", y="count", height=340)
        fig.update_layout(margin=dict(t=10))
        st.plotly_chart(fig, width='stretch')

    st.subheader("Seniority mix")
    order = ["Intern", "Junior", "Mid", "Senior", "Staff/Principal", "Leadership"]
    sen = sub["seniority"].value_counts().reindex(order).fillna(0).reset_index()
    sen.columns = ["seniority", "count"]
    fig = px.bar(sen, x="seniority", y="count", height=300)
    fig.update_layout(margin=dict(t=10))
    st.plotly_chart(fig, width='stretch')

    st.subheader("Roles")
    st.dataframe(
        sub[["company", "title", "seniority", "location"]].sort_values("company"),
        hide_index=True,
        width='stretch',
    )


# ----------------------------------------------------------- Past briefs
elif view == "Past briefs":
    briefs = load_briefs()
    if not briefs:
        st.info("No briefs generated yet. Run `telltale brief`.")
    else:
        labels = [
            f"{b['week_start'].date()}  (generated {b['generated_at'].date()})"
            for b in briefs
        ]
        idx = st.selectbox(
            "Brief", range(len(briefs)), format_func=lambda i: labels[i]
        )
        st.markdown(briefs[idx]["markdown"])
