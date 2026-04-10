import asyncio
import json
import uuid

import uvicorn
import websockets
# Import your custom framework library
from zonzaio import Application, post, query

# ---------------------------------------------------------
# Shared State
# ---------------------------------------------------------
# Maps client_id -> websocket connection
active_ws_clients = {}

# Maps request_id -> asyncio.Future (pauses REST requests until WS responds)
pending_evaluations = {}


# ---------------------------------------------------------
# WebSocket Server Logic
# ---------------------------------------------------------
async def heartbeat(client_ws, client_id, every_n_sec=3):
    """Sends a ping to the browser every N seconds to keep the connection alive."""
    try:
        while True:
            await asyncio.sleep(every_n_sec)
            await client_ws.send(json.dumps({"type": "heartbeat"}))
    except websockets.ConnectionClosed:
        pass


async def handle_clients_ws(client_ws):
    """Handles incoming WebSocket connections from the Browsers."""
    client_id = str(uuid.uuid4())[:8]
    active_ws_clients[client_id] = client_ws

    print(f"[WS] Browser connected: {client_id}")
    task_hb = asyncio.create_task(heartbeat(client_ws, client_id))

    try:
        async for msg in client_ws:
            parsed = json.loads(msg)
            msg_id = parsed.get("id")

            if msg_id in pending_evaluations:
                pending_evaluations[msg_id].set_result(parsed)
            else:
                print(
                    f"[WS] Unmatched message from {client_id}: {parsed}"
                    )

    except websockets.ConnectionClosed:
        pass
    finally:
        print(f"[WS] Browser disconnected: {client_id}")
        task_hb.cancel()
        active_ws_clients.pop(client_id, None)


# ---------------------------------------------------------
# REST API Endpoints
# ---------------------------------------------------------
@query('/clients')
def list_clients(request):
    """Returns a list of connected browser IDs."""
    return {"clients": list(active_ws_clients.keys())}


@post('/eval/:client_id')
async def eval_code(request, client_id, code: str):
    """Takes code via HTTP POST, sends it to the WS client, and waits for the result."""
    ws = active_ws_clients.get(client_id)
    if not ws:
        return {"error": f"Client {client_id} not found"}

    msg_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    pending_evaluations[msg_id] = future

    try:
        await ws.send(json.dumps({"id": msg_id, "code": code}))
    except Exception as e:
        pending_evaluations.pop(msg_id, None)
        return {"error": "Failed to send to browser", "details": str(e)}

    try:
        result = await asyncio.wait_for(future, timeout=10.0)
        return {"status": "success", "result": result}
    except asyncio.TimeoutError:
        return {
            "status": "error",
            "error": "Browser evaluation timed out"
            }
    finally:
        pending_evaluations.pop(msg_id, None)


# ---------------------------------------------------------
# Application Initialization & Main Runner
# ---------------------------------------------------------
# Initialize the Bobo-style ASGI App
app = Application.from_module(__name__)


async def main():
    ws_server = websockets.serve(handle_clients_ws, '127.0.0.1', 5757)

    config = uvicorn.Config(
        app=app, host="127.0.0.1", port=8000, log_level="error"
        )
    rest_server = uvicorn.Server(config)

    print("=========================================")
    print("🚀 Bridge Server Running")
    print("📡 WebSocket Server : ws://127.0.0.1:5757")
    print("🌍 REST API Server  : http://127.0.0.1:8000")
    print("=========================================\n")

    await asyncio.gather(ws_server, rest_server.serve())


if __name__ == '__main__':
    asyncio.run(main())
