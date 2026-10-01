"""Searchable NSE/BSE equity directory with optional official-list downloads and local cache.

NOT a price feed. A listing's presence does not guarantee Yahoo Finance coverage,
active tradability at a broker, or real-time quotations.
"""
import csv
import io
import json
import os
import re
import threading
import time
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent / "data"
CACHE_PATH = DATA_DIR / "stock_directory.json"
CACHE_TTL = 24 * 60 * 60
FAILED_RETRY = 15 * 60
FETCH_TIMEOUT = 7

NSE_URLS = {
    "nse_eq": "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
    "nse_sme": "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv",
}
BSE_URL = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData_new/w"
FALLBACK = [
    {"symbol": "RELIANCE.NS", "name": "Reliance Industries", "exchange": "NSE", "source": "starter"},
    {"symbol": "TCS.NS", "name": "Tata Consultancy Services", "exchange": "NSE", "source": "starter"},
    {"symbol": "INFY.NS", "name": "Infosys", "exchange": "NSE", "source": "starter"},
    {"symbol": "HDFCBANK.NS", "name": "HDFC Bank", "exchange": "NSE", "source": "starter"},
    {"symbol": "SBIN.NS", "name": "State Bank of India", "exchange": "NSE", "source": "starter"},
    {"symbol": "500325.BO", "name": "Reliance Industries", "exchange": "BSE", "source": "starter"},
    {"symbol": "532540.BO", "name": "Tata Consultancy Services", "exchange": "BSE", "source": "starter"},
    {"symbol": "500209.BO", "name": "Infosys", "exchange": "BSE", "source": "starter"},
    {"symbol": "500180.BO", "name": "HDFC Bank", "exchange": "BSE", "source": "starter"},
    {"symbol": "500112.BO", "name": "State Bank of India", "exchange": "BSE", "source": "starter"},
]
SOURCES = ("nse_eq", "nse_sme", "bse")
_lock = threading.Lock()
_memory = None
_last_attempt = 0


def clean_text(v):
    return " ".join(str(v or "").split())[:160]


