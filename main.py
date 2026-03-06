import asyncio
import time

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

app = FastAPI(title="TRMNL Trading212 Plugin")

# In-memory cache: key = "{api_key}:{account_type}", value = (payload, timestamp)
_cache: dict[str, tuple[dict, float]] = {}
CACHE_TTL_SECONDS = 300  # 5 minutes

BASE_URLS = {
    "live": "https://live.trading212.com/api/v0",
    "demo": "https://demo.trading212.com/api/v0",
}


def _fmt(value: float, sign: bool = False) -> str:
    """Format a float to 2 decimal places with thousands separator and optional + sign."""
    formatted = f"{value:,.2f}"
    if sign and value > 0:
        return f"+{formatted}"
    return formatted


async def fetch_portfolio_data(api_key: str, account_type: str) -> dict:
    base_url = BASE_URLS.get(account_type, BASE_URLS["live"])
    headers = {"Authorization": api_key}

    async with httpx.AsyncClient(timeout=10.0) as client:
        cash_resp, portfolio_resp = await asyncio.gather(
            client.get(f"{base_url}/equity/account/cash", headers=headers),
            client.get(f"{base_url}/equity/portfolio", headers=headers),
        )

    if cash_resp.status_code == 401 or portfolio_resp.status_code == 401:
        return {"has_error": True, "error": "Invalid API key"}

    if cash_resp.status_code != 200:
        return {"has_error": True, "error": f"Cash API error {cash_resp.status_code}"}

    if portfolio_resp.status_code != 200:
        return {"has_error": True, "error": f"Portfolio API error {portfolio_resp.status_code}"}

    cash = cash_resp.json()
    positions_raw: list[dict] = portfolio_resp.json()

    # Sort by absolute P&L descending (biggest movers first)
    positions_raw.sort(key=lambda p: abs(p.get("ppl", 0.0)), reverse=True)

    positions = []
    for p in positions_raw[:10]:
        ppl = p.get("ppl", 0.0)
        current_value = p.get("quantity", 0.0) * p.get("currentPrice", 0.0)
        # Strip exchange suffix from ticker for readability (e.g. "AAPL_US_EQ" → "AAPL")
        ticker = p.get("ticker", "")
        short_ticker = ticker.split("_")[0] if "_" in ticker else ticker
        positions.append(
            {
                "ticker": short_ticker,
                "quantity": _fmt(p.get("quantity", 0.0)),
                "avg_price": _fmt(p.get("averagePrice", 0.0)),
                "current_price": _fmt(p.get("currentPrice", 0.0)),
                "current_value": _fmt(current_value),
                "ppl": _fmt(ppl, sign=True),
                "ppl_raw": round(ppl, 2),
                "is_positive": ppl >= 0,
            }
        )

    total_result = cash.get("result", 0.0)

    return {
        "total_value": _fmt(cash.get("total", 0.0)),
        "free_cash": _fmt(cash.get("free", 0.0)),
        "invested": _fmt(cash.get("invested", 0.0)),
        "unrealized_pnl": _fmt(total_result, sign=True),
        "unrealized_pnl_raw": round(total_result, 2),
        "is_pnl_positive": total_result >= 0,
        "position_count": len(positions_raw),
        "positions": positions,
        "account_type": account_type.upper(),
        "has_error": False,
        "error": "",
    }


@app.get("/data")
async def get_data(
    api_key: str = Query(..., description="Trading212 API key"),
    account_type: str = Query("live", description="live or demo"),
):
    if account_type not in ("live", "demo"):
        account_type = "live"

    cache_key = f"{api_key}:{account_type}"
    now = time.time()

    if cache_key in _cache:
        cached_payload, cached_at = _cache[cache_key]
        if now - cached_at < CACHE_TTL_SECONDS:
            return JSONResponse(content=cached_payload)

    payload = await fetch_portfolio_data(api_key, account_type)

    if not payload.get("has_error"):
        _cache[cache_key] = (payload, now)

    return JSONResponse(content=payload)


@app.get("/health")
async def health():
    return {"status": "ok"}
