import asyncio
import base64
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

app = FastAPI(title="TRMNL Trading212 Plugin")

# In-memory cache: key = "{api_key_id}:{account_type}", value = (payload, timestamp)
_cache: dict[str, tuple[dict, float]] = {}
CACHE_TTL_SECONDS = 300  # 5 minutes

# Snapshot store for 24hr change: key = cache_key, value = list of (timestamp, raw_value)
_snapshots: dict[str, list[tuple[float, float]]] = {}

BASE_URLS = {
    "live": "https://live.trading212.com/api/v0",
    "demo": "https://demo.trading212.com/api/v0",
}


def _strip(value: str) -> str:
    """TRMNL prepends ## to form field values when interpolating into polling URLs."""
    return value[2:] if value.startswith("##") else value


def _auth_header(api_key_id: str, api_secret: str) -> str:
    """Build HTTP Basic Auth header value from Trading212 key ID and secret."""
    credentials = base64.b64encode(f"{api_key_id}:{api_secret}".encode()).decode()
    return f"Basic {credentials}"


def _fmt(value: float, sign: bool = False) -> str:
    """Format a float to 2 decimal places with thousands separator and optional + sign."""
    formatted = f"{value:,.2f}"
    if sign and value > 0:
        return f"+{formatted}"
    return formatted


def _record_snapshot(cache_key: str, value: float) -> None:
    """Record a portfolio value snapshot for 24hr change tracking."""
    now = time.time()
    if cache_key not in _snapshots:
        _snapshots[cache_key] = []
    _snapshots[cache_key].append((now, value))
    # Prune snapshots older than 48 hours
    cutoff = now - 172800
    _snapshots[cache_key] = [(t, v) for t, v in _snapshots[cache_key] if t > cutoff]


def _get_24hr_change(cache_key: str, current_value: float) -> tuple[float | None, float | None]:
    """Calculate 24hr portfolio value change. Returns (change, change_pct) or (None, None)."""
    snaps = _snapshots.get(cache_key, [])
    if not snaps:
        return None, None

    now = time.time()
    target = now - 86400  # 24 hours ago

    # Use snapshot closest to 24hr ago; fall back to oldest if less than 24hrs of data
    baseline_snap = min(snaps, key=lambda x: abs(x[0] - target))

    # Don't show change if our oldest snapshot is only minutes old (< 10 min)
    if now - baseline_snap[0] < 600:
        return None, None

    baseline_value = baseline_snap[1]
    change = current_value - baseline_value
    change_pct = (change / baseline_value * 100) if baseline_value else 0.0
    return change, change_pct


def _get_market_status(exchanges: list) -> dict:
    """Parse exchange time events to determine open/closed status and next event time."""
    if not isinstance(exchanges, list):
        return {"market_open": False, "market_status": "Unknown", "market_name": ""}

    now = datetime.now(timezone.utc)
    open_markets = []
    upcoming_opens = []

    for exchange in exchanges:
        name = exchange.get("name", "Market")
        for schedule in exchange.get("workingSchedules", []):
            events = []
            for e in schedule.get("timeEvents", []):
                try:
                    dt = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
                    events.append({"dt": dt, "type": e["type"]})
                except Exception:
                    continue
            events.sort(key=lambda x: x["dt"])

            past = [e for e in events if e["dt"] <= now]
            future = [e for e in events if e["dt"] > now]

            if not past and not future:
                continue

            is_open = False
            if past:
                last = past[-1]
                is_open = last["type"] in (
                    "OPEN", "BREAK_END", "AFTER_HOURS_OPEN", "PRE_MARKET_OPEN", "OVERNIGHT_OPEN"
                )

            if is_open and future:
                next_close = next(
                    (e for e in future if e["type"] in ("CLOSE", "BREAK_START", "AFTER_HOURS_CLOSE")),
                    None,
                )
                if next_close:
                    open_markets.append({
                        "name": name,
                        "closes_at": next_close["dt"].strftime("%H:%M"),
                        "closes_at_dt": next_close["dt"],
                    })
            elif not is_open and future:
                next_open = next(
                    (e for e in future if e["type"] in ("OPEN", "PRE_MARKET_OPEN")),
                    None,
                )
                if next_open:
                    upcoming_opens.append({
                        "opens_at": next_open["dt"].strftime("%H:%M"),
                        "opens_at_dt": next_open["dt"],
                    })

    if open_markets:
        open_markets.sort(key=lambda x: x["closes_at_dt"])
        m = open_markets[0]
        return {
            "market_open": True,
            "market_status": f"Open · closes {m['closes_at']} UTC",
            "market_name": m["name"],
        }
    elif upcoming_opens:
        upcoming_opens.sort(key=lambda x: x["opens_at_dt"])
        m = upcoming_opens[0]
        return {
            "market_open": False,
            "market_status": f"Closed · opens {m['opens_at']} UTC",
            "market_name": "",
        }
    else:
        return {
            "market_open": False,
            "market_status": "Closed",
            "market_name": "",
        }


