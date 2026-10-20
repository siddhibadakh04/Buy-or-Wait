"""Deterministic, auditable 90-day affordability baseline.

The input data is intentionally mapped by column aliases because participant
datasets may use small naming variations. Unknown records are handled
conservatively and the app surfaces schema warnings before a run.
"""
from __future__ import annotations

import calendar
import math
import itertools
import re
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

REQUIRED_INPUTS = ["requests.csv", "financial_profiles.csv", "financial_events.csv",
                   "request_payment_options.csv", "exchange_rates.csv", "messages.csv", "images.csv"]
OUTPUT_COLUMNS = ["request_id", "amount_safe_to_pay", "affordability_status",
                  "recommended_payment_method", "payment_plan", "earliest_date_for_full_payment",
                  "spending_changes_needed", "decision_explanation"]

ALIASES = {
    "request_id": ["request_id"], "user_id": ["user_id"], "date": ["request_date", "date", "event_date", "transaction_date", "payment_date"],
    "linked_event_id": ["linked_event_id"], "settlement_date": ["settlement_date"],
    "amount": ["requested_amount", "amount", "event_amount", "transaction_amount", "balance", "available_balance", "current_available_balance"],
    "currency": ["currency", "home_currency", "event_currency"], "status": ["status", "event_status", "transaction_status"],
    "direction": ["direction"], "category": ["category"],
    "kind": ["event_type", "transaction_type", "category", "type"], "name": ["event_name", "description", "name", "merchant"],
    "recurring": ["is_recurring", "recurring", "recurrence"], "flexible": ["is_flexible", "flexible", "flexibility"],
    "option_id": ["payment_option_id"], "method": ["payment_method", "method", "option_type", "payment_type"],
    "count": ["number_of_payments", "installment_count", "payment_count", "number_of_installments"],
    "interval": ["days_between_payments", "payment_interval_days", "interval_days", "payment_frequency_days"],
    "first_date": ["first_payment_date", "payment_start_date", "first_payment_on"],
    "total": ["total_payable_amount", "total_amount", "total_payable"], "payment_amount": ["payment_amount", "installment_amount"],
    "fee": ["financing_fee", "fee", "interest_amount"],
    "min_balance": ["minimum_balance_to_keep", "minimum_balance", "min_balance"],
    "max_installments": ["max_installment_months", "maximum_installment_months"],
    "prefs": ["payment_methods_user_will_consider", "payment_methods", "payment_preferences"],
    "reduce_categories": ["expense_categories_user_is_willing_to_reduce"],
    "stop_categories": ["expense_categories_user_is_willing_to_stop"],
    "protect_categories": ["expense_categories_to_protect"],
    "event_id": ["event_id"],
}

def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).strip().lower()).strip("_")

def _col(df: pd.DataFrame, key: str) -> str | None:
    aliases = {_norm(a) for a in ALIASES.get(key, [key])}
    return next((c for c in df.columns if _norm(c) in aliases), None)

def _get(row: Any, df: pd.DataFrame, key: str, default=None):
    c = _col(df, key)
    if c is None: return default
    v = row.get(c, default)
    return default if pd.isna(v) else v

def _date(v: Any) -> date | None:
    if v is None or pd.isna(v) or str(v).strip() == "": return None
    try: return pd.to_datetime(v).date()
    except Exception: return None

def _num(v: Any, default=0.0) -> float:
    try:
        if v is None or pd.isna(v): return default
        return float(str(v).replace(",", "").replace(" ", ""))
    except (TypeError, ValueError): return default

def _truth(v: Any) -> bool:
    return str(v).strip().lower() in {"true", "yes", "1", "y", "flexible", "recurring"}

