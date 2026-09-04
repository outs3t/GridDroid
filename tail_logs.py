import asyncio
import json
import time
import websockets

async def main():
    url = "ws://127.0.0.1:8470/ws/logs"
    origin = "http://127.0.0.1:8470"
    async with websockets.connect(url, origin=origin) as ws:
        t0 = time.time()
        while time.time() - t0 < 20:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                data = json.loads(msg)
                for e in data:
                    print(f"{e.get('ts','-'):.3f} [{e.get('serial','-')}] {e['level']}: {e['message']}")
            except asyncio.TimeoutError:
                pass

asyncio.run(main())
