import asyncio
import json
import time
import websockets

HOST = "ws://127.0.0.1:8470"
SERIAL = "RF8NA06233B"

async def logs():
    try:
        async with websockets.connect(f"{HOST}/ws/logs") as ws:
            print("--- logs connesso ---")
            t0 = time.time()
            while time.time() - t0 < 6:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    data = json.loads(msg)
                    for e in data:
                        print(f"{e['ts']:.1f} [{e.get('serial','-')}] {e['level']}: {e['message']}")
                except asyncio.TimeoutError:
                    pass
    except Exception as exc:
        print("logs error:", exc)

async def stream():
    try:
        async with websockets.connect(f"{HOST}/ws/stream/{SERIAL}") as ws:
            print(f"--- stream {SERIAL} connesso ---")
            total = 0
            count = 0
            first = None
            t0 = time.time()
            while time.time() - t0 < 6:
                data = await asyncio.wait_for(ws.recv(), timeout=2.0)
                if data:
                    total += len(data)
                    count += 1
                    if first is None:
                        first = (len(data), data[:12].hex())
                        print("primo pacchetto", first)
                    if total > 5000:
                        break
            print("totale byte", total, "pacchetti", count, "primo", first)
    except Exception as exc:
        print("stream error:", exc)

async def main():
    await logs()
    await stream()

asyncio.run(main())
