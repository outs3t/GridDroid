#!/usr/bin/env python3
"""Calibrazione saldi: dump dei candidati saldo dal Chrome di un device.

Uso:
    python calibra_saldo.py <serial> [porta_adb]
        Dump: per ogni pagina aperta stampa URL, il risultato del lettore
        attuale e TUTTI gli elementi con un importo + path CSS.

    python calibra_saldo.py <serial> --saldo 123,45 [--site betsson]
        Auto-calibrazione: cerca i candidati il cui importo e' uguale al
        saldo dichiarato, sceglie il selettore CSS piu' stabile, lo salva
        in ~/.griddroid/site_selectors.json e verifica subito che il
        lettore lo trovi. I selettori si ricaricano a caldo nell'app.

Solo lettura sul device: non tocca Chrome, non scrive sul telefono.
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
SELS_FILE = Path.home() / ".griddroid" / "site_selectors.json"

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


def _num(text: str):
    """Normalizza un importo ('€ 1.234,56', '123.45 EUR') in float.
    Formato italiano: ',' decimale, '.' migliaia. Ritorna None se non
    parsabile."""
    t = re.sub(r"[^0-9.,]", "", text or "")
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif t.count(".") > 1 or re.search(r"\.\d{3}$", t) and not re.search(r"\.\d{1,2}$", t):
        t = t.replace(".", "")
    try:
        return float(t)
    except ValueError:
        return None


def _sel_score(cand: dict) -> float:
    """Punteggio di stabilita' del selettore: id e data-testid sono
    ancore forti, classi generate automaticamente sono deboli."""
    sel = cand["sel"]
    score = 0.0
    if cand.get("leaf"):
        score += 3
    if "#" in sel:
        score += 4
    if "data-testid" in sel:
        score += 3
    # Classi tipo css-1x2y3z4 o hash lunghi: cambiano a ogni deploy
    for seg in re.findall(r"\.([\w-]+)", sel):
        if re.search(r"(?:^|[-_])(?:[a-z0-9]{7,}|css-[a-z0-9]+)$", seg):
            score -= 2
    score -= sel.count(">") * 0.5  # path profondi = fragili
    return score


def _domain_key(host: str) -> str:
    """Chiave per site_selectors.json: dominio registrabile
    ('www.betsson.it' -> 'betsson.it', 'sports.bet365.it' -> 'bet365.it',
    'www.bookmaker.co.uk' -> 'bookmaker.co.uk')."""
    h = host.lower()
    if h.startswith("www."):
        h = h[4:]
    parts = h.split(".")
    sld2 = {"co", "com", "net", "org", "ac", "gov"}
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in sld2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


async def dump(serial: str, adb_port: int, want_saldo: str = None,
               site_filter: str = None) -> int:
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
        return 1
    targets = json.loads(body[1].decode("utf-8", errors="replace"))
    pages = [
        t for t in targets
        if t.get("type") == "page" and t.get("url", "").startswith("http")
        and t.get("webSocketDebuggerUrl")
    ]
    if site_filter:
        pages = [
            p for p in pages
            if site_filter.lower() in p.get("url", "").lower()
        ]
    print(f"{len(pages)} pagine web in Chrome su {serial}\n")

    want_num = _num(want_saldo) if want_saldo else None
    matches = []  # (score, site, cand, page_url)

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
            if want_num is not None:
                if _num(c["val"]) is None or abs(_num(c["val"]) - want_num) > 0.011:
                    continue
                matches.append((_sel_score(c), info["site"], c, info["url"]))
            mark = " *" if c["leaf"] else "  "
            print(f"   {mark} {c['val']!r:>18}  {c['sel']}")
            print(f"      testo: {c['text']!r}")
        print()

    if want_num is None:
        return 0

    if not matches:
        print(f"Nessun candidato con importo {want_saldo!r} — il saldo e' "
              f"visibile sulla pagina aperta? Prova il dump senza --saldo.")
        return 1

    matches.sort(key=lambda m: -m[0])
    score, site, best, url = matches[0]
    key = _domain_key(site)
    print(f"MATCH migliore su {site} (score {score:.1f}):")
    print(f"    sel  : {best['sel']}")
    print(f"    val  : {best['val']!r}  testo: {best['text']!r}")
    for s, st, c, u in matches[1:4]:
        print(f"    altro su {st} (score {s:.1f}): {c['sel']}")

    # Il lettore JS usa querySelector: il path 'a > b > c' e' valido come
    # selettore CSS discendente — salviamo l'ultimo segmento se ha un'id
    # (piu' corto e robusto), altrimenti il path completo.
    sel = best["sel"]
    if "#" in sel:
        sel = sel.split(">")[-1].strip()

    data = {}
    try:
        data = json.loads(SELS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    data[key] = sel
    SELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SELS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nSalvato in {SELS_FILE}:")
    print(f'    "{key}": "{sel}"')
    print("Il lettore lo ricarica a caldo: prossima lettura saldi lo usa.")
    return 0


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    want_saldo = None
    site_filter = None
    for i, a in enumerate(sys.argv):
        if a == "--saldo" and i + 1 < len(sys.argv):
            want_saldo = sys.argv[i + 1]
        elif a == "--site" and i + 1 < len(sys.argv):
            site_filter = sys.argv[i + 1]
    if not args:
        print(__doc__)
        sys.exit(1)
    serial = args[0]
    adb_port = int(args[1]) if len(args) > 1 else 5037
    sys.exit(asyncio.run(dump(serial, adb_port, want_saldo, site_filter)))


if __name__ == "__main__":
    main()
