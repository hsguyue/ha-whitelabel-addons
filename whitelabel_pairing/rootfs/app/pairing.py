"""White-Label Pairing add-on.

Exposes a small HTTP endpoint on the gateway's LAN. The branded App POSTs a
pairing code (printed on the device). If valid, this add-on authenticates to
Home Assistant as the master user and mints a fresh long-lived access token
for that App via the WebSocket command `auth/long_lived_access_token`.

Flow:
    App  ->  POST /pair {"pairing_code":"...", "client_name":"MyApp"}
    Add-on -> connects to HA WebSocket, auths with master_token,
              calls auth/long_lived_access_token
    Add-on -> returns {"access_token":"...", "expires_in_days": 3650}

The App then talks to HA through the White-Label API Proxy (port 8080)
using that token. End users never see a Home Assistant screen.

Endpoints:
    POST /pair    issue a token (requires pairing_code)
    GET  /health  liveness + whether master_token is configured
"""

import asyncio
import json
import os
import time

from aiohttp import ClientError, ClientSession, web

# Home Assistant Core is reached from a Supervisor add-on via this official
# DNS name. Overridable via env for local testing only.
HA_HOST = os.environ.get("HA_HOST", "homeassistant.local.hass.io")
HA_PORT = int(os.environ.get("HA_PORT", "8123"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8099"))

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# Loaded at startup (from the Supervisor API, or from env vars when testing).
CONFIG = {
    "pairing_code": os.environ.get("PAIRING_CODE", ""),
    "master_token": os.environ.get("MASTER_TOKEN", ""),
    "token_lifespan_days": int(os.environ.get("TOKEN_LIFESPAN_DAYS", "3650")),
}

# Minimal per-IP abuse protection.
_attempts: dict[str, list] = {}


async def load_options() -> None:
    """Read this add-on's options from the Supervisor API.

    `GET /addons/self/info` returns the caller's own options un-redacted.
    """
    if not SUPERVISOR_TOKEN:
        print("No SUPERVISOR_TOKEN — using env-var config (test/off-OS mode).")
        return

    headers = {"X-Supervisor-Token": SUPERVISOR_TOKEN}
    url = "http://supervisor/addons/self/info"
    try:
        async with ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                data = await resp.json()
        opts = data.get("data", {}).get("options", {})
        CONFIG["pairing_code"] = opts.get("pairing_code", CONFIG["pairing_code"])
        CONFIG["master_token"] = opts.get("master_token", CONFIG["master_token"])
        CONFIG["token_lifespan_days"] = int(
            opts.get("token_lifespan_days", CONFIG["token_lifespan_days"])
        )
    except Exception as e:  # noqa: BLE001
        print(f"ERROR loading options from Supervisor: {e}")
        return

    print(
        "Loaded options: "
        f"pairing_code={'<set>' if CONFIG['pairing_code'] else '<empty>'}, "
        f"master_token={'<set>' if CONFIG['master_token'] else '<empty>'}, "
        f"lifespan={CONFIG['token_lifespan_days']}d"
    )


async def issue_token(client_name: str) -> str | None:
    """Authenticate to HA as the master user and mint a long-lived token."""
    url = f"ws://{HA_HOST}:{HA_PORT}/api/websocket"
    try:
        async with ClientSession() as session:
            async with session.ws_connect(url, timeout=10) as ws:
                hello = await ws.receive(timeout=10)
                if json.loads(hello.data).get("type") != "auth_required":
                    print(f"Unexpected hello: {hello.data}")
                    return None

                await ws.send_json({"type": "auth", "access_token": CONFIG["master_token"]})
                auth = json.loads((await ws.receive(timeout=10)).data)
                if auth.get("type") != "auth_ok":
                    print(f"HA master auth failed: {auth}")
                    return None

                await ws.send_json(
                    {
                        "id": 1,
                        "type": "auth/long_lived_access_token",
                        "client_name": client_name,
                        "lifespan": CONFIG["token_lifespan_days"],
                    }
                )
                result = json.loads((await ws.receive(timeout=15)).data)
                if result.get("success"):
                    return result.get("result")
                print(f"Token issue failed: {result}")
                return None
    except (ClientError, asyncio.TimeoutError) as e:
        print(f"HA connection error: {e}")
        return None


def check_rate_limit(ip: str) -> bool:
    now = time.time()
    rec = _attempts.get(ip)
    if rec and now - rec[1] < 60 and rec[0] >= 5:
        return False
    if not rec or now - rec[1] > 60:
        _attempts[ip] = [1, now]
    else:
        rec[0] += 1
    return True


async def handle_pair(request: web.Request) -> web.Response:
    ip = request.remote or "unknown"
    if not check_rate_limit(ip):
        return web.json_response({"error": "too many attempts, wait a minute"}, status=429)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    if body.get("pairing_code") != CONFIG["pairing_code"]:
        return web.json_response({"error": "invalid pairing code"}, status=401)
    if not CONFIG["master_token"]:
        return web.json_response({"error": "master token not configured"}, status=500)

    client_name = body.get("client_name", "White-Label App")
    token = await issue_token(client_name)
    if not token:
        return web.json_response({"error": "could not issue token"}, status=502)

    return web.json_response(
        {"access_token": token, "expires_in_days": CONFIG["token_lifespan_days"]}
    )


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response(
        {"status": "ok", "master_token_set": bool(CONFIG["master_token"])}
    )


async def main() -> None:
    await load_options()
    app = web.Application()
    app.router.add_post("/pair", handle_pair)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", LISTEN_PORT)
    await site.start()
    print(f"Pairing server listening on :{LISTEN_PORT}  (POST /pair, GET /health)")
    await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
