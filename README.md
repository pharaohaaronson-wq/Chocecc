# ExpressVPN Checkout API (v2)

FastAPI + Playwright stealth service. Persistent browser — no cold start per card.

## Railway Deploy

1. Push this folder to its own GitHub repo
2. New Railway project → Deploy from GitHub
3. Set Variables:
   - `API_SECRET_KEY` = any long secret string
   - `MAX_CONCURRENCY` = 2 (safe for 512MB RAM)
4. Copy the Railway URL → paste into Choso Platform admin panel

## API

### POST /check
```json
{
  "email": "user@example.com",
  "cardholder_name": "John Smith",
  "card_number": "4111111111111111",
  "expiry": "12/28",
  "cvv": "123",
  "proxy": "http://user:pass@host:port",
  "actually_submit": true
}
```
Header: `x-api-key: YOUR_SECRET`

### Response
```json
{ "status": "live|dead|3ds|retry|error", "reason": "...", "card_status": "approved|declined|bad_cvv|invalid|3ds_required" }
```

### GET /health
Returns slot availability + busy state.