def _fmt(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")

def _currency_rate(rates: pd.DataFrame, source: str, target: str, on: date) -> float | None:
    if source == target: return 1.0
    src=_col(rates,"source_currency") or next((c for c in rates if _norm(c) in {"from_currency","base_currency","currency_from"}),None)
    dst=_col(rates,"target_currency") or next((c for c in rates if _norm(c) in {"to_currency","quote_currency","currency_to"}),None)
    rat=next((c for c in rates if _norm(c) in {"rate","exchange_rate","conversion_rate"}),None)
    dt=next((c for c in rates if _norm(c) in {"rate_date","date","effective_date"}),None)
    if not all((src,dst,rat)): return None
    work=rates.copy()
    if dt:
        work["_rate_date"]=pd.to_datetime(work[dt],errors="coerce").dt.date
        work=work[work["_rate_date"]<=on]
        work=work.sort_values("_rate_date").drop_duplicates([src,dst],keep="last")
    graph: dict[str,list[tuple[str,float]]]={}
    for _,r in work.iterrows():
        a=str(r[src]).upper(); b=str(r[dst]).upper(); value=_num(r[rat],None)
        if not a or not b or not value or value<=0: continue
        graph.setdefault(a,[]).append((b,value)); graph.setdefault(b,[]).append((a,1/value))
    queue=[(source.upper(),1.0)]; seen={source.upper()}
    while queue:
        cur,mult=queue.pop(0)
        for nxt,edge in graph.get(cur,[]):
            if nxt in seen: continue
            if nxt==target.upper(): return mult*edge
            seen.add(nxt); queue.append((nxt,mult*edge))
    return None

def inspect_inputs(tables: dict[str, pd.DataFrame]) -> list[dict[str,str]]:
    out=[]
    for filename in REQUIRED_INPUTS:
        if filename not in tables: continue
        df=tables[filename]
        if df.empty: out.append({"level":"error","message":f"{filename} has no rows."}); continue
        if filename=="requests.csv":
            for field in ("request_id","user_id","date","amount"):
                if _col(df,field) is None: out.append({"level":"error","message":f"requests.csv is missing a recognizable {field} column."})
        if filename=="financial_profiles.csv":
            for field in ("user_id","amount","min_balance","prefs"):
                if _col(df,field) is None: out.append({"level":"error","message":f"financial_profiles.csv is missing a recognizable {field} column."})
        if filename=="requests.csv":
            for field in ("desired_completion_date","allows_partial_payment"):
                if not any(_norm(c)==field for c in df.columns): out.append({"level":"error","message":f"requests.csv is missing required {field}."})
        if filename=="financial_events.csv":
            for field in ("user_id","event_id","date","amount","status","kind","direction","currency","settlement_date","linked_event_id","flexible"):
                if _col(df,field) is None: out.append({"level":"error","message":f"financial_events.csv is missing a recognizable {field} column."})
        if filename=="request_payment_options.csv":
            for field in ("request_id","option_id","method","payment_amount","count","first_date","interval","fee","total"):
                if _col(df,field) is None: out.append({"level":"error","message":f"request_payment_options.csv is missing a recognizable {field} column."})
        if filename=="exchange_rates.csv":
            rate_fields={"rate_date":{"rate_date","date","effective_date"},"from_currency":{"from_currency","source_currency","base_currency"},"to_currency":{"to_currency","target_currency","quote_currency"},"rate":{"rate","exchange_rate","conversion_rate"}}
            for field,aliases in rate_fields.items():
                if not any(_norm(c) in aliases for c in df.columns): out.append({"level":"error","message":f"exchange_rates.csv is missing a recognizable {field} column."})
        if filename=="messages.csv":
            for field in ("user_id","related_event_id","source_type","sent_at","message_text"):
                if not any(_norm(c)==field for c in df.columns): out.append({"level":"error","message":f"messages.csv is missing required {field}."})
        if filename=="images.csv":
            for field in ("image_id","related_event_id"):
                if not any(_norm(c)==field for c in df.columns): out.append({"level":"error","message":f"images.csv is missing required {field}."})
    if not out: out.append({"level":"success","message":"Core files are present. Basic required columns look usable."})
    return out

def missing_amount_events_for_forecast(tables: dict[str,pd.DataFrame]) -> pd.DataFrame:
    """Blank event amounts that can affect a supplied request's 90-day forecast."""
    events=tables.get("financial_events.csv",pd.DataFrame()); req=tables.get("requests.csv",pd.DataFrame())
    amount_col=_col(events,"amount"); event_id_col=_col(events,"event_id"); user_col=_col(events,"user_id")
    req_user=_col(req,"user_id"); req_date=_col(req,"date")
    if not all((amount_col,event_id_col,user_col,req_user,req_date)): return events.iloc[0:0]
    blank=events[events[amount_col].isna() | (events[amount_col].astype(str).str.strip()=="")]
    request_dates={str(r[req_user]):_date(r[req_date]) for _,r in req.iterrows()}
    date_col=_col(events,"date"); settle_col=_col(events,"settlement_date"); status_col=_col(events,"status")
    needed=[]
    for _,r in blank.iterrows():
        start=request_dates.get(str(r[user_col]))
        if not start: continue
        status=str(r.get(status_col,"settled")).lower() if status_col else "settled"
        event_date=_date(r.get(date_col)) if date_col else None
        settle=_date(r.get(settle_col)) if settle_col else None
        effective=settle if status in {"pending","scheduled"} and settle else event_date
        if effective and start<=effective<=start+timedelta(days=90) and status not in {"cancelled","failed","unrealized"}:
            needed.append(r)
    return pd.DataFrame(needed,columns=events.columns)

def _event_flows(events: pd.DataFrame, user_id: Any, start: date, end: date,
                 home: str, rates: pd.DataFrame, amount_overrides: dict[str,float]|None=None,
                 messages: pd.DataFrame|None=None) -> list[tuple[date,float,str]]:
    uc=_col(events,"user_id"); dc=_col(events,"date")
    if not uc or not dc: return []
    rows=events[events[uc].astype(str)==str(user_id)]
    flows=[]
    linked=_col(events,"linked_event_id"); event_id_col=_col(events,"event_id")
    settled_links=set()
    if linked:
        for _,r in rows.iterrows():
            link=str(r.get(linked,"")); status=str(_get(r,events,"status","")).lower()
            if link and status=="settled":
                prior=rows[rows[event_id_col].astype(str)==link] if event_id_col else rows.iloc[0:0]
                if not prior.empty:
                    prev=prior.iloc[-1]
                    same_direction=str(_get(prev,events,"direction","")).lower()==str(_get(r,events,"direction","")).lower()
                    same_kind=str(_get(prev,events,"kind","")).lower()==str(_get(r,events,"kind","")).lower()
                    same_amount=abs(_num(_get(prev,events,"amount",0))- _num(_get(r,events,"amount",0)))<0.02
                    if same_direction and (same_kind or same_amount): settled_links.add(link)
    cancelled_by_message=set()
    if messages is not None and not messages.empty:
        mu=_col(messages,"user_id"); me=next((c for c in messages if _norm(c)=="related_event_id"),None)
        mt=next((c for c in messages if _norm(c)=="message_text"),None)
        ms=next((c for c in messages if _norm(c)=="sent_at"),None)
        if mu and me and mt:
            user_messages=messages[messages[mu].astype(str)==str(user_id)]
            for _,m in user_messages.iterrows():
                sent=_date(m.get(ms)) if ms else None
                text=str(m.get(mt,"" )).lower()
                if sent and sent>start: continue
                # Only explicit event-linked cancellation statements amend an event.
                if re.search(r"\b(cancelled|canceled|voided|withdrawn|no longer due|payment reversed)\b",text):
                    eid=str(m.get(me,""))
                    if eid: cancelled_by_message.add(eid)
    hist_groups: dict[tuple[str,str,str], list[tuple[date,float,str,float,str]]] = {}
    future_keys={}
    for _,r in rows.iterrows():
        status=str(_get(r,events,"status","")).lower()
        if any(x in status for x in ("cancel","fail","reject","duplicate")): continue
        direction=str(_get(r,events,"direction","")).lower()
        if status=="pending" and direction=="credit": continue
        typ=str(_get(r,events,"kind","")).lower()
        if "investment" in typ and any(x in typ for x in ("value","balance","valuation")): continue
        eid=str(_get(r,events,"event_id",""))
        if eid in settled_links and status!="settled": continue
        if eid in cancelled_by_message: continue
        amt=_num(_get(r,events,"amount",None),None)
        if amt is None and amount_overrides and eid in amount_overrides: amt=amount_overrides[eid]
        if amt is None: continue
        dt_event=_date(r[dc]); dt_settle=_date(_get(r,events,"settlement_date",None))
        dt=dt_settle if status in {"pending","scheduled"} and dt_settle else dt_event
        if dt is None: continue
        curr=str(_get(r,events,"currency",home)).upper()
        raw_amount=amt
        if curr!=home:
            rate=_currency_rate(rates,curr,home,dt)
            if rate is None: continue
            amt*=rate
        sign = -1 if direction=="debit" else (1 if direction=="credit" else (-1 if any(k in typ for k in ("expense","payment","purchase","debit","bill","rent","transfer out")) else 1))
        name=str(_get(r,events,"name",typ or "cash flow")); cat=str(_get(r,events,"category",""))
        # Payroll descriptions often differ between a prorated first salary
        # and the next confirmed salary. Keep those records in one recurrence
        # group by their salary category instead of requiring identical text.
        recurring_name="__salary__" if cat.lower() in {"salary","payroll"} else name
        key=(typ,cat,recurring_name)
        if status in {"pending","scheduled"} and start<=dt<=end:
            flows.append((dt,sign*abs(amt),name)); future_keys.setdefault(key,[]).append((dt,sign*abs(amt),name))
        elif status=="settled":
            if dt_event and dt_event<start:
                hist_groups.setdefault(key,[]).append((dt_event,sign*abs(amt),name,raw_amount,curr))
            elif start<=dt<=end: flows.append((dt,sign*abs(amt),name))
    # Infer a recurrence only from at least three settled observations with a
    # stable cadence. Future scheduled records supersede inferred occurrences.
    for key,hist in hist_groups.items():
        hist.sort(key=lambda x:x[0])
        future=future_keys.get(key,[])
        # A single settled salary plus its explicitly scheduled next payroll is
        # enough evidence for a monthly cadence; other categories still need
        # three settled observations before recurring amounts are projected.
        confirmed_salary=(key[1].lower() in {"salary","payroll"} and bool(future) and bool(hist))
        if len(hist)<3 and not confirmed_salary: continue
        observations=sorted([(x[0],x[1],x[2]) for x in hist]+[(x[0],x[1],x[2]) for x in future],key=lambda x:x[0])
        if len(observations)<2: continue
        gaps=[(observations[i][0]-observations[i-1][0]).days for i in range(1,len(observations))]
        cadence=sorted(gaps)[len(gaps)//2]
        if not 6<=cadence<=45: continue
        if confirmed_salary and len(hist)<3:
            if not 25<=cadence<=35 or any(abs(g-cadence)>max(3,int(cadence*.2)) for g in gaps): continue
        elif sum(abs(g-cadence)<=max(3,int(cadence*.2)) for g in gaps)<max(2,len(gaps)-1): continue
        anchor_date=hist[-1][0]; anchor_amount=hist[-1][1]; anchor_from_history=True
        projection_name=hist[-1][2]
        if future:
            future_anchor=max(future,key=lambda x:x[0])
            if future_anchor[0]>anchor_date: anchor_date=future_anchor[0]; anchor_amount=future_anchor[1]; anchor_from_history=False
            if not anchor_from_history: projection_name=future_anchor[2]
        cursor=anchor_date+timedelta(days=cadence)
        while cursor<start: cursor+=timedelta(days=cadence)
        while cursor<=end:
            projected_amount=anchor_amount
            if anchor_from_history and hist[-1][4]!=home:
                future_rate=_currency_rate(rates,hist[-1][4],home,cursor)
                if future_rate is None: cursor+=timedelta(days=cadence); continue
                projected_amount=math.copysign(abs(hist[-1][3])*future_rate,hist[-1][1])
            if not any(d==cursor and n==projection_name for d,a,n in flows):
                flows.append((cursor,projected_amount,projection_name))
            cursor+=timedelta(days=cadence)
    return flows

def _spending_change_candidates(events: pd.DataFrame, user_id: Any, start: date, end: date,
                                home: str, rates: pd.DataFrame, profile: Any,
                                profiles: pd.DataFrame, flows) -> list[dict[str,Any]]:
    """Offer only user-authorized changes on recurring, explicitly flexible outflows."""
    uc=_col(events,"user_id"); dc=_col(events,"date")
    if not uc or not dc: return []
    rows=events[events[uc].astype(str)==str(user_id)]
    groups={}
    for _,r in rows.iterrows():
        if str(_get(r,events,"status","")).lower()!="settled": continue
        if str(_get(r,events,"direction","")).lower()!="debit": continue
        d=_date(r[dc]); amt=_num(_get(r,events,"amount",None),None)
        if d is None or d>=start or amt is None: continue
        typ=str(_get(r,events,"kind","" )).lower(); cat=str(_get(r,events,"category","")); name=str(_get(r,events,"name",""))
        groups.setdefault((typ,cat,name),[]).append((d,r,amt))
    reduce_cats=set(str(_get(profile,profiles,"reduce_categories","")).split("|"))
    stop_cats=set(str(_get(profile,profiles,"stop_categories","")).split("|"))
    protect_cats=set(str(_get(profile,profiles,"protect_categories","")).split("|"))
    candidates=[]
    for (typ,cat,name),hist in groups.items():
        hist.sort(key=lambda x:x[0])
        if cat in protect_cats: continue
        if len(hist)<3: continue
        gaps=[(hist[i][0]-hist[i-1][0]).days for i in range(1,len(hist))]
        cadence=sorted(gaps)[len(gaps)//2]
        if not 6<=cadence<=45 or sum(abs(g-cadence)<=max(3,int(cadence*.2)) for g in gaps)<max(2,len(gaps)-1): continue
        last_date,last_row,last_amount=hist[-1]
        eid=str(_get(last_row,events,"event_id","")); flexibility=str(_get(last_row,events,"flexible","fixed")).lower()
        occurrences=[(d,abs(a)) for d,a,n in flows if start<=d<=end and n==name and a<0]
        if not eid or not occurrences: continue
        currency=str(_get(last_row,events,"currency",home)).upper(); rate=_currency_rate(rates,currency,home,last_date) if currency!=home else 1.0
        if rate is None: continue
        if cat in stop_cats and flexibility in {"stoppable","reducible_or_stoppable"}:
            candidates.append({"event_id":eid,"category":cat,"mode":"stop","target":0.0,"occurrences":occurrences,
                               "label":f"stop:{eid}"})
        min_col=next((c for c in events.columns if _norm(c)=="minimum_allowed_amount"),None)
        minimum=_num(last_row.get(min_col),None) if min_col else None
        if cat in reduce_cats and flexibility in {"reducible","reducible_or_stoppable"} and minimum is not None:
            target=minimum*rate
            if 0<=target<last_amount*rate:
                candidates.append({"event_id":eid,"category":cat,"mode":"reduce","target":target,"occurrences":occurrences,
                                   "label":f"reduce_to:{eid}:{_fmt(target)}"})
    return candidates

def _apply_spending_changes(flows, selected):
    adjusted=list(flows)
    for change in selected:
        for d,current in change["occurrences"]:
            adjusted.append((d,max(0.0,current-change["target"]),"flexible expense adjustment"))
    return adjusted

def _confirmed_message_income(messages: pd.DataFrame|None, user_id: Any, start: date, end: date,
                              home: str, rates: pd.DataFrame, known_flows) -> list[tuple[date,float,str]]:
    """Apply explicit confirmed payroll/invoice facts; never follow message instructions."""
    if messages is None or messages.empty: return []
    uc=_col(messages,"user_id"); source=next((c for c in messages if _norm(c)=="source_type"),None)
    textcol=next((c for c in messages if _norm(c)=="message_text"),None)
    timecol=next((c for c in messages if _norm(c)=="sent_at"),None)
    if not all((uc,source,textcol)): return []
    result=list(known_flows)
    subset=messages[(messages[uc].astype(str)==str(user_id)) & (messages[source].astype(str).str.lower().isin({"employer","service_provider","financial_service"}))]
    for _,m in subset.iterrows():
        sent=_date(m.get(timecol)) if timecol else None
        if sent and sent>start: continue
        txt=str(m.get(textcol,"")); low=txt.lower()
        payroll=bool(re.search(r"salary|payroll|gaji|gaji rutin",low)) and bool(re.search(r"confirmed|scheduled|increased|reduced|changed|applies from|confirmed credit date|naik|berlaku mulai|dikonfirmasi|ended|credit",low))
        invoice=bool(re.search(r"client approved an invoice payment|client menyetujui pembayaran faktur",low)) and bool(re.search(r"settlement is expected|penyelesaian diperkirakan",low))
        if not (payroll or invoice): continue
        date_matches=re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b",txt)
        dt=_date(date_matches[-1]) if date_matches else None
        amounts=re.findall(r"\b(INR|IDR|USD|EUR|ZAR)\s*([0-9]+(?:[.,][0-9]+)?)\b",txt,re.I)
        if not amounts: continue
        cur,raw=amounts[0]; amt=_num(raw,None)
        if amt is None: continue
        if (not dt or dt<start) and sent:
            # Messages about the next payroll can amend the already-supplied
            # confirmed upcoming salary even when they omit its calendar date.
            next_payroll=re.search(r"next payroll|next salary|payroll berikutnya|penggajian berikutnya",low)
            remaining_pay=re.search(r"remaining confirmed monthly salary|remaining .*monthly salary|remaining .*gaji",low)
            if not (next_payroll or remaining_pay): continue
            upcoming=[d for d,a,n in result if d>sent and any(k in n.lower() for k in ("salary","payroll"))]
            if not upcoming: continue
            dt=min(upcoming)
        if not dt or dt>end: continue
        cur=cur.upper()
        monthly=payroll and bool(re.search(r"monthly|per month|gaji bulanan|bulanan",low)) and not bool(re.search(r"one.time|one.off|sekali",low))
        dates=[dt]
        if monthly:
            y,m=dt.year,dt.month
            while True:
                m+=1
                if m>12: y+=1; m=1
                next_dt=date(y,m,min(dt.day,calendar.monthrange(y,m)[1]))
                if next_dt>end: break
                dates.append(next_dt)
        for effective in dates:
            effective_amount=amt
            if cur!=home:
                rate=_currency_rate(rates,cur,home,effective)
                if rate is None: continue
                effective_amount*=rate
            # The most recent explicit payroll amendment supersedes the older
            # scheduled/forecast value on the same payday.
            if payroll:
                result=[(d,a,n) for d,a,n in result if not (d==effective and any(k in n.lower() for k in ("salary","payroll")))]
                label="confirmed payroll update"
            else:
                if any(d==effective and abs(a-effective_amount)<=0.02 for d,a,n in result): continue
                label="confirmed invoice settlement"
            result.append((effective,effective_amount,label))
    return result

def _apply_confirmed_rent_updates(messages: pd.DataFrame|None, events: pd.DataFrame, user_id: Any,
                                 start: date, home: str, rates: pd.DataFrame, flows):
    if messages is None or messages.empty: return flows
    uc=_col(messages,"user_id"); source=next((c for c in messages if _norm(c)=="source_type"),None)
    txtcol=next((c for c in messages if _norm(c)=="message_text"),None); sentcol=next((c for c in messages if _norm(c)=="sent_at"),None)
    if not all((uc,source,txtcol)): return flows
    subset=messages[(messages[uc].astype(str)==str(user_id)) & (messages[source].astype(str).str.lower()=="service_provider")]
    updates=[]
    for _,m in subset.iterrows():
        sent=_date(m.get(sentcol)) if sentcol else None
        if sent and sent>start: continue
        low=str(m.get(txtcol,"")).lower()
        if "renewed lease" not in low or "rent" not in low: continue
        match=re.search(r"increases monthly rent by\s*([0-9]+(?:\.[0-9]+)?)\s*%",low)
        if match: updates.append((sent or start,1+float(match.group(1))/100))
    if not updates: return flows
    effective,factor=max(updates,key=lambda x:x[0])
    rows=events[events[_col(events,"user_id")].astype(str)==str(user_id)]
    rent_hist=[]
    for _,r in rows.iterrows():
        if str(_get(r,events,"status","")).lower()!="settled" or str(_get(r,events,"direction","")).lower()!="debit": continue
        if str(_get(r,events,"category","")).lower()!="rent": continue
        d=_date(_get(r,events,"date")); amt=_num(_get(r,events,"amount",None),None)
        if d and d<start and amt is not None:
            cur=str(_get(r,events,"currency",home)).upper(); rate=_currency_rate(rates,cur,home,d) if cur!=home else 1.0
            if rate: rent_hist.append((d,amt*rate))
    if not rent_hist: return flows
    baseline=max(rent_hist,key=lambda x:x[0])[1]
    adjusted=[]
    for d,a,n in flows:
        if d>=start and d>=effective and "rent" in n.lower() and a<0 and abs(abs(a)-baseline)<=max(0.02,baseline*.03):
            adjusted.append((d,a*factor,n))
        else: adjusted.append((d,a,n))
    return adjusted

def _safe_payment(balance: float, minimum: float, flows: list[tuple[date,float,str]], paydate: date, amount: float, start: date, end: date) -> bool:
    cash=balance
    bydate={}
    for d,a,_ in flows: bydate[d]=bydate.get(d,0)+a
    for d in sorted(set(bydate)|{paydate}):
        if d<start or d>end: continue
        cash+=bydate.get(d,0)
        if d==paydate: cash-=amount
        if cash < minimum-1e-7: return False
    return True

def _max_safe_today(balance: float, minimum: float, flows, start: date, end: date, cap: float) -> float:
    """Largest immediate payment that leaves every subsequent checkpoint safe."""
    cash=balance; max_pay=max(0.0,balance-minimum); bydate={}
    for d,a,_ in flows: bydate[d]=bydate.get(d,0)+a
    for d in sorted(bydate):
        if start<=d<=end:
            cash+=bydate[d]
            max_pay=min(max_pay,cash-minimum)
    return math.floor(max(0.0,min(cap,max_pay))*100+1e-9)/100

def _safe_plan(balance: float, minimum: float, flows, payments, start, end) -> bool:
    cash=balance; bydate={}
    for d,a,_ in flows: bydate[d]=bydate.get(d,0)+a
    for d,a in payments: bydate[d]=bydate.get(d,0)-a
    for d in sorted(bydate):
        if start<=d<=end:
            cash+=bydate[d]
            if cash<minimum-1e-7: return False
    return True

def build_predictions(tables: dict[str,pd.DataFrame], media: dict[str,bytes]|None=None,
                      amount_overrides: dict[str,float]|None=None) -> tuple[pd.DataFrame,pd.DataFrame]:
    req=tables["requests.csv"]; profiles=tables["financial_profiles.csv"]; events=tables["financial_events.csv"]
    options=tables["request_payment_options.csv"]; rates=tables["exchange_rates.csv"]
    event_id_col=_col(events,"event_id")
    missing_events=missing_amount_events_for_forecast(tables)
    unresolved=[str(r[event_id_col]) for _,r in missing_events.iterrows() if not amount_overrides or str(r[event_id_col]) not in amount_overrides]
    if unresolved:
        raise ValueError("Missing financial-event amounts must be recovered from their linked images before generating final predictions: " + ", ".join(unresolved))
    pc=_col(profiles,"user_id"); rc=_col(req,"request_id"); ru=_col(req,"user_id")
    rows=[]; audit=[]
    if not all((pc,rc,ru)): raise ValueError("Could not identify request_id or user_id columns.")
    for _,q in req.iterrows():
        rid=_get(q,req,"request_id",""); uid=_get(q,req,"user_id","")
        profs=profiles[profiles[pc].astype(str)==str(uid)]
        if profs.empty: raise ValueError(f"No financial profile found for user_id={uid} (request {rid}).")
        p=profs.iloc[-1]; when=_date(_get(q,req,"date")); amount=_num(_get(q,req,"amount"),0)
        if when is None: raise ValueError(f"Request {rid} has an invalid request_date.")
        deadline=_date(_get(q,req,"desired_completion_date",None)) or when
        currency=str(_get(p,profiles,"currency","USD")).upper()
        balance=_num(_get(p,profiles,"amount",None),0); minimum=_num(_get(p,profiles,"min_balance",None),0)
        end=when+timedelta(days=90)
        messages=tables.get("messages.csv")
        flows=_event_flows(events,uid,when,end,currency,rates,amount_overrides,messages)
        flows=_confirmed_message_income(messages,uid,when,end,currency,rates,flows)
        flows=_apply_confirmed_rent_updates(messages,events,uid,when,currency,rates,flows)
        safe_today=_max_safe_today(balance,minimum,flows,when,end,amount)
        earliest=None
        for day_offset in range(91):
            d=when+timedelta(days=day_offset)
            if _safe_payment(balance,minimum,flows,d,amount,when,end): earliest=d; break
        methods=str(_get(p,profiles,"prefs","full_payment|partial_payment|installments|wait")).lower().replace(",","|").replace(" ","")
        allowed=set(methods.split("|"))
        if when.isoformat() in allowed: allowed.add("full_payment")
        change_options=_spending_change_candidates(events,uid,when,end,currency,rates,p,profiles,flows)
        oc=_col(options,"request_id")
        opts=options[options[oc].astype(str)==str(rid)] if oc else options.iloc[0:0]
        installment_offers=[]
        max_installments=_num(_get(p,profiles,"max_installments",None),None)
        for _,o in opts.iterrows():
            if str(_get(o,options,"method","")).lower()!="installments": continue
            n=int(_num(_get(o,options,"count",1),1)); first=_date(_get(o,options,"first_date",when)) or when
            gap=int(_num(_get(o,options,"interval",0),0)); total=_num(_get(o,options,"total",None),amount+_num(_get(o,options,"fee",0)))
            if n<1 or n>36 or (max_installments is not None and n>max_installments) or gap<0 or first<when: continue
            pay_each=_num(_get(o,options,"payment_amount",None),total/n)
            offer_plan=[(first+timedelta(days=i*gap),pay_each) for i in range(n)]
            if offer_plan[-1][0]<=deadline and offer_plan[-1][0]<=end:
                installment_offers.append((total,n,str(_get(o,options,"option_id","")),offer_plan))

        def evaluate_changes(selected):
            adjusted=_apply_spending_changes(flows,selected)
            labels=[c["label"] for c in selected]
            options_for_scenario=[]
            if amount>0 and "full_payment" in allowed and _safe_plan(balance,minimum,adjusted,[(when,amount)],when,end):
                options_for_scenario.append(((bool(selected),amount,when,1,""),"full_payment",
                    "affordable_with_plan" if selected else "affordable_now",[(when,amount)],labels))
            if "installments" in allowed:
                for total,n,option_id,offer_plan in installment_offers:
                    if _safe_plan(balance,minimum,adjusted,offer_plan,when,end):
                        options_for_scenario.append(((bool(selected),total,offer_plan[0][0],n,option_id),"installments",
                            "affordable_with_plan",offer_plan,labels))
            if (str(_get(q,req,"allows_partial_payment",False)).lower() in {"true","yes","1"} and
                "partial_payment" in allowed and 0<safe_today<amount and earliest and earliest<=deadline):
                partial_plan=[(when,safe_today),(earliest,amount-safe_today)]
                if _safe_plan(balance,minimum,adjusted,partial_plan,when,end):
                    options_for_scenario.append(((bool(selected),amount,when,2,""),"partial_payment",
                        "affordable_with_plan",partial_plan,labels))
            if "full_payment" in allowed:
                for day_offset in range(1, min(90,(deadline-when).days)+1):
                    paydate=when+timedelta(days=day_offset)
                    if _safe_payment(balance,minimum,adjusted,paydate,amount,when,end):
                        options_for_scenario.append(((bool(selected),amount,paydate,1,""),"wait",
                            "affordable_with_plan" if selected else "affordable_later",[(paydate,amount)],labels))
                        break
            return options_for_scenario

        candidates=evaluate_changes([])
        # A plan without expense changes always outranks every plan that needs
        # changes, so explore flexible options only when the base case has no safe plan.
        if not candidates and change_options:
            for n_changes in range(1,min(3,len(change_options))+1):
                for selected in itertools.combinations(change_options,n_changes):
                    if len({c["event_id"] for c in selected})<len(selected): continue
                    candidates.extend(evaluate_changes(selected))
        if candidates:
            _,chosen,status,payments,chosen_changes=sorted(candidates,key=lambda x:x[0])[0]
        else:
            chosen="not_recommended"; status="affordable_later" if earliest and earliest>when and earliest<=deadline else "not_affordable"
            payments=[]; chosen_changes=[]
        plan="|".join(f"{d.isoformat()}:{_fmt(a)}" for d,a in sorted(payments)) if payments else "none"
        earliest_txt=earliest.isoformat() if earliest else ""
        expl=(f"Safe today: {_fmt(safe_today)} of {_fmt(amount)} {currency}. "
              f"Minimum reserve: {_fmt(minimum)} {currency}. " + (f"Full payment first passes the 90-day cash-flow check on {earliest_txt}." if earliest else "Full payment does not pass the 90-day cash-flow check."))
        if chosen=="full_payment": expl+=f" Paying in full on {when.isoformat()} passes the forecast check."
        elif chosen=="partial_payment": expl+=f" Pay {_fmt(safe_today)} today and the remaining {_fmt(amount-safe_today)} on {earliest_txt}."
        elif chosen=="installments": expl+=f" The supplied {len(payments)}-payment offer totals {_fmt(sum(a for _,a in payments))} {currency} and finishes by {payments[-1][0].isoformat()}."
        elif chosen=="wait": expl+=f" Wait until {payments[0][0].isoformat()} to pay the full amount safely."
        else: expl+=" No eligible safe payment plan fits the deadline and the 90-day reserve check."
        if chosen_changes: expl+=f" Required flexible-spending changes: {'; '.join(chosen_changes)}."
        rows.append({"request_id":rid,"amount_safe_to_pay":round(safe_today,2),"affordability_status":status,
                     "recommended_payment_method":chosen,"payment_plan":plan,"earliest_date_for_full_payment":earliest_txt,
                     "spending_changes_needed":"|".join(chosen_changes) if chosen_changes else "none","decision_explanation":expl})
        audit.append({"request_id":rid,"user_id":uid,"currency":currency,"balance":balance,"minimum_balance":minimum,"cashflow_events_used":len(flows),"image_files_received":len(media or {}),"notes":"Deterministic baseline; review image-linked missing amounts and event classifications."})
    return pd.DataFrame(rows,columns=OUTPUT_COLUMNS),pd.DataFrame(audit)
