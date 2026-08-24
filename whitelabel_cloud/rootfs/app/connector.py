#!/usr/bin/env python3
"""White-Label Cloud Connector — device side (runs ON the gateway as a
Supervisor add-on).

Maintains ONE persistent OUTBOUND WebSocket to your cloud relay. Because it's
outbound, it works behind any home NAT/router without port forwarding. The
cloud multiplexes App sessions over this tunnel; each App session opens a
local WebSocket to Home Assistant Core and the frames are relayed
transparently in both directions.

  cloud relay <--outbound tunnel-- this connector
       |
       +-- "open  channel c1" --> connector opens ws://HA:8123/api/websocket
       +-- "data  channel c1" --> connector forwards payload to that local HA WS
       <-- "data  channel c1" --  connector forwards HA frames back up the tunnel
       +-- "close channel c1" --> connector closes the local HA WS

The connector holds NO Home Assistant credentials: the App's `auth` message
(with its long-lived token) is relayed straight to HA, which validates it.
HA's web UI is never exposed; only the API/WebSocket port is reached locally.
"""

import asyncio
import json
import os
import sys

from aiohttp import ClientSession, WSMsgType

CLOUD_URL = os.environ.get("CLOUD_URL", "ws://127.0.0.1:9000/device-tunnel")
DEVICE_ID = os.environ.get("DEVICE_ID", "DEV-DEMO-001")
DEVICE_TOKEN = os.environ.get("DEVICE_TOKEN", "devtoken")

# Home Assistant Core is reached from a Supervisor add-on via this official
# DNS name. Override for local testing only.
HA_HOST = os.environ.get("HA_HOST", "homeassistant.local.hass.io")
HA_PORT = int(os.environ.get("HA_PORT", "8123"))

RECONNECT_DELAY = 5


class _LocalChannel:
    """One App session's local HA connection + the queue feeding it."""

    def __init__(self, ha_ws):
        self.ha_ws = ha_ws
        self.q: asyncio.Queue = asyncio.Queue()


# channel_id -> _LocalChannel (owned by the local reader task for that channel)
_channels: dict[str, _LocalChannel] = {}


async def _open_local(session: ClientSession, channel: str, tunnel_ws, tunnel_lock):
    """Open a local HA WebSocket for one App session and relay frames both ways.

    The local HA ws is owned by this task (aiohttp ws sends must come from one
    task), so a per-channel queue lets the tunnel reader hand App->HA payloads
    down to HA here.
    """
    ha_url = f"ws://{HA_HOST}:{HA_PORT}/api/websocket"
    print(f"[connector] open local HA WS for channel {channel}")

    async def tunnel_send(obj: dict):
        try:
            async with tunnel_lock:
                await tunnel_ws.send_str(json.dumps(obj))
        except Exception as e:  # noqa: BLE001
            print(f"[connector] tunnel send error: {e}")

    try:
        async with session.ws_connect(ha_url, heartbeat=30) as ha_ws:
            lc = _LocalChannel(ha_ws)
            _channels[channel] = lc

            async def ha_to_tunnel():
                """HA frames up the tunnel to the App."""
                async for msg in ha_ws:
                    if msg.type == WSMsgType.TEXT:
                        await tunnel_send({"type": "data", "channel": channel, "payload": msg.data})
                    elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        break

            async def app_to_ha():
                """Queued App frames down to local HA."""
                while True:
                    payload = await lc.q.get()
                    if payload is None:  # close signal
                        break
                    await ha_ws.send_str(payload)

            await asyncio.gather(ha_to_tunnel(), app_to_ha())
    except Exception as e:  # noqa: BLE001
        print(f"[connector] local HA WS {channel} error: {e}")
    finally:
        _channels.pop(channel, None)
        await tunnel_send({"type": "channel_closed", "channel": channel})
        print(f"[connector] closed local HA WS for channel {channel}")


async def _tunnel_loop(session: ClientSession):
    """One lifecycle of the outbound cloud tunnel."""
    async with session.ws_connect(CLOUD_URL, heartbeat=30) as ws:
        await ws.send_str(json.dumps({
            "type": "hello",
            "device_id": DEVICE_ID,
            "device_token": DEVICE_TOKEN,
        }))

        tunnel_lock = asyncio.Lock()
        local_tasks: dict[str, asyncio.Task] = {}

        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                mtype = data.get("type")

                if mtype == "hello_ok":
                    print(f"[connector] connected to cloud as {DEVICE_ID}")

                elif mtype == "auth_failed":
                    print(f"[connector] cloud rejected device credentials: "
                          f"{data.get('message')}")
                    await ws.close()
                    return

                elif mtype == "open":
                    ch = data.get("channel")
                    local_tasks[ch] = asyncio.create_task(
                        _open_local(session, ch, ws, tunnel_lock)
                    )

                elif mtype == "data":
                    ch = data.get("channel")
                    lc = _channels.get(ch)
                    if lc:
                        await lc.q.put(data.get("payload"))

                elif mtype == "close":
                    ch = data.get("channel")
                    lc = _channels.pop(ch, None)
                    if lc:
                        await lc.q.put(None)

                elif mtype == "pong":
                    pass

            elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                break

        # Tunnel dropped: stop all local channels and wait for cleanup.
        for lc in list(_channels.values()):
            await lc.q.put(None)
        _channels.clear()
        for t in local_tasks.values():
            t.cancel()
        await asyncio.gather(*local_tasks.values(), return_exceptions=True)


async def run():
    """Outer loop: connect to the cloud, reconnect with backoff on failure."""
    async with ClientSession() as session:
        while True:
            try:
                await _tunnel_loop(session)
            except Exception as e:  # noqa: BLE001
                print(f"[connector] tunnel dropped: {e}")
            print(f"[connector] reconnecting in {RECONNECT_DELAY}s...")
            await asyncio.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)