async def fetch_portfolio_data(api_key_id: str, api_secret: str, account_type: str) -> dict:
    base_url = BASE_URLS.get(account_type, BASE_URLS["live"])
    headers = {"Authorization": _auth_header(api_key_id, api_secret)}

    async with httpx.AsyncClient(timeout=10.0) as client:
        summary_resp, positions_resp, exchanges_resp = await asyncio.gather(
            client.get(f"{base_url}/equity/account/summary", headers=headers),
            client.get(f"{base_url}/equity/positions", headers=headers),
            client.get(f"{base_url}/equity/metadata/exchanges", headers=headers),
        )

    if summary_resp.status_code == 401 or positions_resp.status_code == 401:
        return {"has_error": True, "error": "Invalid API key or secret"}

    if summary_resp.status_code != 200:
        return {"has_error": True, "error": f"Account API error {summary_resp.status_code}"}

    if positions_resp.status_code != 200:
        return {"has_error": True, "error": f"Positions API error {positions_resp.status_code}"}

    summary = summary_resp.json()
    positions_raw: list[dict] = positions_resp.json()
    exchanges = exchanges_resp.json() if exchanges_resp.status_code == 200 else []

    # Extract account-level values
    investments = summary.get("investments", {})
    cash = summary.get("cash", {})
    total_value = summary.get("totalValue", 0.0)
    unrealized_pnl = investments.get("unrealizedProfitLoss", 0.0)
    realized_pnl = investments.get("realizedProfitLoss", 0.0)
    available_cash = cash.get("availableToTrade", 0.0)

    # Net deposits ≈ total value minus all P&L ever made (proxy, slightly off by fees)
    total_return = unrealized_pnl + realized_pnl
    net_deposits = total_value - total_return

    # Sort positions by current value descending (largest holdings first)
    positions_raw.sort(
        key=lambda p: p.get("walletImpact", {}).get("currentValue", 0.0),
        reverse=True,
    )

    positions = []
    for p in positions_raw[:14]:
        wallet = p.get("walletImpact", {})
        instrument = p.get("instrument", {})
        ppl = wallet.get("unrealizedProfitLoss", 0.0)
        current_value_pos = wallet.get("currentValue", 0.0)

        # Strip exchange suffix from ticker for readability (e.g. "AAPL_US_EQ" → "AAPL")
        ticker = instrument.get("ticker", "")
        short_ticker = ticker.split("_")[0] if "_" in ticker else ticker

        positions.append(
            {
                "ticker": short_ticker,
                "quantity": _fmt(p.get("quantity", 0.0)),
                "avg_price": _fmt(p.get("averagePricePaid", 0.0)),
                "current_price": _fmt(p.get("currentPrice", 0.0)),
                "current_value": _fmt(current_value_pos),
                "ppl": _fmt(ppl, sign=True),
                "ppl_raw": round(ppl, 2),
                "is_positive": ppl >= 0,
            }
        )

    return_pct = (total_return / net_deposits * 100) if net_deposits != 0 else 0.0

    market_info = _get_market_status(exchanges)

    return {
        "total_value": _fmt(total_value),
        "total_value_raw": total_value,
        "free_cash": _fmt(available_cash),
        "net_deposits": _fmt(net_deposits),
        "total_return": _fmt(total_return, sign=True),
        "return_pct": _fmt(return_pct, sign=True),
        "unrealized_pnl": _fmt(unrealized_pnl, sign=True),
        "realized_pnl": _fmt(realized_pnl, sign=True),
        "is_return_positive": total_return >= 0,
        "position_count": len(positions_raw),
        "positions": positions,
        "has_error": False,
        "error": "",
        "market_open": market_info["market_open"],
        "market_status": market_info["market_status"],
        "market_name": market_info["market_name"],
        # 24hr change fields populated by endpoint handler
        "change_24h": "",
        "change_24h_pct": "",
        "has_24h_change": False,
        "is_24h_positive": True,
    }


@app.get("/data")
async def get_data(
    api_key_id: str = Query(..., description="Trading212 API key ID"),
    api_secret: str = Query(..., description="Trading212 API secret"),
    account_type: str = Query("live", description="live or demo"),
):
    api_key_id = _strip(api_key_id)
    api_secret = _strip(api_secret)
    account_type = _strip(account_type)

    if account_type not in ("live", "demo"):
        account_type = "live"

    cache_key = f"{api_key_id}:{account_type}"
    now = time.time()

    if cache_key in _cache:
        cached_payload, cached_at = _cache[cache_key]
        if now - cached_at < CACHE_TTL_SECONDS:
            return JSONResponse(content=cached_payload)

    payload = await fetch_portfolio_data(api_key_id, api_secret, account_type)

    if not payload.get("has_error"):
        total_value_raw = payload.pop("total_value_raw", 0.0)

        # Calculate 24hr change before recording new snapshot
        change_24h, change_24h_pct = _get_24hr_change(cache_key, total_value_raw)
        _record_snapshot(cache_key, total_value_raw)

        if change_24h is not None:
            payload["change_24h"] = _fmt(change_24h, sign=True)
            payload["change_24h_pct"] = _fmt(change_24h_pct, sign=True)
            payload["has_24h_change"] = True
            payload["is_24h_positive"] = change_24h >= 0

        _cache[cache_key] = (payload, now)

    return JSONResponse(content=payload)


@app.get("/health")
async def health():
    return {"status": "ok"}
