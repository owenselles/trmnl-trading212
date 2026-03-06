# trmnl-trading212

A [TRMNL](https://usetrmnl.com) private plugin that displays your [Trading212](https://trading212.com) portfolio on your e-ink device.

**Displays:**
- Total portfolio value, invested amount, free cash
- Total unrealized P&L (Trading212's API does not expose daily P&L)
- Top 8 open positions sorted by absolute P&L

---

## How it works

A lightweight FastAPI server (hosted on Railway) fetches your portfolio data from the Trading212 API and returns it as JSON. TRMNL polls this server on a schedule, renders the Liquid template, and pushes it to your device as a PNG image.

---

## Prerequisites

- [TRMNL](https://usetrmnl.com) device + account with **Developer** perks enabled
- [Railway](https://railway.app) account (free tier works)
- [Trading212](https://trading212.com) account (Invest or ISA — CFD accounts are not supported by the Trading212 API)
- Trading212 API key: **Settings → API → Generate key**
- Python 3.11+ (for local server development)
- Ruby + `gem install trmnl_preview` (for local template development)

---

## Local development

### Server

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set TRADING212_API_KEY

uvicorn main:app --reload
```

Test the endpoint:
```bash
curl "http://localhost:8000/data?api_key=YOUR_KEY&account_type=live" | python3 -m json.tool
curl "http://localhost:8000/health"
```

### Template (trmnlp)

```bash
gem install trmnl_preview
cp .env.example .env   # if not already done
trmnlp serve           # opens preview at http://localhost:4567
```

Edit `src/full.liquid` — the preview hot-reloads automatically.

---

## Deploy to Railway

1. Push this repo to GitHub.
2. [Railway](https://railway.app) → **New Project** → **Deploy from GitHub repo** → select `trmnl-trading212`.
3. Railway auto-detects Python via `requirements.txt`. The `Procfile` sets the start command.
4. After deploy: **Settings → Networking → Generate Domain** → copy the URL (e.g. `https://trmnl-trading212-production.up.railway.app`).
5. Update `src/settings.yml`: replace `YOUR-RAILWAY-APP` in `polling_url` with your actual Railway domain.
6. Verify: `curl https://YOUR-APP.up.railway.app/health`

No environment variables are needed on Railway — the API key is passed per-request.

---

## TRMNL plugin setup

1. Go to **trmnl.com → Plugins → Private Plugins → Create new**.
2. **Name:** `Trading212 Portfolio`
3. **Strategy:** Polling
4. **Polling URL:** paste the `polling_url` value from `src/settings.yml` (with your Railway domain)
5. **Refresh interval:** 60 minutes
6. **Form Fields (YAML):** paste the `custom_fields` block from `src/settings.yml`
7. **Markup:** paste the contents of `src/full.liquid`
8. Click **Save**, then fill in your Trading212 API key and account type in the plugin settings.
9. Click **Force Refresh** to immediately generate a screen.

### Push template with trmnlp (alternative)

```bash
trmnlp login   # enter your TRMNL API key from trmnl.com/settings
trmnlp push    # uploads src/ to your TRMNL plugin
```

---

## Making it public (marketplace)

To support users with different currencies:
1. Add a `currency_symbol` custom field (type: string, default: `€`) to `src/settings.yml`.
2. Replace the hardcoded `€` in `src/full.liquid` with `{{ currency_symbol }}`.
3. Export the plugin ZIP from TRMNL and submit to the marketplace.

---

## Notes

- **Rate limiting:** Responses are cached per API key for 5 minutes on the server, so TRMNL can poll frequently without hitting Trading212's rate limits.
- **Currency:** Values are displayed in euros (`€`). The Trading212 API returns values in your account's base currency without specifying the symbol.
- **Positions:** Shows top 8 positions by absolute P&L. The server fetches up to 10 and the template limits to 8 for display fit.
- **CFD accounts:** The Trading212 API only supports Invest/ISA accounts.
