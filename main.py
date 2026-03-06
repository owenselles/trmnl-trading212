import asyncio
import base64
import time

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

app = FastAPI(title="TRMNL Trading212 Plugin")

# In-memory cache: key = "{api_key_id}:{account_type}", value = (payload, timestamp)
_cache: dict[str, tuple[dict, float]] = {}
CACHE_TTL_SECONDS = 300  # 5 minutes

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


async def fetch_portfolio_data(api_key_id: str, api_secret: str, account_type: str) -> dict:
    base_url = BASE_URLS.get(account_type, BASE_URLS["live"])
    headers = {"Authorization": _auth_header(api_key_id, api_secret)}

    async with httpx.AsyncClient(timeout=10.0) as client:
        summary_resp, positions_resp = await asyncio.gather(
            client.get(f"{base_url}/equity/account/summary", headers=headers),
            client.get(f"{base_url}/equity/positions", headers=headers),
        )

    if summary_resp.status_code == 401 or positions_resp.status_code == 401:
        return {"has_error": True, "error": "Invalid API key or secret"}

    if summary_resp.status_code != 200:
        return {"has_error": True, "error": f"Account API error {summary_resp.status_code}"}

    if positions_resp.status_code != 200:
        return {"has_error": True, "error": f"Positions API error {positions_resp.status_code}"}

    summary = summary_resp.json()
    positions_raw: list[dict] = positions_resp.json()

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
        current_value = wallet.get("currentValue", 0.0)

        # Strip exchange suffix from ticker for readability (e.g. "AAPL_US_EQ" → "AAPL")
        ticker = instrument.get("ticker", "")
        short_ticker = ticker.split("_")[0] if "_" in ticker else ticker

        positions.append(
            {
                "ticker": short_ticker,
                "quantity": _fmt(p.get("quantity", 0.0)),
                "avg_price": _fmt(p.get("averagePricePaid", 0.0)),
                "current_price": _fmt(p.get("currentPrice", 0.0)),
                "current_value": _fmt(current_value),
                "ppl": _fmt(ppl, sign=True),
                "ppl_raw": round(ppl, 2),
                "is_positive": ppl >= 0,
            }
        )

    return_pct = (total_return / net_deposits * 100) if net_deposits != 0 else 0.0

    return {
        "total_value": _fmt(total_value),
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
        _cache[cache_key] = (payload, now)

    return JSONResponse(content=payload)


@app.get("/health")
async def health():
    return {"status": "ok"}
