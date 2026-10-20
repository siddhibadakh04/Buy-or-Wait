"""PostgreSQL storage for authenticated, multi-user deployments.

Every operation opens its own TLS connection, sets the verified OIDC subject as
the transaction tenant, and relies on PostgreSQL RLS policies as a second
authorization boundary. The database URL must use a restricted app role, never
the database owner or a service-role credential.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import math
from typing import Any, Iterator

import pandas as pd
import psycopg
from psycopg.rows import dict_row


def _connection():
    import streamlit as st

    url = st.secrets["database"]["url"]
    conn = psycopg.connect(url, sslmode="require", row_factory=dict_row, prepare_threshold=None)
    role = conn.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    if not role or role["rolsuper"] or role["rolbypassrls"]:
        conn.close()
        raise RuntimeError("Configure a non-superuser database role without BYPASSRLS.")
    return conn


@contextmanager
def _tenant_connection(user_id: str) -> Iterator[psycopg.Connection]:
    if not user_id or len(user_id) > 512:
        raise ValueError("A verified account identifier is required.")
    with _connection() as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
            yield conn


def load_profile(user_id: str) -> tuple[dict[str, Any] | None, pd.DataFrame]:
    with _tenant_connection(user_id) as conn:
        profile = conn.execute(
            "SELECT currency, balance, reserve, updated_at FROM public.user_profiles WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        flow_rows = conn.execute(
            "SELECT label, direction, amount, frequency, next_date "
            "FROM public.user_cashflows WHERE user_id = %s ORDER BY id",
            (user_id,),
        ).fetchall()
    flows = pd.DataFrame(flow_rows, columns=["label", "direction", "amount", "frequency", "next_date"])
    if not flows.empty:
        flows["next_date"] = pd.to_datetime(flows["next_date"], errors="coerce").dt.date
    return (dict(profile) if profile else None), flows


def save_profile(user_id: str, currency: str, balance: float, reserve: float, flows: pd.DataFrame) -> None:
    if (not currency or len(currency) != 3 or not math.isfinite(float(balance))
            or not math.isfinite(float(reserve)) or balance < 0 or reserve < 0 or reserve > balance):
        raise ValueError("Check the currency, available balance, and protected reserve.")
    clean = []
    for _, row in flows.iterrows():
        label_value, amount_value = row.get("label"), row.get("amount")
        label_text = "" if pd.isna(label_value) else str(label_value).strip()
        if not label_text and (pd.isna(amount_value) or float(amount_value) == 0):
            continue
        if pd.isna(amount_value):
            raise ValueError("Each cash-flow row needs a positive amount.")
        amount = float(amount_value)
        direction = str(row.get("direction", "")).strip().lower()
        frequency = str(row.get("frequency", "")).strip().lower()
        next_date = pd.to_datetime(row.get("next_date"), errors="coerce")
        if (not label_text or len(label_text) > 120 or direction not in {"income", "expense"}
                or not math.isfinite(amount)
                or amount <= 0 or amount > 1_000_000_000 or frequency not in {"once", "weekly", "biweekly", "monthly", "quarterly"}
                or pd.isna(next_date)):
            raise ValueError("Each row needs a description, type, valid positive amount, repeat interval, and date.")
        clean.append((label_text, direction, amount, frequency, next_date.date()))

    with _tenant_connection(user_id) as conn:
        conn.execute(
            "INSERT INTO public.user_profiles(user_id,currency,balance,reserve,updated_at) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET "
            "currency=EXCLUDED.currency,balance=EXCLUDED.balance,reserve=EXCLUDED.reserve,updated_at=EXCLUDED.updated_at",
            (user_id, currency, float(balance), float(reserve), date.today()),
        )
        conn.execute("DELETE FROM public.user_cashflows WHERE user_id=%s", (user_id,))
        conn.executemany(
            "INSERT INTO public.user_cashflows(user_id,label,direction,amount,frequency,next_date) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            [(user_id, *flow) for flow in clean],
        )


def clear_profile(user_id: str) -> None:
    with _tenant_connection(user_id) as conn:
        conn.execute("DELETE FROM public.user_cashflows WHERE user_id=%s", (user_id,))
        conn.execute("DELETE FROM public.user_profiles WHERE user_id=%s", (user_id,))
