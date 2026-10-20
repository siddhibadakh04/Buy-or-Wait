"""Local, single-device finance profile and 90-day affordability forecast.

This module supports a product demo only. It stores one local profile in
SQLite; it does not provide hosted accounts, encryption, or bank connections.
"""
from __future__ import annotations

import calendar
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

DB_PATH = Path(__file__).with_name("local_finances.sqlite3")
FREQUENCIES = ("once", "weekly", "biweekly", "monthly", "quarterly")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS local_profile ("
        "id INTEGER PRIMARY KEY CHECK(id=1), currency TEXT NOT NULL, "
        "balance REAL NOT NULL, reserve REAL NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS local_cashflows ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, "
        "direction TEXT NOT NULL, amount REAL NOT NULL, frequency TEXT NOT NULL, "
        "next_date TEXT NOT NULL)"
    )
    return conn


def load_profile() -> tuple[dict[str, Any] | None, pd.DataFrame]:
    with _connect() as conn:
        profile_row = conn.execute("SELECT * FROM local_profile WHERE id=1").fetchone()
        flows = pd.read_sql_query(
            "SELECT label, direction, amount, frequency, next_date FROM local_cashflows ORDER BY id",
            conn,
        )
    profile = dict(profile_row) if profile_row else None
    return profile, flows


def save_profile(currency: str, balance: float, reserve: float, flows: pd.DataFrame) -> None:
    clean = []
    for _, row in flows.iterrows():
        raw_label = row.get("label")
        raw_amount = row.get("amount")
        if (pd.isna(raw_label) or not str(raw_label).strip()) and (pd.isna(raw_amount) or float(raw_amount) == 0):
            continue
        label = str(raw_label if not pd.isna(raw_label) else "").strip()
        direction = str(row.get("direction", "expense")).strip().lower()
        amount = float(raw_amount)
        frequency = str(row.get("frequency", "once")).strip().lower()
        next_date = pd.to_datetime(row.get("next_date"), errors="raise").date()
        if not label or direction not in {"income", "expense"}:
            raise ValueError("Each cash-flow row needs a description and income/expense direction.")
        if amount <= 0 or frequency not in FREQUENCIES:
            raise ValueError("Cash-flow amounts must be positive and frequency must be supported.")
        clean.append((label, direction, amount, frequency, next_date.isoformat()))

    with _connect() as conn:
        conn.execute(
            "INSERT INTO local_profile(id,currency,balance,reserve,updated_at) VALUES(1,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET currency=excluded.currency,balance=excluded.balance,"
            "reserve=excluded.reserve,updated_at=excluded.updated_at",
            (currency, float(balance), float(reserve), date.today().isoformat()),
        )
        conn.execute("DELETE FROM local_cashflows")
        conn.executemany(
            "INSERT INTO local_cashflows(label,direction,amount,frequency,next_date) VALUES(?,?,?,?,?)",
            clean,
        )


def clear_profile() -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM local_cashflows")
        conn.execute("DELETE FROM local_profile")


def _next_month(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _expand_flows(flows: pd.DataFrame, start: date, end: date) -> dict[date, float]:
    daily: dict[date, float] = {}
    for _, row in flows.iterrows():
        first = pd.to_datetime(row["next_date"]).date()
        frequency = str(row["frequency"])
        direction = str(row["direction"]).lower()
        sign = 1 if direction == "income" else -1
        amount = float(row["amount"])
        occurrence = first
        while occurrence < start:
            if frequency == "once":
                break
            if frequency == "weekly":
                occurrence += timedelta(days=7)
            elif frequency == "biweekly":
                occurrence += timedelta(days=14)
            elif frequency == "monthly":
                occurrence = _next_month(occurrence, 1)
            else:
                occurrence = _next_month(occurrence, 3)
        while start <= occurrence <= end:
            daily[occurrence] = daily.get(occurrence, 0.0) + sign * amount
            if frequency == "once":
                break
            if frequency == "weekly":
                occurrence += timedelta(days=7)
            elif frequency == "biweekly":
                occurrence += timedelta(days=14)
            elif frequency == "monthly":
                occurrence = _next_month(occurrence, 1)
            else:
                occurrence = _next_month(occurrence, 3)
    return daily


def assess_purchase(
    balance: float,
    reserve: float,
    flows: pd.DataFrame,
    amount: float,
    start: date,
    deadline: date,
    horizon_days: int = 90,
) -> dict[str, Any]:
    end = start + timedelta(days=horizon_days)
    daily = _expand_flows(flows, start, end)

    def safe_with_payments(payments: list[tuple[date, float]]) -> bool:
        payment_map: dict[date, float] = {}
        for payment_date, payment_amount in payments:
            payment_map[payment_date] = payment_map.get(payment_date, 0.0) + payment_amount
        cash = balance
        for day_offset in range(horizon_days + 1):
            day = start + timedelta(days=day_offset)
            cash += daily.get(day, 0.0)
            cash -= payment_map.get(day, 0.0)
            if cash < reserve - 1e-7:
                return False
        return True

    cash = balance
    max_today = max(0.0, balance - reserve)
    for day_offset in range(horizon_days + 1):
        day = start + timedelta(days=day_offset)
        cash += daily.get(day, 0.0)
        max_today = min(max_today, cash - reserve)
    safe_today = round(max(0.0, min(amount, max_today)), 2)

    earliest = None
    for day_offset in range(horizon_days + 1):
        pay_date = start + timedelta(days=day_offset)
        if safe_with_payments([(pay_date, amount)]):
            earliest = pay_date
            break

    if safe_today + 1e-7 >= amount:
        method, status = "Pay in full today", "Affordable now"
        plan = [(start, amount)]
    elif earliest and earliest <= deadline:
        method, status = "Wait", "Affordable later"
        plan = [(earliest, amount)]
    else:
        method, status = "Do not proceed yet", "Not affordable by your date"
        plan = []

    return {
        "safe_today": safe_today,
        "earliest_full_date": earliest,
        "method": method,
        "status": status,
        "plan": plan,
        "horizon_end": end,
        "safe_now": safe_with_payments([(start, amount)]),
    }
