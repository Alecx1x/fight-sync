"""Minimal obs-websocket v5 client — FightSync's live-control spine (OBS 28+, ws on :4455).

OBS does the real-time compositing + RTMP; FightSync is the brain that drives it: switch scenes,
configure the platform, start/stop the stream, read status. Later phases add the live tracking
overlay + automatic between-round graphics on top of this same connection.

Protocol (v5): connect → server Hello(op0) [+ auth challenge] → client Identify(op1) → Identified(op2)
→ Request(op6) → RequestResponse(op7). Auth = base64(sha256( base64(sha256(pw+salt)) + challenge )).
"""
import asyncio
import base64
import hashlib
import json

import websockets

# Platform RTMP ingests (persistent stream keys pasted from each platform's dashboard).
RTMP = {
    "twitch": "rtmp://live.twitch.tv/app",
    "youtube": "rtmp://a.rtmp.youtube.com/live2",
}


class OBSError(Exception):
    pass


async def _identify(ws, password, timeout):
    hello = json.loads(await asyncio.wait_for(ws.recv(), timeout))   # op 0 Hello
    d = hello.get("d", {})
    ident = {"op": 1, "d": {"rpcVersion": d.get("rpcVersion", 1)}}
    auth = d.get("authentication")
    if auth:
        if not password:
            raise OBSError("OBS's websocket has a password set, but FightSync has none saved. "
                           "Paste the OBS websocket password in the Live tab.")
        secret = base64.b64encode(
            hashlib.sha256((password + auth["salt"]).encode()).digest()).decode()
        resp = base64.b64encode(
            hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        ident["d"]["authentication"] = resp
    await ws.send(json.dumps(ident))
    idd = json.loads(await asyncio.wait_for(ws.recv(), timeout))      # op 2 Identified
    if idd.get("op") != 2:
        raise OBSError(f"OBS rejected the connection (bad password?): {idd}")


async def call(requests, host="localhost", port=4455, password="", timeout=6.0):
    """Connect, identify, run [(requestType, requestData), …] → list of responseData dicts.
    A single (type, data) tuple is fine too. Raises OBSError with a human message on any failure."""
    if isinstance(requests, tuple):
        requests = [requests]
    out = []
    try:
        async with websockets.connect(f"ws://{host}:{port}", open_timeout=timeout,
                                      close_timeout=2, max_size=4_000_000) as ws:
            await _identify(ws, password, timeout)
            for i, (rtype, rdata) in enumerate(requests):
                rid = f"r{i}"
                await ws.send(json.dumps({"op": 6, "d": {
                    "requestType": rtype, "requestId": rid, "requestData": rdata or {}}}))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                    if msg.get("op") == 7 and msg["d"].get("requestId") == rid:
                        st = msg["d"].get("requestStatus", {})
                        if not st.get("result"):
                            raise OBSError(f"{rtype} failed: {st.get('comment') or st.get('code')}")
                        out.append(msg["d"].get("responseData") or {})
                        break
    except OBSError:
        raise
    except (OSError, ConnectionError, asyncio.TimeoutError, websockets.WebSocketException) as e:
        raise OBSError(f"Can't reach OBS on {host}:{port} — is OBS open with the WebSocket server "
                       f"enabled (Tools → WebSocket Server Settings)? ({type(e).__name__})") from e
    return out


async def status(host="localhost", port=4455, password="", timeout=2.5):
    # short timeout so the Live tab resolves "OBS not connected" fast when OBS/ws is off
    ver, scenes, stream = await call(
        [("GetVersion", {}), ("GetSceneList", {}), ("GetStreamStatus", {})],
        host, port, password, timeout)
    return {
        "connected": True,
        "obs_version": ver.get("obsVersion"),
        # OBS returns scenes top-of-list = top-of-UI (reverse of display order); flip for readability
        "scenes": [s["sceneName"] for s in reversed(scenes.get("scenes", []))],
        "current_scene": scenes.get("currentProgramSceneName"),
        "streaming": bool(stream.get("outputActive")),
    }


async def set_scene(name, host="localhost", port=4455, password=""):
    await call(("SetCurrentProgramScene", {"sceneName": name}), host, port, password)


async def configure(platform, key, host="localhost", port=4455, password=""):
    """Point OBS at a platform's RTMP ingest with the given stream key (custom service)."""
    server = RTMP.get(platform)
    if not server:
        raise OBSError(f"Unknown platform '{platform}'.")
    if not key:
        raise OBSError(f"No {platform} stream key saved.")
    await call(("SetStreamServiceSettings", {
        "streamServiceType": "rtmp_custom",
        "streamServiceSettings": {"server": server, "key": key},
    }), host, port, password)


async def start_stream(host="localhost", port=4455, password=""):
    await call(("StartStream", {}), host, port, password)


async def stop_stream(host="localhost", port=4455, password=""):
    await call(("StopStream", {}), host, port, password)
