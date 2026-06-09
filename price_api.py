"""
price_api.py  —  FairHarvest Mock Price Feed API
================================================
Serves commodity prices that the Oracle Updater polls every 2-4 hours.
Run:  uvicorn price_api:app --reload --port 8000

Endpoints
---------
GET /price/{commodity}   → { commodity, price_tl, unit, timestamp, source }
GET /prices              → list of all tracked commodities

Prices drift realistically using Brownian motion so the demo looks live.
"""

from __future__ import annotations

import math
import os
import random
import time
from datetime import datetime, timezone
from typing import Dict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="FairHarvest Price Feed API",
    description="Mock commodity price oracle for the FairHarvest testnet demo.",
    version="1.0.0",
)

# ─── Commodity Base Prices (TL per kg) ────────────────────────────────────────

BASE_PRICES: Dict[str, float] = {
    "potato":   5.20,
    "tomato":   8.75,
    "wheat":    3.10,
    "corn":     4.50,
    "onion":    6.00,
    "sunflower": 14.30,
}

# Tracks the last "walked" price so drift is continuous across calls
_price_state: Dict[str, float] = dict(BASE_PRICES)
_last_walk_ts: float = time.time()

# ─── Brownian-ish price walk ───────────────────────────────────────────────────

DRIFT_PER_HOUR = 0.005       # 0.5 % upward drift per hour
VOLATILITY_PER_HOUR = 0.015  # 1.5 % std-dev per hour


def _walk_prices() -> None:
    """Apply a small random walk to all prices (capped at ±18% from base)."""
    global _last_walk_ts
    now = time.time()
    dt_hours = (now - _last_walk_ts) / 3600.0
    _last_walk_ts = now

    for commodity, price in _price_state.items():
        drift = DRIFT_PER_HOUR * dt_hours
        shock = random.gauss(0, VOLATILITY_PER_HOUR * math.sqrt(dt_hours))
        new_price = price * (1 + drift + shock)

        # Hard floor at 80 % of base; ceiling at 120 % of base
        base = BASE_PRICES[commodity]
        new_price = max(base * 0.80, min(base * 1.20, new_price))
        _price_state[commodity] = round(new_price, 4)


# ─── Response Models ──────────────────────────────────────────────────────────

class PriceResponse(BaseModel):
    commodity: str
    price_tl: float          # TL per kg, 4 decimal places
    price_scaled: int        # price_tl × 1_000_000  (sent to oracle contract)
    unit: str
    timestamp: str           # ISO-8601 UTC
    source: str


class PriceListResponse(BaseModel):
    prices: list[PriceResponse]
    timestamp: str


_start_time = time.time()

# ─── Routes ───────────────────────────────────────────────────────────────────


@app.get("/prices", response_model=PriceListResponse, tags=["prices"])
def list_prices():
    _walk_prices()
    now = datetime.now(timezone.utc).isoformat()
    items = [
        PriceResponse(
            commodity=c,
            price_tl=p,
            price_scaled=int(p * 1_000_000),
            unit="TL/kg",
            timestamp=now,
            source="FairHarvest-MockFeed-v1",
        )
        for c, p in _price_state.items()
    ]
    return PriceListResponse(prices=items, timestamp=now)


@app.get("/price/{commodity}", response_model=PriceResponse, tags=["prices"])
def get_price(commodity: str):
    commodity = commodity.lower()
    if commodity not in _price_state:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown commodity '{commodity}'. Available: {list(_price_state.keys())}",
        )
    _walk_prices()
    price = _price_state[commodity]
    return PriceResponse(
        commodity=commodity,
        price_tl=price,
        price_scaled=int(price * 1_000_000),
        unit="TL/kg",
        timestamp=datetime.now(timezone.utc).isoformat(),
        source="FairHarvest-MockFeed-v1",
    )
