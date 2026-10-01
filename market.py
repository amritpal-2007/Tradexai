"""Deterministic FICTIONAL practice candles for searchable companies; NEVER real quotes.
Uses same immutable close price for chart, watchlist, portfolio and execution.
"""
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import re

INSTRUMENTS = {
    'DEMO-A': ('Example Industries', 1240, 17),
    'DEMO-B': ('Practice Technologies', 785, 91),
    'DEMO-C': ('Sample Banking', 1635, 42),
}

def is_valid_symbol(symbol):
    return isinstance(symbol, str) and bool(re.fullmatch(r'(?:[A-Z0-9&_.-]{1,23}\.NS|[0-9]{6}\.BO|DEMO-[ABC])', symbol))

def profile(symbol):
    if not is_valid_symbol(symbol):
        raise ValueError('Invalid symbol')
    if symbol in INSTRUMENTS:
        _, base, seed = INSTRUMENTS[symbol]
        return base, seed
    digest = hashlib.sha256(('TRADEX-SIM:' + symbol).encode()).digest()
    # Fictional per-ticker baseline is NOT the underlying security's market price.
    base = 120 + int.from_bytes(digest[:3], 'big') % 3900
    seed = 1 + int.from_bytes(digest[3:7], 'big') % (2147483646)
    return base, seed

def candles(symbol):
    base, v = profile(symbol)
    last = float(base)
    rows = []
    def rnd():
        nonlocal v
        v = (v * 16807) % 2147483647
        return (v - 1) / 2147483646
    # Imaginary 1m session. Does NOT reflect any real NSE/BSE session.
    for i in range(375):
        open_ = last
        volatility = base * .0028
        close = max(20.0, open_ + (rnd() - .48) * volatility * 2)
        high = max(open_, close) + rnd() * volatility
        low = min(open_, close) - rnd() * volatility
        rows.append({'minutes': 555 + i, 'open': round(open_, 2),
                     'high': round(high, 2), 'low': round(low, 2), 'close': round(close, 2)})
        last = close
    return rows

def price_paise(symbol):
    # Orders use the final candle displayed in the browser, not a separate quote.
    return int((Decimal(str(candles(symbol)[-1]['close'])) * 100).quantize(
        Decimal('1'), rounding=ROUND_HALF_UP))