def parse_nse(text, source):
    """NSE EQUITY_L/SME_EQUITY_L header: SYMBOL, NAME OF COMPANY, SERIES, ..."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        return []
    reader.fieldnames = [x.strip().upper() for x in reader.fieldnames]
    result = {}
    for row in reader:
        ticker = clean_text(row.get("SYMBOL")).upper()
        name = clean_text(row.get("NAME OF COMPANY") or row.get("COMPANY NAME"))
        series = clean_text(row.get("SERIES")).upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9&_.-]{0,22}", ticker) or not name:
            continue
        # Equity / SME listing lists may contain multiple series for the same company.
        key = ticker + ".NS"
        entry = {"symbol": key, "name": name, "exchange": "NSE", "source": source,
                 "segment": "SME" if source == "nse_sme" else "EQUITY", "series": series}
        result.setdefault(key, entry)
    return list(result.values())


def parse_bse(data):
    """BSE ListofScripData_new uses SCRIP_CD, Scrip_Name, scrip_id fields."""
    if isinstance(data, dict):
        # Some BSE API versions wrap rows in Table, Data or Data[].
        data = next((data[k] for k in ("Table", "Data", "data", "results")
                     if isinstance(data.get(k), list)), [])
    if not isinstance(data, list):
        return []
    result = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        code = clean_text(row.get("SCRIP_CD") or row.get("scrip_code") or
                          row.get("ScripCode") or row.get("SecurityCode") or row.get("scripcode"))
        name = clean_text(row.get("Scrip_Name") or row.get("Issuer_Name") or
                          row.get("SCRIP_NAME") or row.get("SecurityName") or row.get("ScripName"))
        bse_id = clean_text(row.get("scrip_id") or row.get("SCRIP_ID"))
        if not re.fullmatch(r"\d{6}", code) or not name:
            continue
        sym = code + ".BO"
        result[sym] = {"symbol": sym, "name": name, "exchange": "BSE", "source": "bse", "bse_id": bse_id}
    return list(result.values())


def session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36",
        "Accept": "text/csv,application/json,text/plain,*/*",
        "Referer": "https://www.nseindia.com/",
    })
    return s


def download_nse(s, source):
    # Local official CSV can be imported when network blocks automated downloads.
    local = DATA_DIR / ("EQUITY_L.csv" if source == "nse_eq" else "SME_EQUITY_L.csv")
    if local.exists():
        data = parse_nse(local.read_text(encoding="utf-8-sig"), source)
        if data:
            return data
    r = s.get(NSE_URLS[source], timeout=FETCH_TIMEOUT)
    r.raise_for_status()
    data = parse_nse(r.content.decode("utf-8-sig", errors="replace"), source)
    if len(data) < 10:
        raise ValueError("Unexpected/empty NSE listing response")
    return data


def download_bse(s):
    local = DATA_DIR / "bse_equity.csv"
    if local.exists():
        parsed = parse_bse(list(csv.DictReader(local.open(encoding="utf-8-sig", newline=""))))
        if parsed:
            return parsed
    s.headers.update({"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"})
    r = s.get(BSE_URL, params={"Group": "", "Scripcode": "", "segment": "Equity",
                                "status": "Active", "scripName": ""}, timeout=FETCH_TIMEOUT)
    r.raise_for_status()
    result = parse_bse(r.json())
    if len(result) < 10:
        raise ValueError("Unexpected/empty BSE listing response")
    return result


def _load_cache():
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data.get("groups"), dict):
            return data
    except (FileNotFoundError, ValueError, OSError):
        pass
    return {"groups": {}, "updated_at": 0, "errors": {}}


def _save_cache(doc):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, CACHE_PATH)


def catalog(force=False):
    """Fetch lists at most daily; preserve individually cached feeds if refresh fails."""
    global _memory, _last_attempt
    with _lock:
        if _memory is None:
            _memory = _load_cache()
        now = time.time()
        has_all = all(_memory["groups"].get(k) for k in SOURCES)
        # Public deployments must not fetch exchange listing feeds unless
        # the operator has verified the necessary access and reuse rights.
        if os.environ.get('TRADEX_ENABLE_DIRECTORY_FETCH') != '1':
            # Local operator-supplied directory CSVs (where rights permit) require no network calls.
            if not all(_memory['groups'].get(k) for k in SOURCES):
                groups = dict(_memory['groups'])
                for name in ('nse_eq','nse_sme'):
                    csv_path = DATA_DIR / ('EQUITY_L.csv' if name == 'nse_eq' else 'SME_EQUITY_L.csv')
                    if csv_path.exists():
                        rows = parse_nse(csv_path.read_text(encoding='utf-8-sig'), name)
                        if rows: groups[name] = rows
                csv_path = DATA_DIR / 'bse_equity.csv'
                if csv_path.exists():
                    with csv_path.open(encoding='utf-8-sig', newline='') as f:
                        rows = parse_bse(list(csv.DictReader(f)))
                    if rows: groups['bse'] = rows
                _memory = {**_memory, 'groups': groups}
            return _result(_memory)
        if not force:
            if has_all and now - _memory.get("updated_at", 0) < CACHE_TTL:
                return _result(_memory)
            if now - _last_attempt < FAILED_RETRY:
                return _result(_memory)
        _last_attempt = now
        groups = dict(_memory["groups"])
        errors = {}
        updated = False
        with session() as s:
            # Each source is independent: a network failure never deletes cached stocks.
            for key in SOURCES:
                try:
                    rows = download_bse(s) if key == "bse" else download_nse(s, key)
                    groups[key] = rows
                    updated = True
                except (requests.RequestException, ValueError, OSError) as exc:
                    errors[key] = str(exc)[:140]
        _memory = {"groups": groups, "updated_at": now if updated else _memory.get("updated_at", 0),
                   "errors": errors}
        if updated:
            _save_cache(_memory)
        return _result(_memory)


def _result(doc):
    by_symbol = {item["symbol"]: item for item in FALLBACK}
    for key in SOURCES:
        for row in doc.get("groups", {}).get(key, []):
            if isinstance(row, dict) and row.get("symbol"):
                by_symbol[row["symbol"]] = row
    rows = sorted(by_symbol.values(), key=lambda x: (x["name"].casefold(), x["exchange"], x["symbol"]))
    source_counts = {s: len(doc.get("groups", {}).get(s, [])) for s in SOURCES}
    return rows, {"source_counts": source_counts, "directory_count": len(rows),
                  "updated_at": doc.get("updated_at") or None,
                  "source_errors": doc.get("errors", {}),
                  "partial": not all(source_counts.values())}


def find_stock(symbol):
    """Use only locally available directory data; quote lookup must not trigger list download."""
    cached = _memory if _memory is not None else _load_cache()
    for row in FALLBACK:
        if row["symbol"] == symbol:
            fallback = row
            break
    else:
        fallback = None
    for group in cached.get("groups", {}).values():
        for row in group:
            if row.get("symbol") == symbol:
                return row
    return fallback


def search(q="", exchange="ALL", limit=25, offset=0, force=False):
    rows, meta = catalog(force=force)
    q = q.strip().casefold()
    exchange = exchange.upper()
    if exchange not in ("ALL", "NSE", "BSE"):
        exchange = "ALL"
    rows = (row for row in rows if exchange == "ALL" or row["exchange"] == exchange)
    if q:
        rows = (row for row in rows if q in row["name"].casefold() or q in row["symbol"].casefold()
                or q in row.get("bse_id", "").casefold())
        rows = sorted(rows, key=lambda row: (
            0 if row["symbol"].casefold().startswith(q) else
            1 if row.get("bse_id", "").casefold().startswith(q) else
            2 if row["name"].casefold().startswith(q) else 3,
            row["name"].casefold(), row["symbol"]))
    else:
        rows = list(rows)
    total = len(rows)
    return {"stocks": rows[offset: offset + limit], "total": total,
            "limit": limit, "offset": offset, **meta}
