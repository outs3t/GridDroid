import asyncio
import json
import time
import websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8470/ws") as ws:
        print("--- ws connesso ---")
        t0 = time.time()
        while time.time() - t0 < 25:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                data = json.loads(msg)
                if data.get("type") == "log":
                    e = data["data"]
                    print(f"{e['ts']:.2f} [{e.get('serial','-')}] {e['level']}: {e['message']}")
            except asyncio.TimeoutError:
                pass

asyncio.run(main())
