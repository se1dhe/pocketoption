#!/usr/bin/env python3
"""
PocketOption Data Microservice
Connects to real PocketOption API using SSID for live balance and market data
"""

import asyncio
import json
import os
from aiohttp import web
import logging
from datetime import datetime

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Cache
candles_cache = {}
balance_cache = {"amount": None, "currency": "USD", "updated_at": None}
po_client = None
client_connected = False
last_warning_at = {}

SSID = os.environ.get("POCKET_OPTION_SSID", "").strip().strip('"').strip("'")
PO_UID = int(os.environ.get("POCKET_OPTION_UID", "0") or "0")
PO_IS_DEMO = os.environ.get("POCKET_OPTION_IS_DEMO", "true").lower() in {"1", "true", "yes", "y"}
WARN_THROTTLE_SECONDS = int(os.environ.get("PO_SERVICE_WARN_THROTTLE_SECONDS", "60") or "60")


def throttled_warning(key: str, message: str) -> None:
    """Log noisy warnings no more than once per key per throttle window."""
    now = datetime.utcnow().timestamp()
    previous = last_warning_at.get(key, 0)
    if now - previous >= WARN_THROTTLE_SECONDS:
        last_warning_at[key] = now
        logger.warning(message)


async def init_po_client():
    """Initialize real PocketOption client with SSID"""
    global po_client, client_connected, PO_UID, PO_IS_DEMO
    if not SSID:
        logger.warning("[PO_SERVICE] No SSID found — balance will be unavailable")
        return

    try:
        from pocketoptionapi_async import AsyncPocketOptionClient

        ssid_str = SSID
        uid = PO_UID
        is_demo = PO_IS_DEMO

        try:
            parsed = json.loads(ssid_str)
            if isinstance(parsed, list) and len(parsed) >= 2 and parsed[0] == "auth":
                uid = int(parsed[1].get("uid", uid) or 0)
                is_demo = parsed[1].get("isDemo", 1) == 1
                logger.info(f"[PO_SERVICE] SSID parsed as auth JSON: uid={uid}, isDemo={is_demo}")
            else:
                logger.info(f"[PO_SERVICE] Raw SSID token detected: uid={uid}, isDemo={is_demo}")
        except Exception:
            logger.info(f"[PO_SERVICE] Raw SSID token detected: uid={uid}, isDemo={is_demo}")

        po_client = AsyncPocketOptionClient(
            ssid=ssid_str,
            is_demo=is_demo,
            uid=uid,
            platform=29,
            is_fast_history=True,
            auto_reconnect=True,
            enable_logging=False
        )

        logger.info("[PO_SERVICE] Connecting to PocketOption...")
        connected = await asyncio.wait_for(po_client.connect(), timeout=20)
        if connected:
            client_connected = True
            PO_UID = uid
            PO_IS_DEMO = is_demo
            logger.info("[PO_SERVICE] ✅ Connected to PocketOption!")
            await refresh_balance()
        else:
            logger.warning("[PO_SERVICE] ⚠️ Could not connect to PocketOption — using offline mode")
    except asyncio.TimeoutError:
        logger.warning("[PO_SERVICE] ⚠️ Connection timeout — using offline mode")
    except Exception as e:
        logger.warning(f"[PO_SERVICE] ⚠️ PocketOption init failed: {e} — using offline mode")


async def refresh_balance():
    """Fetch balance from PocketOption"""
    global balance_cache
    if not po_client or not client_connected:
        return
    try:
        bal = await asyncio.wait_for(po_client.get_balance(), timeout=10)
        if bal:
            balance_cache["amount"] = float(bal.amount) if hasattr(bal, 'amount') else float(bal)
            balance_cache["updated_at"] = datetime.utcnow().isoformat()
            logger.info(f"[PO_SERVICE] Balance: ${balance_cache['amount']:.2f}")
    except Exception as e:
        throttled_warning("balance", f"[PO_SERVICE] Balance fetch error: {e}")


async def balance_refresh_loop():
    """Refresh balance every 30 seconds"""
    while True:
        await asyncio.sleep(30)
        await refresh_balance()


async def handle_balance(request):
    """Return real account balance"""
    return web.json_response({
        "success": True,
        "connected": client_connected,
        "balance": balance_cache["amount"],
        "currency": balance_cache["currency"],
        "updated_at": balance_cache["updated_at"]
    })


async def handle_candles(request):
    """HTTP endpoint to fetch candles"""
    try:
        data = await request.json()
        asset = data.get("asset")
        timeframe = data.get("timeframe", "1m")
        count = data.get("count", 50)

        if not asset:
            return web.json_response({"error": "asset required"}, status=400)

        cache_key = f"{asset}/{timeframe}"
        if cache_key in candles_cache:
            return web.json_response({"success": True, "candles": candles_cache[cache_key]})

        if po_client and client_connected:
            try:
                real_candles = await asyncio.wait_for(
                    po_client.get_candles(asset, timeframe, count),
                    timeout=10
                )
                if real_candles and len(real_candles) > 0:
                    formatted = []
                    for c in real_candles:
                        formatted.append({
                            "time": int(c.time.timestamp() * 1000) if hasattr(c.time, 'timestamp') else int(c.time),
                            "open": float(c.open),
                            "high": float(c.high),
                            "low": float(c.low),
                            "close": float(c.close),
                            "volume": float(c.volume) if hasattr(c, 'volume') else 1000.0
                        })
                    candles_cache[cache_key] = formatted
                    logger.info(f"[PO_SERVICE] Real candles fetched for {asset}/{timeframe}")
                    return web.json_response({"success": True, "candles": formatted})
            except Exception as e:
                throttled_warning(
                    f"fetch_failed:{cache_key}",
                    f"[PO_SERVICE] Real candle fetch failed for {asset}/{timeframe}: {e}"
                )

        throttled_warning(
            f"unavailable:{cache_key}",
            f"[PO_SERVICE] No real candles available for {asset}/{timeframe} — exchange not connected"
        )
        return web.json_response({"success": False, "candles": [], "reason": "exchange_unavailable"})

    except Exception as e:
        logger.error(f"[PO_SERVICE] Handler error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle_status(request):
    """HTTP endpoint for service status"""
    return web.json_response({
        "status": "ready",
        "connected": client_connected,
        "uid": PO_UID,
        "is_demo": PO_IS_DEMO,
        "cache_size": len(candles_cache),
        "balance": balance_cache["amount"]
    })


async def main():
    """Main service runner"""
    asyncio.create_task(init_po_client())

    app = web.Application()
    app.router.add_post('/api/candles', handle_candles)
    app.router.add_get('/api/balance', handle_balance)
    app.router.add_get('/api/status', handle_status)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 5001)
    await site.start()

    logger.info("[PO_SERVICE] 🚀 Microservice running on http://127.0.0.1:5001")
    logger.info("[PO_SERVICE] POST /api/candles - Fetch market candles")
    logger.info("[PO_SERVICE] GET /api/balance  - Fetch real account balance")
    logger.info("[PO_SERVICE] GET /api/status   - Service status")

    asyncio.create_task(balance_refresh_loop())

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("[PO_SERVICE] Shutting down...")
        if po_client:
            await po_client.disconnect()
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
