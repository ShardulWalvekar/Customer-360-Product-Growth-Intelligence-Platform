#!/usr/bin/env python3
"""
Meesho Growth Intelligence - AI Business Analyst Assistant (Phase 16)
=====================================================================
A dependency-light Natural Language Query Engine over `meesho_growth_db`.

Stakeholders type plain-English questions; the engine matches an intent,
runs the SQL / pandas logic and answers with business context and a
recommendation. Rule-based intent matching keeps answers deterministic and
auditable (no LLM hallucination risk on financial numbers).

Usage
-----
    python app.py                               # interactive loop
    python app.py --db path/to/meesho_growth_db.sqlite
    python app.py -q "What is the RTO rate?"    # single question, then exit
    python app.py --selftest                    # run every intent once

Data sources (first one found wins)
-----------------------------------
1. SQLite file   : --db, $MESHO_DB, or ./meesho_growth_db.sqlite|.db|.sqlite3
2. CSV fallback  : a folder (--csv-dir, default ./data) holding
                   meesho_sales_flat_analytics.csv, meesho_customers.csv,
                   meesho_feature_events.csv
3. Neither found : the assistant still starts and answers from the
                   documented project baseline, clearly labelled as such.

Expected tables / columns (same as the Power BI model)
------------------------------------------------------
meesho_sales_flat_analytics : Order_ID, Customer_ID, Order_Date, SKU,
    Product_Category, Supplier_Price, Base_Price, "Discount_%", Units_Sold,
    Revenue, Payment_Method, Status   (Payment Class optional)
meesho_customers            : Customer_ID, "RFM Segment"
meesho_feature_events       : Session_ID, Customer_ID, Feature_Name
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
SALES_TABLE = "meesho_sales_flat_analytics"
CUSTOMER_TABLE = "meesho_customers"
EVENTS_TABLE = "meesho_feature_events"

DB_CANDIDATES = [
    "meesho_growth_db.sqlite",
    "meesho_growth_db.db",
    "meesho_growth_db.sqlite3",
    "meesho_growth_db",
]

# Documented project baseline - used ONLY when live data is unavailable or
# when the event log cannot support a sequential funnel calculation.
BASELINE = {
    "gross_revenue": 42_290_000.0,
    "delivered_revenue": 24_200_000.0,
    "rto_revenue": 6_080_000.0,
    "rto_rate_pct": 14.38,
    "cod_share_of_rto_pct": 68.0,
    "funnel": [
        ("Search", 100_000),
        ("Category Filter", 65_000),
        ("Wishlist / Cart", 38_000),
        ("Checkout Click", 22_000),
        ("Completed Order", 10_000),
    ],
}

AT_RISK_SEGMENTS = ["At Risk", "Cannot Lose Them", "Hibernating", "Lost"]
DELIVERED = "DELIVERED"
RTO = "RTO_COMPLETE"


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def inr(value: float) -> str:
    """Format a rupee amount as Rs X.XX Million / Thousand for readability."""
    if value is None or pd.isna(value):
        return "n/a"
    sign = "-" if value < 0 else ""
    v = abs(float(value))
    if v >= 1e7:
        return f"{sign}Rs {v / 1e7:,.2f} Crore"
    if v >= 1e6:
        return f"{sign}Rs {v / 1e6:,.2f} Million"
    if v >= 1e3:
        return f"{sign}Rs {v / 1e3:,.1f} Thousand"
    return f"{sign}Rs {v:,.0f}"


def pct(value: float, digits: int = 2) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{value:.{digits}f}%"


def safe_div(num: float, den: float) -> Optional[float]:
    """Divide-by-zero safe division (returns None instead of raising)."""
    try:
        return None if den in (0, None) or pd.isna(den) else num / den
    except (TypeError, ZeroDivisionError):
        return None


def banner(title: str) -> str:
    line = "=" * 72
    return f"\n{line}\n {title}\n{line}"


# --------------------------------------------------------------------------- #
# Data layer
# --------------------------------------------------------------------------- #
@dataclass
class DataStore:
    """Holds the three analytic frames plus metadata on where they came from."""

    sales: Optional[pd.DataFrame] = None
    customers: Optional[pd.DataFrame] = None
    events: Optional[pd.DataFrame] = None
    source: str = "none"
    notes: List[str] = field(default_factory=list)

    @property
    def has_sales(self) -> bool:
        return self.sales is not None and not self.sales.empty

    @property
    def has_customers(self) -> bool:
        return self.customers is not None and not self.customers.empty

    @property
    def has_events(self) -> bool:
        return self.events is not None and not self.events.empty


def find_db(explicit: Optional[str]) -> Optional[str]:
    candidates = [explicit, os.environ.get("MESHO_DB")] + DB_CANDIDATES
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in candidates:
        if not cand:
            continue
        for path in (cand, os.path.join(here, cand), os.path.join(here, "..", cand)):
            if os.path.isfile(path):
                return os.path.abspath(path)
    return None


def _read_table(conn: sqlite3.Connection, table: str) -> Optional[pd.DataFrame]:
    try:
        return pd.read_sql_query(f'SELECT * FROM "{table}"', conn)
    except (pd.errors.DatabaseError, sqlite3.Error):
        return None


def _normalise_sales(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce types, standardise status text and derive helper columns."""
    df = df.copy()
    for col in ("Revenue", "Supplier_Price", "Base_Price", "Discount_%", "Units_Sold"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "Status" in df.columns:
        df["Status"] = df["Status"].astype(str).str.strip().str.upper()
    if "Order_Date" in df.columns:
        df["Order_Date"] = pd.to_datetime(df["Order_Date"], errors="coerce")

    if "Payment Class" not in df.columns and "Payment_Method" in df.columns:
        is_cod = df["Payment_Method"].astype(str).str.contains("COD|Cash on", case=False, regex=True)
        df["Payment Class"] = is_cod.map({True: "COD", False: "Prepaid"})

    needed = {"Base_Price", "Discount_%", "Supplier_Price", "Units_Sold"}
    if needed.issubset(df.columns):
        net_price = df["Base_Price"] * (1 - df["Discount_%"] / 100.0)
        df["Gross_Profit"] = (net_price - df["Supplier_Price"]) * df["Units_Sold"]
    return df


def load_data(db_path: Optional[str], csv_dir: str) -> DataStore:
    store = DataStore()

    path = find_db(db_path)
    if path:
        try:
            conn = sqlite3.connect(path)
            store.sales = _read_table(conn, SALES_TABLE)
            store.customers = _read_table(conn, CUSTOMER_TABLE)
            store.events = _read_table(conn, EVENTS_TABLE)
            conn.close()
            store.source = f"SQLite: {path}"
        except sqlite3.Error as exc:
            store.notes.append(f"Could not open SQLite database ({exc}).")

    if not store.has_sales:
        csv_map = {
            "sales": f"{SALES_TABLE}.csv",
            "customers": f"{CUSTOMER_TABLE}.csv",
            "events": f"{EVENTS_TABLE}.csv",
        }
        loaded_any = False
        for attr, fname in csv_map.items():
            fpath = os.path.join(csv_dir, fname)
            if os.path.isfile(fpath):
                try:
                    setattr(store, attr, pd.read_csv(fpath))
                    loaded_any = True
                except (OSError, pd.errors.ParserError) as exc:
                    store.notes.append(f"Could not read {fpath}: {exc}")
        if loaded_any:
            store.source = f"CSV folder: {os.path.abspath(csv_dir)}"

    if store.has_sales:
        store.sales = _normalise_sales(store.sales)
        missing = {"Revenue", "Status"} - set(store.sales.columns)
        if missing:
            store.notes.append(f"Sales table is missing column(s): {', '.join(sorted(missing))}.")
            store.sales = None
    if store.source == "none":
        store.source = "documented project baseline (no database found)"
    return store


# --------------------------------------------------------------------------- #
# Answer container
# --------------------------------------------------------------------------- #
@dataclass
class Answer:
    title: str
    findings: List[str]
    context: str
    recommendation: str
    basis: str = "live data"

    def render(self) -> str:
        out = [banner(self.title)]
        out.append("FINDINGS")
        out.extend(f"  - {line}" for line in self.findings)
        out.append("\nBUSINESS CONTEXT")
        out.append(f"  {self.context}")
        out.append("\nRECOMMENDATION")
        out.append(f"  {self.recommendation}")
        out.append(f"\n[basis: {self.basis}]")
        return "\n".join(out)


# --------------------------------------------------------------------------- #
# Intent handlers
# --------------------------------------------------------------------------- #
class Analyst:
    def __init__(self, store: DataStore):
        self.s = store

    # ---- 1. Revenue ------------------------------------------------------- #
    def revenue(self) -> Answer:
        if self.s.has_sales:
            df = self.s.sales
            gross = df["Revenue"].sum()
            delivered = df.loc[df["Status"] == DELIVERED, "Revenue"].sum()
            basis = "live data"
        else:
            gross, delivered = BASELINE["gross_revenue"], BASELINE["delivered_revenue"]
            basis = "documented baseline"
        realised = safe_div(delivered, gross)
        gap = gross - delivered
        return Answer(
            "Total Gross & Delivered Revenue",
            [
                f"Gross revenue (all order statuses): {inr(gross)}",
                f"Delivered revenue (cash actually realised): {inr(delivered)}",
                f"Realisation rate: {pct(realised * 100 if realised is not None else None)}",
                f"Revenue not realised (RTO, cancelled, in transit): {inr(gap)}",
            ],
            "Gross revenue counts every order placed. Only delivered orders convert to "
            "cash, so the gap is the pool of revenue the operations team can still influence.",
            "Report delivered revenue to leadership as the headline number and track the "
            "realisation rate monthly; each 1 pt gain is worth about "
            f"{inr(gross * 0.01)} at current volume.",
            basis,
        )

    # ---- 2. RTO ----------------------------------------------------------- #
    def rto(self) -> Answer:
        if self.s.has_sales:
            df = self.s.sales
            gross = df["Revenue"].sum()
            rto_rev = df.loc[df["Status"] == RTO, "Revenue"].sum()
            rate = safe_div(rto_rev, gross)
            rate_pct = rate * 100 if rate is not None else None
            findings = [
                f"RTO rate (RTO revenue / gross revenue): {pct(rate_pct)}",
                f"Revenue lost to RTO: {inr(rto_rev)}",
            ]
            if "Order_ID" in df.columns:
                total_orders = df["Order_ID"].nunique()
                rto_orders = df.loc[df["Status"] == RTO, "Order_ID"].nunique()
                findings.append(
                    f"RTO orders: {rto_orders:,} of {total_orders:,} "
                    f"({pct(safe_div(rto_orders, total_orders) * 100 if total_orders else None)})"
                )
            observed_cod_share = None
            if "Payment Class" in df.columns and rto_rev:
                cod_rto = df.loc[(df["Status"] == RTO) & (df["Payment Class"] == "COD"), "Revenue"].sum()
                observed_cod_share = safe_div(cod_rto, rto_rev)
                findings.append(
                    f"COD share of RTO revenue in this dataset: {pct(observed_cod_share * 100)} "
                    f"({inr(cod_rto)})"
                )
                if "Order_ID" in df.columns:
                    for klass in ("COD", "Prepaid"):
                        sub = df[df["Payment Class"] == klass]
                        o = sub["Order_ID"].nunique()
                        r = sub.loc[sub["Status"] == RTO, "Order_ID"].nunique()
                        findings.append(f"{klass} order RTO rate: {pct(safe_div(r, o) * 100 if o else None)}")
            basis = "live data"
        else:
            rate_pct, rto_rev = BASELINE["rto_rate_pct"], BASELINE["rto_revenue"]
            findings = [
                f"RTO rate: {pct(rate_pct)}",
                f"Revenue tied up in RTO: {inr(rto_rev)}",
                f"COD share of RTO volume (documented): {pct(BASELINE['cod_share_of_rto_pct'], 0)}",
            ]
            basis = "documented baseline"
        return Answer(
            "RTO Rate & Lost Revenue",
            findings,
            "Return-to-Origin means the parcel was shipped and came back undelivered. The "
            "business pays forward and reverse freight and receives no revenue.",
            "Verify risky orders before dispatch (WhatsApp confirmation, address quality "
            "scoring) and nudge COD buyers to prepay. Compare COD and Prepaid RTO rates above "
            "to decide how much of the problem is payment-mode driven.",
            basis,
        )

    # ---- 3. Category profitability --------------------------------------- #
    def categories(self) -> Answer:
        if not (self.s.has_sales and "Gross_Profit" in self.s.sales.columns and "Product_Category" in self.s.sales.columns):
            return self._unavailable(
                "Category Profitability",
                "Needs Product_Category, Base_Price, Discount_%, Supplier_Price and Units_Sold in the sales table.",
            )
        df = self.s.sales
        g = (
            df.groupby("Product_Category", dropna=False)
            .agg(revenue=("Revenue", "sum"), profit=("Gross_Profit", "sum"))
            .reset_index()
        )
        g["margin_pct"] = g.apply(lambda r: (safe_div(r["profit"], r["revenue"]) or 0) * 100, axis=1)
        g = g.sort_values("profit", ascending=False)
        top, bottom = g.head(3), g.tail(3).iloc[::-1]
        fmt = lambda r: f"{r['Product_Category']}: profit {inr(r['profit'])} on revenue {inr(r['revenue'])} ({pct(r['margin_pct'], 1)} margin)"
        findings = ["Top 3 categories by gross profit:"] + [f"    {fmt(r)}" for _, r in top.iterrows()]
        findings += ["Bottom 3 categories by gross profit:"] + [f"    {fmt(r)}" for _, r in bottom.iterrows()]
        losers = g[g["profit"] < 0]
        if not losers.empty:
            findings.append(f"Loss-making categories: {', '.join(losers['Product_Category'].astype(str))}")
        return Answer(
            "Top & Bottom Categories by Profitability",
            findings,
            "Profit = (Base price x (1 - discount) - supplier price) x units, on all order "
            "statuses, matching the Power BI 'Gross Profit or Loss' measure.",
            "Shift ad and discount budget toward the top margin categories, and review "
            "discount depth and supplier cost in the bottom categories before scaling them.",
        )

    # ---- 4. Segments ------------------------------------------------------ #
    def segments(self) -> Answer:
        if not (self.s.has_customers and "RFM Segment" in self.s.customers.columns):
            return self._unavailable("Customer Segments", "Needs the customers table with an 'RFM Segment' column.")
        c = self.s.customers
        counts = c["RFM Segment"].value_counts()
        buyers = counts.drop(labels=["Never Purchased"], errors="ignore")
        total_buyers = int(buyers.sum())
        champions = int(counts.get("Champions", 0))
        at_risk_total = int(sum(counts.get(seg, 0) for seg in AT_RISK_SEGMENTS))
        findings = [f"Purchasing customers: {total_buyers:,} (of {len(c):,} in the base)"]
        findings += [
            f"{seg}: {int(n):,} ({pct(safe_div(n, total_buyers) * 100 if total_buyers else None, 1)})"
            for seg, n in buyers.items()
        ]
        findings.append(f"Champions vs At-Risk group ({', '.join(AT_RISK_SEGMENTS)}): {champions:,} vs {at_risk_total:,}")
        return Answer(
            "Customer Segment Distribution (Champions vs At Risk)",
            findings,
            "Segments come from RFM scoring (recency, frequency, monetary value). Champions "
            "are the most valuable repeat buyers; the At-Risk group has been buying less recently.",
            "Protect Champions with early access and loyalty perks, and run a win-back offer "
            "on the 'At Risk' and 'Cannot Lose Them' groups before they lapse.",
        )

    # ---- 5. Cart abandonment --------------------------------------------- #
    def cart_abandonment(self) -> Answer:
        observed = None
        if self.s.has_events and {"Feature_Name", "Session_ID"}.issubset(self.s.events.columns):
            ev = self.s.events
            cart = ev.loc[ev["Feature_Name"] == "Wishlist_Add", "Session_ID"].nunique()
            chk = ev.loc[ev["Feature_Name"] == "Checkout_Click", "Session_ID"].nunique()
            if cart > 0 and chk < cart:
                observed = (cart, chk)

        funnel = dict(BASELINE["funnel"])
        cart_b, chk_b = funnel["Wishlist / Cart"], funnel["Checkout Click"]
        base_drop = (1 - chk_b / cart_b) * 100

        findings: List[str] = []
        if observed:
            cart, chk = observed
            findings.append(
                f"Cart-to-checkout abandonment from the event log: {pct((1 - chk / cart) * 100)} "
                f"({cart:,} cart sessions -> {chk:,} checkout sessions)"
            )
            basis = "live data"
        else:
            findings.append(
                "The event log cannot support a sequential cart-to-checkout rate "
                "(checkout sessions are not fewer than cart sessions), so the documented funnel is used."
            )
            basis = "documented baseline"
        findings.append(
            f"Documented funnel baseline: cart {cart_b:,} -> checkout {chk_b:,} = {pct(base_drop, 1)} abandonment"
        )
        steps = list(BASELINE["funnel"])
        for (a, na), (b, nb) in zip(steps, steps[1:]):
            findings.append(f"    {a} -> {b}: {pct((1 - nb / na) * 100, 1)} drop-off")
        return Answer(
            "Cart Abandonment Rate",
            findings,
            "The cart-to-checkout step is the largest controllable leak; unexpected shipping "
            "and handling fees shown late on low-margin baskets are the leading suspected cause.",
            "Show an upfront delivery estimate and fee on the product and cart pages, and A/B "
            "test free-shipping thresholds. Target a drop from 42% to about 32%.",
            basis,
        )

    # ---- helpers ---------------------------------------------------------- #
    @staticmethod
    def _unavailable(title: str, why: str) -> Answer:
        return Answer(
            title,
            ["This question cannot be answered from the data currently loaded."],
            why,
            "Point the assistant at the full database with --db, then ask again.",
            "no data",
        )

    def overview(self) -> Answer:
        parts = [self.revenue(), self.rto()]
        findings = [f"{p.title}: {p.findings[0]}" for p in parts]
        return Answer(
            "Executive Snapshot",
            findings,
            "A one-glance summary. Ask about any item for the full breakdown.",
            "Next best questions: 'cart abandonment', 'top categories', 'customer segments'.",
            parts[0].basis,
        )


# --------------------------------------------------------------------------- #
# Intent matching
# --------------------------------------------------------------------------- #
INTENTS: List[Tuple[str, List[str], str]] = [
    # (intent name, keyword/regex patterns, handler method)
    ("rto", [r"\brto\b", r"return.to.origin", r"\breturns?\b", r"undelivered", r"lost revenue"], "rto"),
    ("cart", [r"cart", r"abandon", r"checkout", r"funnel", r"drop.?off"], "cart_abandonment"),
    ("categories", [r"categor", r"profitab", r"margin", r"profit"], "categories"),
    ("segments", [r"segment", r"champion", r"at.?risk", r"rfm", r"loyal", r"customer"], "segments"),
    ("revenue", [r"revenue", r"sales", r"gross", r"delivered", r"turnover", r"income"], "revenue"),
    ("overview", [r"overview", r"summary", r"snapshot", r"kpi", r"dashboard"], "overview"),
]

HELP_TEXT = """
I can answer questions such as:
  - What is our total gross and delivered revenue?
  - What is the RTO rate and how much revenue do we lose?
  - Which categories are the most and least profitable?
  - How many Champions vs At Risk customers do we have?
  - What is our cart abandonment rate?
  - Give me an executive snapshot
Type 'help' to see this again, 'quit' to exit.
"""


def match_intent(question: str) -> Optional[str]:
    """Score each intent by pattern hits; earliest listed intent wins ties."""
    q = question.lower()
    best, best_score = None, 0
    for _, patterns, handler in INTENTS:
        score = sum(1 for p in patterns if re.search(p, q))
        if score > best_score:
            best, best_score = handler, score
    return best


def answer_question(analyst: Analyst, question: str) -> str:
    handler_name = match_intent(question)
    if handler_name is None:
        return (
            "\nI did not recognise that question. I currently cover revenue, RTO, category "
            "profitability, customer segments and cart abandonment."
            "\nTry: 'What is the RTO rate?' or type 'help'."
        )
    try:
        return getattr(analyst, handler_name)().render()
    except Exception as exc:  # defensive: never crash the interactive loop
        return f"\nSomething went wrong while answering ({type(exc).__name__}: {exc}). Please try another question."


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def interactive_loop(analyst: Analyst) -> None:
    print(banner("Meesho Growth Intelligence - AI Business Analyst"))
    print(f"Data source: {analyst.s.source}")
    for note in analyst.s.notes:
        print(f"Note: {note}")
    print(HELP_TEXT)
    while True:
        try:
            question = input("Ask> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return
        if not question:
            continue
        if question.lower() in {"quit", "exit", "q", "bye"}:
            print("Goodbye.")
            return
        if question.lower() in {"help", "?", "h"}:
            print(HELP_TEXT)
            continue
        print(answer_question(analyst, question))


def selftest(analyst: Analyst) -> int:
    questions = [
        "What is our total revenue?",
        "What is the RTO rate?",
        "Top and bottom categories by profit",
        "Champions vs at risk customers",
        "What is the cart abandonment rate?",
        "Give me a summary",
    ]
    failures = 0
    for q in questions:
        out = answer_question(analyst, q)
        ok = "went wrong" not in out and "did not recognise" not in out
        print(f"[{'PASS' if ok else 'FAIL'}] {q}")
        failures += 0 if ok else 1
    return failures


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Meesho AI Business Analyst Assistant")
    parser.add_argument("--db", help="Path to the SQLite database (meesho_growth_db)")
    parser.add_argument("--csv-dir", default="data", help="Folder with CSV exports (fallback)")
    parser.add_argument("-q", "--question", help="Ask one question and exit")
    parser.add_argument("--selftest", action="store_true", help="Run every intent once and exit")
    args = parser.parse_args(argv)

    analyst = Analyst(load_data(args.db, args.csv_dir))

    if args.selftest:
        return 1 if selftest(analyst) else 0
    if args.question:
        print(answer_question(analyst, args.question))
        return 0
    interactive_loop(analyst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
