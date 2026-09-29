import asyncio, json, os
from fastapi import APIRouter, WebSocket
from fastapi.responses import FileResponse
from mock_provider import MockProvider

TOKEN = os.getenv("PUMPPULSE_TOKEN", "change-me")
HERE = os.path.dirname(os.path.abspath(__file__))

provider = MockProvider()   # <- Stage 3: swap this line for a live provider
router = APIRouter()


@router.get("/terminal")
def terminal_page():
    return FileResponse(os.path.join(HERE, "terminal.html"))


@router.websocket("/ws")
async def ws(sock: WebSocket):
    if sock.query_params.get("token") != TOKEN:
        await sock.close(code=4401)
        return
    await sock.accept()
    await provider.start()
    q = provider.subscribe()
    await sock.send_text(json.dumps({"t": "snapshot", **provider.snapshot()}))

    async def reader():
        while True:
            msg = json.loads(await sock.receive_text())
            if msg.get("op") == "history":
                await sock.send_text(json.dumps({
                    "t": "history", "m": msg["m"], "tf": msg["tf"],
                    "candles": provider.history(msg["m"], msg["tf"])}))

    async def writer():
        while True:
            await sock.send_text(json.dumps(await q.get(), separators=(",", ":")))

    tasks = [asyncio.create_task(reader()), asyncio.create_task(writer())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        provider.unsubscribe(q)
