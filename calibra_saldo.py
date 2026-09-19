#!/usr/bin/env python3
"""Calibrazione saldi: dump dei candidati saldo dal Chrome di un device.

Uso:
    python calibra_saldo.py <serial> [porta_adb]

Per ogni pagina web aperta in Chrome stampa:
- URL e titolo
- il risultato del lettore attuale (_CDP_JS)
- TUTTI gli elementi che contengono un importo, con il loro path CSS:
  da qui si sceglie il selettore stabile da salvare in
  ~/.griddroid/site_selectors.json

Solo lettura: non tocca il device, non scrive nulla.
"""

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

ADB = REPO / "tools" / "adb.exe"

# JS di dump: per ogni elemento con un importo visibile restituisce il
# path CSS (tag#id / tag.classe), il testo e l'importo estratto. Serve a
# scegliere IL selettore stabile del saldo per quel sito.
_DUMP_JS = r"""
(() => {
  const money = /(?:€|EUR|USD|\$|£)\s*[0-9][0-9.,\s]*[0-9]|[0-9][0-9.,]*[0-9]\s*(?:€|EUR|USD|\$|£)/i;
  const vis = el => {
    const it = (el.innerText || '').trim();
    if (it) return it;
    const visible = el.checkVisibility ? el.checkVisibility() : el.offsetParent !== null;
    return visible ? (el.textContent || '').trim() : '';
  };
  const path = el => {
    const p = [];
    while (el && el.nodeType === 1 && p.length < 7) {
      let s = el.tagName.toLowerCase();
      if (el.id) { s += '#' + el.id; p.unshift(s); break; }
      const cn = (typeof el.className === 'string' ? el.className : '').trim();
      if (cn) s += '.' + cn.split(/\s+/).slice(0, 3).join('.');
      const dt = el.getAttribute && el.getAttribute('data-testid');
      if (dt) s += `[data-testid="${dt}"]`;
      p.unshift(s);
      el = el.parentElement;
    }
    return p.join(' > ');
  };
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    const t = vis(el);
    if (!t || t.length > 80) continue;
    const m = t.match(money);
    if (!m) continue;
    out.push({
      sel: path(el),
      text: t.slice(0, 80),
      val: m[0],
      leaf: !el.children.length,
    });
    if (out.length >= 50) break;
  }
  return {
    site: location.hostname,
    url: location.href.slice(0, 120),
    title: document.title.slice(0, 60),
    vis: document.visibilityState,
    cands: out,
  };
})()
"""


async def _eval(ws, expr, msg_id=1, timeout=8.0):
    await ws.send(json.dumps({
        "id": msg_id,
        "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True},
    }))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if msg.get("id") == msg_id:
            return msg.get("result", {}).get("result", {}).get("value")


async def dump(serial: str, adb_port: int) -> None:
    import websockets
    from griddroid.adb_manager import AdbManager

    cdp_port = 39999
    subprocess.run(
        [str(ADB), "-P", str(adb_port), "-s", serial, "forward",
         f"tcp:{cdp_port}", "localabstract:chrome_devtools_remote"],
        capture_output=True, timeout=10,
    )
    host = f"127.0.0.1:{cdp_port}"
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", cdp_port), timeout=5.0
    )
    try:
        writer.write(
            f"GET /json HTTP/1.1\r\nHost: {host}\r\n"
            f"Connection: close\r\n\r\n".encode()
        )
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(1 << 20), timeout=5.0)
    finally:
        writer.close()
    body = raw.split(b"\r\n\r\n", 1)
    if len(body) < 2:
        print("Nessuna risposta da Chrome (chrome aperto sul device?)")
        return
    targets = json.loads(body[1].decode("utf-8", errors="replace"))
    pages = [
        t for t in targets
        if t.get("type") == "page" and t.get("url", "").startswith("http")
        and t.get("webSocketDebuggerUrl")
    ]
    print(f"{len(pages)} pagine web in Chrome su {serial}\n")
    for i, page in enumerate(pages):
        ws_url = re.sub(
            r"^ws://[^/]+", f"ws://127.0.0.1:{cdp_port}",
            page["webSocketDebuggerUrl"],
        )
        try:
            async with websockets.connect(
                ws_url, open_timeout=4, close_timeout=1, max_size=2**22,
            ) as ws:
                info = await _eval(ws, _DUMP_JS)
                lettore = await _eval(ws, AdbManager._CDP_JS, msg_id=2)
        except Exception as exc:
            print(f"--- [{i}] {page.get('url', '')[:80]}  (eval fallita: {exc})")
            continue
        if not info:
            print(f"--- [{i}] {page.get('url', '')[:80]}  (nessun dato)")
            continue
        print(f"=== [{i}] {info['site']}  [{info['vis']}]")
        print(f"    {info['url']}")
        print(f"    title: {info['title']}")
        if lettore:
            print(
                f"    LETTORE ATTUALE -> saldo={lettore.get('saldo')!r} "
                f"lo={lettore.get('lo')} user={lettore.get('user')!r}"
            )
        for c in info["cands"]:
            mark = " *" if c["leaf"] else "  "
            print(f"   {mark} {c['val']!r:>18}  {c['sel']}")
            print(f"      testo: {c['text']!r}")
        print()


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    serial = sys.argv[1]
    adb_port = int(sys.argv[2]) if len(sys.argv) > 2 else 5037
    asyncio.run(dump(serial, adb_port))


if __name__ == "__main__":
    main()
