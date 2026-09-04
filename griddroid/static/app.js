/* GridDroid – Frontend Application */

// =====================================================================
// Stato globale
// =====================================================================

const state = {
    devices: [],
    broadcastMode: false,
    focusedSerial: null,
    fullscreenSerial: null,
    logCount: 0,
    ws: null,
    gridCols: 15,
    gridGap: 14,
    feedZoom: 1.0,
    sortBy: false,
    searchText: "",
    searchMode: "name",
};

// =====================================================================
// WebSocket
// =====================================================================

function connectWebSocket() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws`;
    const ws = new WebSocket(url);
    state.ws = ws;

    ws.onopen = () => {
        console.log("WebSocket connesso");
    };

    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            if (msg.type === "devices") {
                state.devices = msg.data;
                state.broadcastMode = msg.broadcast;
                state.focusedSerial = msg.focused;
                renderGrid();
                updateHeader();
            } else if (msg.type === "log") {
                appendLog(msg.data);
            }
        } catch (e) {
            console.error("WS parse error:", e);
        }
    };

    ws.onclose = () => {
        console.log("WebSocket disconnesso, riconnessione tra 2s...");
        setTimeout(connectWebSocket, 2000);
    };

    ws.onerror = () => {
        ws.close();
    };
}

function wsSend(obj) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify(obj));
    }
}

// =====================================================================
// Rendering Griglia
// =====================================================================

function renderGrid() {
    const grid = document.getElementById("deviceGrid");
    let devices = [...state.devices];

    // Filtro per nome o gruppo
    const q = state.searchText.trim().toLowerCase();
    if (q) {
        devices = devices.filter((dev) => {
            if (state.searchMode === "group") {
                return (dev.tags || []).some((t) => t.toLowerCase().includes(q));
            }
            return (dev.display_name || "").toLowerCase().includes(q);
        });
    }

    devices.sort((a, b) => {
        // Online sempre in cima
        const aOnline = a.status === "online";
        const bOnline = b.status === "online";
        if (aOnline && !bOnline) return -1;
        if (bOnline && !aOnline) return 1;
        if (state.sortBy) return (a.display_name || "").localeCompare(b.display_name || "");
        return 0;
    });

    // Nascondi i dispositivi segnati come "giocati"
    devices = devices.filter((dev) => !dev.played);

    // Aggiorna colonne CSS in base a zoom e larghezza container
    updateGridColumns();

    // Costruisci o aggiorna le celle
    const existingCells = grid.querySelectorAll(".device-cell");
    const existingMap = {};
    existingCells.forEach((cell) => {
        existingMap[cell.dataset.serial] = cell;
    });

    const seenSerials = new Set();

    const activeCard = document.activeElement?.closest(".device-card");

    devices.forEach((dev) => {
        seenSerials.add(dev.serial);
        let cell = existingMap[dev.serial];

        if (!cell) {
            cell = createDeviceCell(dev);
            const card = wrapDeviceCard(cell, dev);
            grid.appendChild(card);
        } else {
            // Riordina il DOM solo se non stiamo editando un input nella card
            const card = cell.parentElement;
            if (card !== activeCard) {
                grid.appendChild(card);
            }
        }

        updateDeviceCell(cell, dev);
    });

    // Rimuovi celle di dispositivi non più presenti o giocati
    existingCells.forEach((cell) => {
        if (!seenSerials.has(cell.dataset.serial)) {
            const card = cell.parentElement;
            if (card === activeCard) {
                // Non rimuovere la card che stiamo editando
                seenSerials.add(cell.dataset.serial);
                return;
            }
            const feed = cell.querySelector(".device-feed");
            if (feed) stopStreamWs(feed);
            card?.remove();
        }
    });

    renderGroups();
    renderAssignDevice();
    renderPhoneSelection();
}

function createDeviceCell(dev) {
    const cell = document.createElement("div");
    cell.className = "device-cell";
    cell.dataset.serial = dev.serial;

    cell.innerHTML = `
        <input type="checkbox" class="device-select" title="Seleziona per broadcast" />
        <canvas class="device-feed" style="display:none"></canvas>
        <div class="device-feed-placeholder">
            <div class="icon">📱</div>
            <span>Nessuno stream</span>
        </div>
        <div class="device-toolbar">
            <div class="toolbar-left">
                <button class="toolbar-btn" data-action="screenshot" title="Screenshot">📷</button>
                <button class="toolbar-btn" data-action="screen_toggle" title="Accendi/Spegni schermo">💡</button>
            </div>
            <div class="toolbar-center">
                <button class="toolbar-btn nav-btn" data-action="recent_apps" title="App recenti">▣</button>
                <button class="toolbar-btn nav-btn" data-action="home" title="Home">⌂</button>
                <button class="toolbar-btn nav-btn" data-action="back" title="Indietro">←</button>
            </div>
            <div class="toolbar-right">
                <button class="toolbar-btn" data-action="rotate" title="Rotazione">🔄</button>
                <button class="toolbar-btn" data-action="fullscreen" title="Schermo intero">⛶</button>
                <button class="toolbar-btn" data-action="stream_toggle" title="Avvia/Ferma stream">▶</button>
            </div>
        </div>
    `;

    // Click sul feed → focus
    cell.addEventListener("click", (e) => {
        if (e.target.closest(".toolbar-btn") || e.target.closest(".device-select")) return;
        wsSend({ action: "focus", serial: dev.serial });
    });

    // Checkbox selezione
    const checkbox = cell.querySelector(".device-select");
    checkbox.addEventListener("change", () => {
        wsSend({ action: "select", serial: dev.serial, selected: checkbox.checked });
    });

    // Toolbar actions
    cell.querySelectorAll(".toolbar-btn").forEach((btn) => {
        btn.addEventListener("click", (e) => {
            e.stopPropagation();
            handleToolbarAction(btn.dataset.action, dev.serial, cell);
        });
    });

    // Input relay: tap e swipe sul feed
    const feed = cell.querySelector(".device-feed");
    setupInputHandlers(feed, dev.serial);

    return cell;
}

function getDeviceStatusLabel(status) {
    const map = {
        online: "ONLINE",
        offline: "OFFLINE",
        unauthorized: "NON AUTORIZZATO",
        disconnected: "DISCONNESSO",
    };
    return map[status] || (status || "").toUpperCase();
}

function wrapDeviceCard(cell, dev) {
    const card = document.createElement("div");
    card.className = "device-card";

    const label = document.createElement("div");
    label.className = "device-label";
    label.innerHTML = `
        <div class="device-label-row">
            <input type="text" class="device-name" spellcheck="false" title="Clicca per rinominare" value="${escapeHtml(dev.display_name)}" />
            <span class="status-dot ${dev.status}"></span>
            <span class="device-status-label status-${dev.status}">${escapeHtml(getDeviceStatusLabel(dev.status))}</span>
        </div>
        <div class="device-tags"></div>
    `;

    const nameEl = label.querySelector(".device-name");
    nameEl.addEventListener("blur", () => {
        const newLabel = nameEl.value.trim();
        if (newLabel !== dev.display_name) {
            wsSend({ action: "label", serial: dev.serial, label: newLabel });
        }
    });
    nameEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            nameEl.blur();
        }
    });

    card.appendChild(label);
    card.appendChild(cell);

    // Tasto destro sul telefono → segna come giocato (con conferma)
    card.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        if (window.confirm(`Segnare "${dev.display_name || dev.serial}" come giocato?`)) {
            wsSend({ action: "set_played", serial: dev.serial, played: true });
        }
    });

    return card;
}

function updateDeviceCell(cell, dev) {
    const card = cell.parentElement;

    // Nome
    const nameEl = card?.querySelector(".device-name");
    if (nameEl && nameEl !== document.activeElement) {
        nameEl.value = dev.display_name;
    }

    // Stato
    const dot = card?.querySelector(".status-dot");
    if (dot) dot.className = `status-dot ${dev.status}`;
    const statusLabel = card?.querySelector(".device-status-label");
    if (statusLabel && statusLabel !== document.activeElement) {
        statusLabel.textContent = getDeviceStatusLabel(dev.status);
        statusLabel.className = `device-status-label status-${dev.status}`;
    }

    // Gruppi / tag
    const tagsEl = card?.querySelector(".device-tags");
    if (tagsEl) {
        const tags = dev.tags || [];
        tagsEl.dataset.tags = tags.join(",");
        tagsEl.innerHTML = tags
            .slice(0, 5)
            .map((t) => `<span class="device-tag">${escapeHtml(t)}</span>`)
            .join("");
        if (tags.length > 5) {
            tagsEl.innerHTML += `<span class="device-tag">+${tags.length - 5}</span>`;
        }
    }

    // Classi celle
    cell.classList.toggle("focused", dev.serial === state.focusedSerial);
    cell.classList.toggle("offline", dev.status !== "online");
    cell.classList.toggle("selected", state.broadcastMode && dev.selected);

    // Checkbox
    const checkbox = cell.querySelector(".device-select");
    checkbox.checked = dev.selected;

    // Feed
    const feed = cell.querySelector(".device-feed");
    const placeholder = cell.querySelector(".device-feed-placeholder");
    const placeholderText = placeholder?.querySelector("span");
    if (placeholderText) {
        if (dev.status !== "online") {
            placeholderText.textContent = dev.error || "Non collegato";
        } else if (!dev.streaming) {
            placeholderText.textContent = "Nessuno stream";
        }
    }

    if (dev.streaming) {
        // Avvia WebSocket binario per stream a latenza minima
        if (!feed.dataset.wsActive || feed.dataset.wsActive !== dev.serial) {
            startStreamWs(feed, dev.serial);
        }

        const streamBtn = cell.querySelector('[data-action="stream_toggle"]');
        if (streamBtn) streamBtn.textContent = "⏹";
    } else {
        stopStreamWs(feed);

        const streamBtn = cell.querySelector('[data-action="stream_toggle"]');
        if (streamBtn) streamBtn.textContent = "▶";
    }
}

// =====================================================================
// Stream WebSocket (latenza minima)
// =====================================================================

const streamSessions = {};

function parseSpsPpsFromAnnexB(data) {
    let sps = null, pps = null;
    let i = 0;
    while (i + 4 < data.length) {
        let scLen = 0;
        if (data[i] === 0 && data[i+1] === 0 && data[i+2] === 0 && data[i+3] === 1) scLen = 4;
        else if (data[i] === 0 && data[i+1] === 0 && data[i+2] === 1) scLen = 3;
        if (scLen > 0) {
            const nalType = data[i + scLen] & 0x1F;
            let j = i + scLen + 1;
            while (j + 2 < data.length) {
                if (data[j] === 0 && data[j+1] === 0 && (data[j+2] === 1 || (data[j+2] === 0 && j+3 < data.length && data[j+3] === 1))) break;
                j++;
            }
            if (j + 2 >= data.length) j = data.length;
            const nalData = data.subarray(i + scLen, j);
            if (nalType === 7) sps = nalData;
            else if (nalType === 8) pps = nalData;
            if (sps && pps) break;
            i = j;
        } else {
            i++;
        }
    }
    return { sps, pps };
}

function buildAvcDescription(sps, pps) {
    if (!sps || !pps) return null;
    const buf = new Uint8Array(11 + sps.length + pps.length);
    buf[0] = 1;
    buf[1] = sps[1];
    buf[2] = sps[2];
    buf[3] = sps[3];
    buf[4] = 0xFF;
    buf[5] = 0xE1;
    buf[6] = (sps.length >> 8) & 0xFF;
    buf[7] = sps.length & 0xFF;
    buf.set(sps, 8);
    buf[8 + sps.length] = 1;
    buf[9 + sps.length] = (pps.length >> 8) & 0xFF;
    buf[10 + sps.length] = pps.length & 0xFF;
    buf.set(pps, 11 + sps.length);
    return buf;
}

function annexBToAVCC(data) {
    const nalStarts = [];
    for (let i = 0; i + 3 < data.length; i++) {
        if (data[i] === 0 && data[i+1] === 0) {
            if (data[i+2] === 1) {
                nalStarts.push({ pos: i, scLen: 3 });
                i += 2;
            } else if (data[i+2] === 0 && i + 3 < data.length && data[i+3] === 1) {
                nalStarts.push({ pos: i, scLen: 4 });
                i += 3;
            }
        }
    }
    if (nalStarts.length === 0) return data;

    let totalSize = 0;
    const nals = [];
    for (let i = 0; i < nalStarts.length; i++) {
        const start = nalStarts[i].pos + nalStarts[i].scLen;
        const end = i + 1 < nalStarts.length ? nalStarts[i+1].pos : data.length;
        const nalData = data.subarray(start, end);
        const nalType = nalData[0] & 0x1F;

        // Teniamo solo i NAL video: 1 (non-IDR) e 5 (IDR)
        if (nalType === 1 || nalType === 5) {
            nals.push(nalData);
            totalSize += 4 + nalData.length;
        }
    }

    if (nals.length === 0) return null;

    const result = new Uint8Array(totalSize);
    let offset = 0;
    for (const nal of nals) {
        const len = nal.length;
        result[offset] = (len >> 24) & 0xFF;
        result[offset+1] = (len >> 16) & 0xFF;
        result[offset+2] = (len >> 8) & 0xFF;
        result[offset+3] = len & 0xFF;
        result.set(nal, offset + 4);
        offset += 4 + len;
    }
    return result;
}

function startStreamWs(feedEl, serial) {
    stopStreamWs(feedEl);

    if (typeof VideoDecoder === "undefined") {
        console.error("WebCodecs non supportato: usa Chrome/Edge 94+");
        return;
    }

    const ctx = feedEl.getContext("2d", { alpha: false, desynchronized: true });
    const placeholder = feedEl.parentElement.querySelector('.device-feed-placeholder');
    const iconEl = placeholder ? placeholder.querySelector('.icon') : null;
    const textEl = placeholder ? placeholder.querySelector('span') : null;
    function setPlaceholder(text, icon, show = true) {
        if (iconEl) iconEl.textContent = icon;
        if (textEl) textEl.textContent = text;
        if (placeholder) placeholder.style.display = show ? 'flex' : 'none';
    }
    feedEl.style.display = 'none';
    setPlaceholder('Connessione in corso...', '⏳');

    const session = { ws: null, decoder: null, gotKey: false, configured: false, ts: 0, description: null, frameCount: 0, hasFrame: false };

    const decoder = new VideoDecoder({
        output: (frame) => {
            if (feedEl.width !== frame.displayWidth || feedEl.height !== frame.displayHeight) {
                feedEl.width = frame.displayWidth;
                feedEl.height = frame.displayHeight;
                // Proporzione fissa dal CSS, indipendente dalla risoluzione
            }
            ctx.drawImage(frame, 0, 0);
            if (!session.hasFrame) {
                session.hasFrame = true;
                feedEl.style.display = 'block';
                setPlaceholder('', '', false);
            }
            frame.close();
        },
        error: (err) => {
            console.error(`Decoder ${serial}:`, err);
            session.gotKey = false;
            session.configured = false;
        },
    });
    session.decoder = decoder;

    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${protocol}//${location.host}/ws/stream/${serial}`);
    ws.binaryType = "arraybuffer";
    session.ws = ws;

    ws.onopen = () => {
        setPlaceholder('Connessione in corso...', '⏳');
    };

    ws.onmessage = (event) => {
        const data = new Uint8Array(event.data);
        if (data.length < 2) return;

        const isKey = data[0] === 1;
        const h264Data = data.subarray(1);

        if (!session.gotKey) {
            if (!isKey) return;
            session.gotKey = true;

            // Estrae SPS/PPS dal keyframe e costruisce il description record AVCC
            const { sps, pps } = parseSpsPpsFromAnnexB(h264Data);
            if (!sps || !pps) {
                console.error(`SPS/PPS non trovati nel keyframe per ${serial}`);
                session.gotKey = false;
                return;
            }
            session.description = buildAvcDescription(sps, pps);
            const codecStr = `avc1.${sps[1].toString(16).padStart(2,'0')}${sps[2].toString(16).padStart(2,'0')}${sps[3].toString(16).padStart(2,'0')}`;
            console.log(`Decoder ${serial}: codec=${codecStr} SPS=${sps.length}B PPS=${pps.length}B`);

            const configs = [
                { codec: codecStr, optimizeForLatency: true, hardwareAcceleration: "prefer-hardware", description: session.description },
                { codec: codecStr, optimizeForLatency: true, description: session.description },
                { codec: codecStr, description: session.description },
            ];
            for (const cfg of configs) {
                try {
                    decoder.configure(cfg);
                    session.configured = true;
                    console.log(`Decoder ${serial} configurato: ${cfg.hardwareAcceleration || "software"}`);
                    break;
                } catch (e) {
                    console.warn(`Config fallita (${cfg.hardwareAcceleration || "software"}):`, e.message);
                }
            }
            if (!session.configured) {
                console.error(`Impossibile configurare decoder per ${serial}`);
                session.gotKey = false;
                return;
            }
        }
        if (!session.configured || decoder.state !== "configured") return;

        session.frameCount++;
        if (session.frameCount <= 5 || session.frameCount % 100 === 0) {
            console.log(`Frame ${session.frameCount}: ${isKey ? "KEY" : "delta"} ${h264Data.length}B queue=${decoder.decodeQueueSize}`);
        }

        try {
            // Converte Annex-B → AVCC (length-prefixed), tenendo solo lo slice VCL
            const avccData = annexBToAVCC(h264Data);
            if (!avccData) {
                // Frame senza slice VCL (es. solo SPS/PPS/SEI)
                return;
            }
            decoder.decode(new EncodedVideoChunk({
                type: isKey ? "key" : "delta",
                timestamp: session.ts,
                data: avccData,
            }));
            session.ts += 33333;
        } catch (err) {
            console.error(`Decode ${serial} frame ${session.frameCount}:`, err);
            session.gotKey = false;
            session.configured = false;
        }
    };

    ws.onclose = () => {
        setPlaceholder('Connessione persa', '📵');
        if (streamSessions[serial] === session) {
            feedEl.dataset.wsActive = "";
            delete streamSessions[serial];
        }
    };

    ws.onerror = (e) => {
        console.error(`WS stream ${serial}:`, e);
        setPlaceholder('Errore connessione', '⚠️');
        ws.close();
    };

    feedEl.dataset.wsActive = serial;
    streamSessions[serial] = session;
}

function stopStreamWs(feedEl) {
    const serial = feedEl.dataset.wsActive;
    const session = serial && streamSessions[serial];
    if (session) {
        try { session.ws.close(); } catch (e) { }
        try {
            if (session.decoder.state !== "closed") session.decoder.close();
        } catch (e) { }
        delete streamSessions[serial];
    }
    feedEl.dataset.wsActive = "";
    feedEl.style.display = 'none';
    const placeholder = feedEl.parentElement.querySelector('.device-feed-placeholder');
    if (placeholder) {
        const iconEl = placeholder.querySelector('.icon');
        const textEl = placeholder.querySelector('span');
        if (iconEl) iconEl.textContent = '📱';
        if (textEl) textEl.textContent = 'Nessuno stream';
        placeholder.style.display = 'flex';
    }
}

// =====================================================================
// Input Handlers (tap, swipe, tastiera)
// =====================================================================

/**
 * Converte le coordinate del mouse in coordinate del video.
 * Il canvas usa object-fit: contain, quindi il video è centrato con bande
 * nere (letterbox): senza compensarle il tocco risulta sfalsato.
 */
function feedCoords(feedEl, ev) {
    const vw = feedEl.width, vh = feedEl.height;
    if (!vw || !vh) return null;

    const rect = feedEl.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;

    const videoAR = vw / vh;
    const boxAR = rect.width / rect.height;

    let dispW, dispH, padX = 0, padY = 0;
    if (boxAR > videoAR) {
        // Bande verticali ai lati
        dispH = rect.height;
        dispW = dispH * videoAR;
        padX = (rect.width - dispW) / 2;
    } else {
        // Bande orizzontali sopra/sotto
        dispW = rect.width;
        dispH = dispW / videoAR;
        padY = (rect.height - dispH) / 2;
    }

    const localX = ev.clientX - rect.left - padX;
    const localY = ev.clientY - rect.top - padY;

    return {
        x: Math.round(Math.max(0, Math.min(vw - 1, localX * vw / dispW))),
        y: Math.round(Math.max(0, Math.min(vh - 1, localY * vh / dispH))),
        w: vw,
        h: vh,
        inside: localX >= 0 && localX <= dispW && localY >= 0 && localY <= dispH,
    };
}

function setupInputHandlers(feedEl, serial) {
    let dragging = false;
    let pendingMove = null;
    let moveScheduled = false;

    // Invia i movimenti al massimo una volta per frame: evita di saturare
    // il WebSocket mantenendo il drag perfettamente fluido.
    function flushMove() {
        moveScheduled = false;
        if (!dragging || !pendingMove) return;
        wsSend(pendingMove);
        pendingMove = null;
    }

    feedEl.addEventListener("pointerdown", (ev) => {
        if (ev.button !== 0) return;
        const c = feedCoords(feedEl, ev);
        if (!c) return;

        ev.preventDefault();
        feedEl.setPointerCapture(ev.pointerId);
        dragging = true;

        // Il focus deve arrivare prima dell'evento: i comandi sono ordinati
        wsSend({ action: "focus", serial: serial });
        wsSend({
            action: "touch", touch_action: "down",
            x: c.x, y: c.y, w: c.w, h: c.h,
            pressure: ev.pressure > 0 ? ev.pressure : 1.0,
        });
    });

    feedEl.addEventListener("pointermove", (ev) => {
        if (!dragging) return;
        const c = feedCoords(feedEl, ev);
        if (!c) return;

        ev.preventDefault();
        pendingMove = {
            action: "touch", touch_action: "move",
            x: c.x, y: c.y, w: c.w, h: c.h,
            pressure: ev.pressure > 0 ? ev.pressure : 1.0,
        };
        if (!moveScheduled) {
            moveScheduled = true;
            requestAnimationFrame(flushMove);
        }
    });

    function endDrag(ev) {
        if (!dragging) return;
        dragging = false;
        pendingMove = null;

        const c = feedCoords(feedEl, ev);
        if (!c) return;
        wsSend({
            action: "touch", touch_action: "up",
            x: c.x, y: c.y, w: c.w, h: c.h,
        });
    }

    feedEl.addEventListener("pointerup", (ev) => {
        ev.preventDefault();
        endDrag(ev);
    });

    feedEl.addEventListener("pointercancel", endDrag);
    feedEl.addEventListener("lostpointercapture", endDrag);

    // Rotella del mouse → scroll nativo
    feedEl.addEventListener("wheel", (ev) => {
        const c = feedCoords(feedEl, ev);
        if (!c) return;
        ev.preventDefault();

        wsSend({ action: "focus", serial: serial });
        wsSend({
            action: "scroll",
            x: c.x, y: c.y, w: c.w, h: c.h,
            hscroll: Math.max(-1, Math.min(1, -ev.deltaX / 100)),
            vscroll: Math.max(-1, Math.min(1, -ev.deltaY / 100)),
        });
    }, { passive: false });

    // Il tasto destro fa da BACK, come in scrcpy
    feedEl.addEventListener("contextmenu", (ev) => {
        ev.preventDefault();
        wsSend({ action: "focus", serial: serial });
        wsSend({ action: "keyevent", keycode: 4 });
    });
}

// Tastiera globale → input text / keyevent
document.addEventListener("keydown", (e) => {
    // Ignora se il focus è su un input o textarea
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT") return;

    // Copia / incolla / taglia / seleziona tutto: Ctrl (o Cmd) + C/V/X/A
    if (e.ctrlKey || e.metaKey) {
        const k = e.key.toLowerCase();
        if (k === "c" || k === "v" || k === "x" || k === "a") {
            e.preventDefault();
            sendClipboardShortcut(k);
            return;
        }
        return;
    }

    // Mappa tasti speciali
    const keyMap = {
        "Backspace": 67,
        "Enter": 66,
        "Escape": 4,   // BACK
        "Home": 3,
        "ArrowUp": 19,
        "ArrowDown": 20,
        "ArrowLeft": 21,
        "ArrowRight": 22,
        "Delete": 112,
        "Tab": 61,
    };

    if (keyMap[e.key]) {
        e.preventDefault();
        wsSend({ action: "keyevent", keycode: keyMap[e.key] });
    } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        wsSend({ action: "text", text: e.key });
    }
});

async function sendClipboardShortcut(key) {
    if (!state.focusedSerial && !state.broadcastMode) {
        toast("Seleziona un dispositivo per Ctrl+" + key.toUpperCase(), "warn");
        return;
    }
    if (key === "v") {
        // Ctrl+V: prova a incollare il testo degli appunti del PC
        try {
            const text = await navigator.clipboard.readText();
            if (text) {
                wsSend({ action: "text", text });
                toast("Testo incollato sul dispositivo", "success");
                return;
            }
        } catch {}
    }
    const keyMap = { "a": 29, "c": 31, "v": 50, "x": 52 };
    const keycode = keyMap[key];
    if (keycode) {
        wsSend({ action: "keyevent", keycode, metastate: 0x1000 });
    }
}

// =====================================================================
// Toolbar Actions
// =====================================================================

function handleToolbarAction(action, serial, cell) {
    switch (action) {
        case "fullscreen":
            toggleFullscreen(serial, cell);
            break;
        case "screen_toggle": {
            const dev = state.devices.find((d) => d.serial === serial);
            if (dev && dev.screen_on) {
                wsSend({ action: "screen_off", serial });
            } else {
                wsSend({ action: "screen_on", serial });
            }
            break;
        }
        case "screenshot":
            takeScreenshot(serial);
            break;
        case "rotate":
            wsSend({ action: "rotate", serial });
            break;
        case "stream_toggle": {
            const dev = state.devices.find((d) => d.serial === serial);
            if (dev && dev.streaming) {
                wsSend({ action: "stop_stream", serial });
            } else {
                wsSend({ action: "start_stream", serial });
            }
            break;
        }
        case "home":
            wsSend({ action: "keyevent", serial, keycode: 3 });
            break;
        case "back":
            wsSend({ action: "keyevent", serial, keycode: 4 });
            break;
        case "recent_apps":
            wsSend({ action: "keyevent", serial, keycode: 187 });
            break;
    }
}

function toggleFullscreen(serial, cell) {
    if (state.fullscreenSerial === serial) {
        cell.classList.remove("fullscreen-cell");
        state.fullscreenSerial = null;
    } else {
        // Rimuovi fullscreen da eventuali altre celle
        document.querySelectorAll(".fullscreen-cell").forEach((c) => {
            c.classList.remove("fullscreen-cell");
        });
        cell.classList.add("fullscreen-cell");
        state.fullscreenSerial = serial;
    }
}

async function takeScreenshot(serial) {
    try {
        const resp = await fetch(`/api/devices/${serial}/screenshot`);
        if (!resp.ok) throw new Error("Screenshot fallito");
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `screenshot_${serial}_${Date.now()}.png`;
        a.click();
        URL.revokeObjectURL(url);
        toast("Screenshot salvato", "success");
    } catch (e) {
        toast("Errore screenshot: " + e.message, "error");
    }
}

// Escape per uscire da fullscreen
document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.fullscreenSerial) {
        document.querySelectorAll(".fullscreen-cell").forEach((c) => {
            c.classList.remove("fullscreen-cell");
        });
        state.fullscreenSerial = null;
    }
});

// =====================================================================
// Header
// =====================================================================

function updateHeader() {
    const online = state.devices.filter((d) => d.status === "online").length;
    const total = state.devices.length;
    const played = state.devices.filter((d) => d.played).length;
    const countEl = document.getElementById("deviceCount");
    if (countEl) {
        let text = `${online}/${total} dispositivi`;
        if (played > 0) text += ` (${played} giocati)`;
        countEl.textContent = text;
    }

    const btnBroadcast = document.getElementById("btnBroadcast");
    if (btnBroadcast) btnBroadcast.classList.toggle("active", state.broadcastMode);

    const btnResetPlayed = document.getElementById("btnResetPlayed");
    if (btnResetPlayed) btnResetPlayed.style.display = played > 0 ? "" : "none";
}

// =====================================================================
// Sidebar
// =====================================================================

function initSidebar() {
    const sidebar = document.getElementById("sidebar");
    const btnOpen = document.getElementById("btnSidebar");
    const btnClose = document.getElementById("btnCloseSidebar");

    btnOpen.addEventListener("click", () => sidebar.classList.toggle("open"));
    btnClose.addEventListener("click", () => sidebar.classList.remove("open"));
}

// =====================================================================
// Bulk Actions
// =====================================================================

function initBulkActions() {
    // APK
    const apkDrop = document.getElementById("apkDropZone");
    const apkInput = document.getElementById("apkFileInput");

    apkDrop.addEventListener("click", () => apkInput.click());
    apkDrop.addEventListener("dragover", (e) => {
        e.preventDefault();
        apkDrop.classList.add("dragover");
    });
    apkDrop.addEventListener("dragleave", () => apkDrop.classList.remove("dragover"));
    apkDrop.addEventListener("drop", (e) => {
        e.preventDefault();
        apkDrop.classList.remove("dragover");
        if (e.dataTransfer.files.length) uploadApk(e.dataTransfer.files[0]);
    });
    apkInput.addEventListener("change", () => {
        if (apkInput.files.length) uploadApk(apkInput.files[0]);
    });

    // Push File
    const fileDrop = document.getElementById("fileDropZone");
    const fileInput = document.getElementById("pushFileInput");

    fileDrop.addEventListener("click", () => fileInput.click());
    fileDrop.addEventListener("dragover", (e) => {
        e.preventDefault();
        fileDrop.classList.add("dragover");
    });
    fileDrop.addEventListener("dragleave", () => fileDrop.classList.remove("dragover"));
    fileDrop.addEventListener("drop", (e) => {
        e.preventDefault();
        fileDrop.classList.remove("dragover");
        if (e.dataTransfer.files.length) pushFile(e.dataTransfer.files[0]);
    });
    fileInput.addEventListener("change", () => {
        if (fileInput.files.length) pushFile(fileInput.files[0]);
    });

    // Shell
    document.getElementById("btnRunShell").addEventListener("click", runShellCommand);

    // Global actions
    document.getElementById("btnWakeAll").addEventListener("click", () => {
        fetch("/api/bulk/wake-all", { method: "POST" });
        toast("Wake inviato a tutti");
    });
    document.getElementById("btnSleepAll").addEventListener("click", () => {
        fetch("/api/bulk/sleep-all", { method: "POST" });
        toast("Sleep inviato a tutti");
    });
    document.getElementById("btnRebootAll").addEventListener("click", () => {
        if (confirm("Riavviare tutti i dispositivi?")) {
            fetch("/api/bulk/reboot-all", { method: "POST" });
            toast("Riavvio in corso...", "warn");
        }
    });
}

async function uploadApk(file) {
    if (!file.name.endsWith(".apk")) {
        toast("Seleziona un file .apk", "error");
        return;
    }

    const progress = document.getElementById("apkProgress");
    const fill = document.getElementById("apkProgressFill");
    const statusEl = document.getElementById("apkStatus");

    progress.style.display = "block";
    fill.style.width = "10%";
    statusEl.textContent = `Installazione ${file.name}...`;

    const form = new FormData();
    form.append("file", file);

    try {
        const resp = await fetch("/api/bulk/install-apk", { method: "POST", body: form });
        const data = await resp.json();
        fill.style.width = "100%";

        const ok = Object.values(data.results || {}).filter((r) => r === "ok").length;
        statusEl.textContent = `Completato: ${ok}/${data.total} riusciti`;
        toast(`APK installato su ${ok}/${data.total} dispositivi`, ok === data.total ? "success" : "warn");
    } catch (e) {
        statusEl.textContent = "Errore: " + e.message;
        toast("Errore installazione APK", "error");
    }
}

async function pushFile(file) {
    const remotePath = document.getElementById("remotePath").value || "/sdcard/";
    const statusEl = document.getElementById("pushStatus");
    statusEl.textContent = `Invio ${file.name}...`;

    const form = new FormData();
    form.append("file", file);

    try {
        const resp = await fetch(`/api/bulk/push-file?remote_path=${encodeURIComponent(remotePath)}`, {
            method: "POST",
            body: form,
        });
        const data = await resp.json();
        const ok = Object.values(data.results || {}).filter((r) => r === "ok").length;
        statusEl.textContent = `Completato: ${ok}/${data.total} riusciti`;
        toast(`File inviato a ${ok}/${data.total} dispositivi`, ok === data.total ? "success" : "warn");
    } catch (e) {
        statusEl.textContent = "Errore: " + e.message;
        toast("Errore invio file", "error");
    }
}

function showResult(title, content) {
    const modal = document.getElementById("resultModal");
    const titleEl = document.getElementById("resultModalTitle");
    const bodyEl = document.getElementById("resultModalBody");
    if (!modal || !titleEl || !bodyEl) return;
    titleEl.textContent = title;
    const pre = bodyEl.querySelector("pre");
    if (pre) pre.textContent = content;
    modal.style.display = "flex";
}

function closeResult() {
    const modal = document.getElementById("resultModal");
    if (modal) modal.style.display = "none";
}

function initResultModal() {
    const btnClose = document.getElementById("btnCloseResult");
    const btnCopy = document.getElementById("btnCopyResult");
    if (btnClose) {
        btnClose.addEventListener("click", closeResult);
    }
    if (btnCopy) {
        btnCopy.addEventListener("click", () => {
            const body = document.getElementById("resultModalBody");
            if (body) {
                navigator.clipboard.writeText(body.textContent).then(() => toast("Copiato", "success")).catch(() => {});
            }
        });
    }
    document.getElementById("resultModal").addEventListener("click", (e) => {
        if (e.target === e.currentTarget || e.target.classList.contains("result-modal-backdrop")) closeResult();
    });
}

async function runShellCommand() {
    const cmd = document.getElementById("shellCommand").value.trim();
    if (!cmd) return;

    showResult("Esecuzione shell", "Esecuzione in corso...");

    try {
        const resp = await fetch(`/api/bulk/shell?command=${encodeURIComponent(cmd)}`, {
            method: "POST",
        });
        const data = await resp.json();
        let output = "";
        for (const [serial, result] of Object.entries(data)) {
            output += `[${serial}] ${result}\n`;
        }
        showResult("Output shell", output || "(nessun output)");
    } catch (e) {
        showResult("Errore shell", "Errore: " + e.message);
    }
}

// =====================================================================
// Pannello Script ADB
// =====================================================================

const scriptState = { categorie: [], espansi: {} };

async function initScriptPanel() {
    try {
        const r = await fetch("/api/scripts");
        const data = await r.json();
        scriptState.categorie = data.categorie || [];
        renderScriptLista();
    } catch (e) {
        console.error("Errore caricamento script:", e);
    }

    const filtro = document.getElementById("scriptFiltro");
    if (filtro) {
        filtro.addEventListener("input", () => renderScriptLista(filtro.value));
    }

    const targetGroup = document.getElementById("scriptTargetGroup");
    const targetInput = document.getElementById("scriptTarget");
    if (targetGroup) {
        targetGroup.querySelectorAll(".segment-btn").forEach((btn) => {
            btn.addEventListener("click", () => {
                targetGroup.querySelectorAll(".segment-btn").forEach((b) => {
                    b.classList.remove("active");
                    b.setAttribute("aria-pressed", "false");
                });
                btn.classList.add("active");
                btn.setAttribute("aria-pressed", "true");
                if (targetInput) targetInput.value = btn.dataset.value;
            });
        });
    }

    const btnCloseOutput = document.getElementById("btnCloseScriptOutput");
    const outputWrap = document.getElementById("scriptOutputWrap");
    if (btnCloseOutput && outputWrap) {
        btnCloseOutput.addEventListener("click", () => {
            outputWrap.style.display = "none";
        });
    }
}

function renderScriptLista(filtro = "") {
    const cont = document.getElementById("scriptLista");
    if (!cont) return;

    const q = filtro.trim().toLowerCase();
    cont.innerHTML = "";

    scriptState.categorie.forEach((cat) => {
        const script = cat.script.filter((s) =>
            !q || s.nome.toLowerCase().includes(q) ||
            s.descrizione.toLowerCase().includes(q)
        );
        if (!script.length) return;

        // Con la ricerca attiva le categorie sono sempre aperte
        const aperta = q ? true : !!scriptState.espansi[cat.categoria];

        const gruppo = document.createElement("div");
        gruppo.className = "script-gruppo";
        gruppo.innerHTML = `
            <div class="script-categoria">
                <span>${aperta ? "▾" : "▸"} ${escapeHtml(cat.categoria)}</span>
                <span class="script-conteggio">${script.length}</span>
            </div>
            <div class="script-voci" style="display:${aperta ? "block" : "none"}"></div>
        `;

        gruppo.querySelector(".script-categoria").addEventListener("click", () => {
            scriptState.espansi[cat.categoria] = !aperta;
            renderScriptLista(filtro);
        });

        const voci = gruppo.querySelector(".script-voci");
        script.forEach((s) => voci.appendChild(creaVoceScript(s)));
        cont.appendChild(gruppo);
    });

    if (!cont.children.length) {
        cont.innerHTML = `<div class="script-vuoto">Nessuno script corrisponde alla ricerca</div>`;
    }
}

function creaVoceScript(s) {
    const voce = document.createElement("div");
    voce.className = "script-voce" + (s.pericoloso ? " pericoloso" : "");

    const campi = s.parametri.map((p) => `
        <div class="script-param">
            <label>${escapeHtml(p.label)}</label>
            <input type="${p.tipo === "password" ? "password" : p.tipo === "number" ? "number" : "text"}"
                   data-param="${p.name}"
                   value="${escapeHtml(p.default || "")}"
                   placeholder="${escapeHtml(p.placeholder || "")}" />
        </div>
    `).join("");

    voce.innerHTML = `
        <div class="script-testata">
            <span class="script-icona">${s.icona}</span>
            <div class="script-info">
                <div class="script-nome">${escapeHtml(s.nome)}</div>
                <div class="script-desc">${escapeHtml(s.descrizione)}</div>
            </div>
        </div>
        ${campi ? `<div class="script-parametri">${campi}</div>` : ""}
        <button class="btn ${s.pericoloso ? "btn-danger" : "btn-accent"} script-avvia">
            ${s.pericoloso ? "⚠ Esegui" : "▶ Esegui"}
        </button>
    `;

    voce.querySelector(".script-avvia").addEventListener("click", async (ev) => {
        const btn = ev.currentTarget;
        const parametri = {};
        voce.querySelectorAll("[data-param]").forEach((inp) => {
            parametri[inp.dataset.param] = inp.value;
        });

        if (s.pericoloso) {
            const conferma = confirm(
                `"${s.nome}" è un'operazione potenzialmente distruttiva.\n\nProcedere?`
            );
            if (!conferma) return;
        }

        btn.disabled = true;
        const testoOriginale = btn.textContent;
        btn.textContent = "⏳ In corso...";
        try {
            await eseguiScript(s.id, s.nome, parametri);
        } finally {
            btn.disabled = false;
            btn.textContent = testoOriginale;
        }
    });

    return voce;
}

async function eseguiScript(scriptId, nomeScript, parametri) {
    const target = document.getElementById("scriptTarget").value;
    const outputEl = document.getElementById("scriptOutput");
    const outputWrap = document.getElementById("scriptOutputWrap");

    try {
        const r = await fetch(`/api/scripts/${scriptId}/esegui`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ target, parametri }),
        });
        const data = await r.json();

        if (data.errore) {
            toast(data.errore, "error");
            return;
        }

        const { riusciti, totale, risultati } = data;
        if (riusciti === totale) {
            toast(`${nomeScript}: riuscito su ${totale} dispositivi`, "success");
        } else {
            toast(`${nomeScript}: ${riusciti}/${totale} riusciti`, "warn");
        }

        // Mostra il dettaglio solo se c'è output o qualche errore
        const MAX_RIGHE_PER_DISP = 8;
        const MAX_RIGHE_TOTALI = 40;
        const righe = [];
        let righeTotali = 0;

        for (const res of risultati) {
            const esito = res.ok ? "✔" : "✖";
            let testo = `${esito} ${res.serial || "—"}: ${res.messaggio}`;
            if (res.output && res.output.trim()) {
                const outRighe = res.output.split("\n").filter((l) => l.trim());
                const tagliato = outRighe.slice(-MAX_RIGHE_PER_DISP);
                if (outRighe.length > MAX_RIGHE_PER_DISP) {
                    tagliato.unshift(`... (${outRighe.length - MAX_RIGHE_PER_DISP} righe nascoste)`);
                }
                testo += "\n" + tagliato.map((l) => "    " + l).join("\n");
            }
            righe.push(testo);
            righeTotali += testo.split("\n").length;
            if (righeTotali >= MAX_RIGHE_TOTALI) break;
        }

        const testo = `── ${nomeScript} ──\n` + righe.join("\n");
        showResult(nomeScript, testo);
    } catch (e) {
        toast(`Errore: ${e.message}`, "error");
    }
}

// =====================================================================
// Drag selection
// =====================================================================

function initDragSelect() {
    const grid = document.getElementById("deviceGrid");
    if (!grid) return;

    let startX = 0, startY = 0, band = null, isDragging = false;

    function intersect(r1, r2) {
        return r1.left < r2.right && r1.right > r2.left && r1.top < r2.bottom && r1.bottom > r2.top;
    }

    grid.addEventListener("mousedown", (e) => {
        if (e.button !== 0 || e.target !== grid) return;
        e.preventDefault();
        startX = e.clientX;
        startY = e.clientY;
        isDragging = true;

        band = document.createElement("div");
        band.className = "rubberband";
        band.style.left = startX + "px";
        band.style.top = startY + "px";
        band.style.width = "0px";
        band.style.height = "0px";
        document.body.appendChild(band);
    });

    document.addEventListener("mousemove", (e) => {
        if (!isDragging || !band) return;
        const left = Math.min(startX, e.clientX);
        const top = Math.min(startY, e.clientY);
        const width = Math.abs(e.clientX - startX);
        const height = Math.abs(e.clientY - startY);
        band.style.left = left + "px";
        band.style.top = top + "px";
        band.style.width = width + "px";
        band.style.height = height + "px";
    });

    document.addEventListener("mouseup", (e) => {
        if (!isDragging || !band) return;
        isDragging = false;
        const bandRect = band.getBoundingClientRect();
        band.remove();
        band = null;

        if (bandRect.width < 5 || bandRect.height < 5) return;

        const cells = grid.querySelectorAll(".device-cell");
        cells.forEach((cell) => {
            const cellRect = cell.getBoundingClientRect();
            if (intersect(bandRect, cellRect)) {
                const serial = cell.dataset.serial;
                const dev = state.devices.find((d) => d.serial === serial);
                if (dev && !dev.selected) {
                    wsSend({ action: "select", serial, selected: true });
                }
            }
        });
    });
}

// =====================================================================
// Settings
// =====================================================================

async function initSettings() {
    const maxFps = document.getElementById("maxFps");
    const maxSize = document.getElementById("maxSize");

    try {
        const r = await fetch("/api/settings");
        const data = await r.json();
        if (maxFps) maxFps.value = data.stream?.max_fps ?? 30;
        if (maxSize) maxSize.value = data.stream?.max_size ?? 1080;
    } catch (e) {}

    async function saveStream() {
        try {
            await fetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    stream: {
                        max_fps: parseInt(maxFps?.value) || 30,
                        max_size: parseInt(maxSize?.value) || 1080,
                    },
                }),
            });
            toast("Impostazioni stream salvate", "success");
        } catch (e) {
            toast("Errore salvataggio", "error");
        }
    }

    if (maxFps) maxFps.addEventListener("change", saveStream);
    if (maxSize) maxSize.addEventListener("change", saveStream);

    const btnApply = document.getElementById("btnApplyStream");
    if (btnApply) {
        btnApply.addEventListener("click", async () => {
            try {
                await fetch("/api/settings/apply-stream", { method: "POST" });
                toast("Qualità stream riavviata", "success");
            } catch (e) {
                toast("Errore riavvio stream", "error");
            }
        });
    }

    const btnRestartAdb = document.getElementById("btnRestartAdb");
    if (btnRestartAdb) {
        btnRestartAdb.addEventListener("click", async () => {
            if (!confirm("Riavviare il daemon ADB?\\nSul telefono devi aver prima revocato le autorizzazioni debug USB.")) return;
            try {
                const r = await fetch("/api/adb/restart", { method: "POST" });
                const data = await r.json();
                if (r.ok && data.ok) {
                    toast("Daemon ADB riavviato. Controlla il telefono per la richiesta.", "success");
                } else {
                    toast(data.error || "Errore riavvio ADB", "error");
                }
            } catch (e) {
                toast("Errore riavvio ADB", "error");
            }
        });
    }
}

// =====================================================================
// Header Buttons
// =====================================================================

function initHeaderButtons() {
    document.getElementById("btnBroadcast").addEventListener("click", () => {
        const next = !state.broadcastMode;
        wsSend({ action: "broadcast", enabled: next });
    });

    document.getElementById("btnStartAll").addEventListener("click", () => {
        wsSend({ action: "start_all_streams" });
        toast("Avvio stream su tutti i dispositivi...");
    });

    document.getElementById("btnStopAll").addEventListener("click", () => {
        fetch("/api/stream/stop-all", { method: "POST" });
        toast("Stream fermati");
    });

    // Ripristina i dispositivi segnati come giocati
    const btnResetPlayed = document.getElementById("btnResetPlayed");
    if (btnResetPlayed) {
        btnResetPlayed.addEventListener("click", () => {
            if (confirm("Ripristinare tutti i dispositivi giocati?")) {
                wsSend({ action: "reset_played" });
            }
        });
    }

    // Max colonne e distanza tra le celle
    const gridColsInput = document.getElementById("gridCols");
    if (gridColsInput) {
        state.gridCols = parseInt(gridColsInput.value) || 20;
        gridColsInput.addEventListener("change", (e) => {
            state.gridCols = parseInt(e.target.value) || 20;
            if (state.gridCols < 2) state.gridCols = 2;
            if (state.gridCols > 40) state.gridCols = 40;
            e.target.value = state.gridCols;
            updateGridColumns();
        });
    }

    const gridGapInput = document.getElementById("gridGap");
    if (gridGapInput) {
        state.gridGap = parseInt(gridGapInput.value) || 14;
        gridGapInput.addEventListener("change", (e) => {
            state.gridGap = parseInt(e.target.value) || 14;
            if (state.gridGap < 0) state.gridGap = 0;
            if (state.gridGap > 100) state.gridGap = 100;
            e.target.value = state.gridGap;
            updateGridColumns();
        });
    }

    // Adatta colonne al ridimensionamento finestra / multi-schermo
    const gridContainer = document.getElementById("gridContainer");
    if (gridContainer && "ResizeObserver" in window) {
        const resizeObserver = new ResizeObserver(updateGridColumns);
        resizeObserver.observe(gridContainer);
    }

    // Ctrl + rotellina = zoom; Ctrl + Shift + rotellina = distanza
    if (gridContainer) {
        gridContainer.addEventListener("wheel", (e) => {
            if (!e.ctrlKey && !e.metaKey) return;
            e.preventDefault();
            if (e.shiftKey) {
                const delta = e.deltaY > 0 ? -2 : 2;
                state.gridGap = Math.min(100, Math.max(0, state.gridGap + delta));
                if (gridGapInput) gridGapInput.value = state.gridGap;
                updateGridColumns();
            } else {
                const delta = e.deltaY > 0 ? -0.05 : 0.05;
                state.feedZoom = Math.min(3.0, Math.max(0.25, state.feedZoom + delta));
                applyZoom();
            }
        }, { passive: false });
    }

    // Ordinamento
    const btnSort = document.getElementById("btnSort");
    if (btnSort) {
        btnSort.addEventListener("click", () => {
            state.sortBy = !state.sortBy;
            btnSort.classList.toggle("active", state.sortBy);
            renderGrid();
        });
    }

    // Restart ADB dall'header
    const btnRestartAdbHeader = document.getElementById("btnRestartAdbHeader");
    if (btnRestartAdbHeader) {
        btnRestartAdbHeader.addEventListener("click", async () => {
            btnRestartAdbHeader.disabled = true;
            toast("Riavvio server ADB in corso...");
            try {
                const r = await fetch("/api/adb/restart", { method: "POST" });
                const data = await r.json();
                if (r.ok && data.ok) {
                    toast("Server ADB riavviato. Rilevamento dispositivi in corso.", "success");
                } else {
                    toast(data.error || "Errore riavvio ADB", "error");
                }
            } catch (e) {
                toast("Errore riavvio ADB", "error");
            }
            btnRestartAdbHeader.disabled = false;
        });
    }

    // Update manuale
    const btnCheckUpdate = document.getElementById("btnCheckUpdate");
    if (btnCheckUpdate) {
        btnCheckUpdate.addEventListener("click", async () => {
            btnCheckUpdate.disabled = true;
            toast("Controllo aggiornamenti...");
            try {
                const res = await fetch("/api/check-update");
                const data = await res.json();
                if (!res.ok || data.error) {
                    toast(data.error || "Errore connessione server aggiornamenti", "error");
                    return;
                }
                if (data.available) {
                    const modal = document.getElementById("updateModal");
                    const title = document.getElementById("updateTitle");
                    const text = document.getElementById("updateText");
                    const bar = document.getElementById("updateProgressBar");
                    const btnOk = document.getElementById("btnUpdateOk");
                    const btnCancel = document.getElementById("btnUpdateCancel");

                    title.textContent = "Aggiornamento disponibile";
                    text.textContent = `Versione ${data.new_version} pronta. Clicca OK per scaricare e installare automaticamente.`;
                    bar.style.width = "0%";
                    btnOk.disabled = false;
                    btnCancel.disabled = false;
                    modal.style.display = "flex";

                    btnCancel.onclick = () => { modal.style.display = "none"; };
                    btnOk.onclick = async () => {
                        btnOk.disabled = true;
                        btnCancel.disabled = true;
                        text.textContent = "Preparazione download...";
                        const start = await fetch("/api/update/start", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({
                                download_url: data.download_url,
                                version: data.new_version,
                                silent_args: data.silent_args,
                            }),
                        });
                        if (!start.ok) { text.textContent = "Errore avvio aggiornamento."; return; }
                        const poll = setInterval(async () => {
                            try {
                                const p = await fetch("/api/update/progress");
                                const s = await p.json();
                                bar.style.width = `${s.percent || 0}%`;
                                if (s.status === "downloading") {
                                    text.textContent = `Download in corso... ${s.percent || 0}%`;
                                } else if (s.status === "error") {
                                    clearInterval(poll);
                                    text.textContent = "Errore: " + (s.error || "sconosciuto");
                                    btnOk.disabled = false;
                                    btnCancel.disabled = false;
                                } else if (s.status === "ready") {
                                    clearInterval(poll);
                                    text.textContent = "Installazione in corso, GridDroid si riavvierà...";
                                    bar.style.width = "100%";
                                    await fetch("/api/update/apply", { method: "POST" });
                                }
                            } catch {
                                clearInterval(poll);
                                text.textContent = "Riavvio in corso...";
                            }
                        }, 600);
                    };
                } else if (data.message) {
                    toast(data.message, "info");
                } else {
                    toast(`GridDroid ${data.version} è aggiornato.`, "success");
                }
            } catch (e) {
                toast("Errore controllo aggiornamenti", "error");
            } finally {
                btnCheckUpdate.disabled = false;
            }
        });
    }
}

// =====================================================================
// Log Panel
// =====================================================================

function initLogPanel() {
    const toggleBar = document.getElementById("logToggle");
    const panel = document.getElementById("logPanel");

    toggleBar.addEventListener("click", () => {
        panel.classList.toggle("open");
    });

    // Carica log storici
    fetch("/api/logs")
        .then((r) => r.json())
        .then((entries) => {
            entries.forEach(appendLog);
        })
        .catch(() => {});
}

function appendLog(entry) {
    const body = document.getElementById("logBody");
    const div = document.createElement("div");
    div.className = `log-entry ${entry.level}`;

    const time = new Date(entry.ts * 1000);
    const timeStr = time.toLocaleTimeString("it-IT", { hour12: false });

    div.innerHTML = `
        <span class="log-time">${timeStr}</span>
        <span class="log-serial">${entry.serial || "—"}</span>
        <span class="log-msg">${escapeHtml(entry.message)}</span>
    `;
    body.appendChild(div);
    body.scrollTop = body.scrollHeight;

    state.logCount++;
    document.getElementById("logCountBadge").textContent = state.logCount;
}

// =====================================================================
// Log Actions
// =====================================================================

function copyLog() {
    const entries = document.querySelectorAll("#logBody .log-entry");
    const lines = [];
    entries.forEach((el) => {
        const time = el.querySelector(".log-time")?.textContent || "";
        const serial = el.querySelector(".log-serial")?.textContent || "";
        const msg = el.querySelector(".log-msg")?.textContent || "";
        lines.push(`${time}\t${serial}\t${msg}`);
    });
    const text = lines.join("\n");
    navigator.clipboard.writeText(text).then(() => {
        toast("Log copiato negli appunti");
    }).catch(() => {
        toast("Errore nella copia del log", "error");
    });
}

function clearLog() {
    document.getElementById("logBody").innerHTML = "";
    state.logCount = 0;
    document.getElementById("logCountBadge").textContent = "0";
    toast("Log azzerato");
}

// =====================================================================
// Toast
// =====================================================================

function toast(message, type = "info") {
    const container = document.getElementById("toastContainer");
    const div = document.createElement("div");
    div.className = `toast ${type}`;
    div.textContent = message;
    container.appendChild(div);
    setTimeout(() => div.remove(), 4000);
}

// =====================================================================
// Utility
// =====================================================================

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

// =====================================================================
// Zoom Feed
// =====================================================================

function updateGridColumns() {
    const grid = document.getElementById("deviceGrid");
    const container = document.getElementById("gridContainer");
    if (!grid || !container) return;
    const gap = state.gridGap || 14;
    const baseWidth = 260 * (state.feedZoom || 1) + gap;
    const maxCols = Math.max(2, state.gridCols || 10);
    const width = container.clientWidth;
    let cols = Math.max(2, Math.min(maxCols, Math.floor(width / baseWidth)));
    grid.style.setProperty("--grid-cols", cols);
    grid.style.setProperty("--grid-gap", gap + "px");
}

function applyZoom() {
    document.documentElement.style.setProperty("--feed-zoom", state.feedZoom);
    const label = document.getElementById("zoomLabel");
    if (label) label.textContent = Math.round(state.feedZoom * 100) + "%";
    updateGridColumns();
}

function initZoomControls() {
    const btnIn = document.getElementById("btnZoomIn");
    const btnOut = document.getElementById("btnZoomOut");
    const btnReset = document.getElementById("btnZoomReset");

    if (btnIn) btnIn.addEventListener("click", () => {
        state.feedZoom = Math.min(3.0, state.feedZoom + 0.05);
        applyZoom();
    });
    if (btnOut) btnOut.addEventListener("click", () => {
        state.feedZoom = Math.max(0.25, state.feedZoom - 0.05);
        applyZoom();
    });
    if (btnReset) btnReset.addEventListener("click", () => {
        state.feedZoom = 1.0;
        applyZoom();
    });
    applyZoom();
}

function shellQuote(s) {
    return `'${s.replace(/'/g, "'\"'\"'")}'`;
}

async function sendInputCommand(command, description) {
    try {
        const resp = await fetch(`/api/bulk/shell?command=${encodeURIComponent(command)}`, {
            method: "POST",
        });
        const data = await resp.json();
        const online = Object.keys(data).length;
        const ok = Object.values(data).filter((r) => typeof r === "string" && !r.toLowerCase().includes("error")).length;
        toast(`${description} inviati su ${ok}/${online} dispositivi`, ok === online ? "success" : "warn");
    } catch (e) {
        toast(`Errore invio ${description}: ` + e.message, "error");
    }
}

async function openBookmaker(url, name) {
    // Apre Chrome esplicitamente con l'URL; se non installato, fallisce sul dispositivo
    const command = `am start -n com.android.chrome/com.google.android.apps.chrome.Main -d "${url}"`;
    try {
        const resp = await fetch(`/api/bulk/shell?command=${encodeURIComponent(command)}`, {
            method: "POST",
        });
        const data = await resp.json();
        const online = Object.keys(data).length;
        const ok = Object.values(data).filter((r) => typeof r === "string" && !r.toLowerCase().includes("error")).length;
        toast(`${name} aperto su ${ok}/${online} dispositivi`, ok === online ? "success" : "warn");
    } catch (e) {
        toast(`Errore apertura ${name}: ` + e.message, "error");
    }
}

function initMacro() {
    const btnRecord = document.getElementById("btnMacroRecord");
    const btnStop = document.getElementById("btnMacroStop");
    const inputName = document.getElementById("macroName");
    const status = document.getElementById("macroStatus");
    const list = document.getElementById("macroList");

    let recording = false;

    function setUi(recordingNow, macroCount) {
        recording = recordingNow;
        if (btnRecord) btnRecord.disabled = recordingNow;
        if (btnStop) btnStop.disabled = !recordingNow;
        if (status) {
            status.textContent = recordingNow
                ? "Registrazione in corso..."
                : (macroCount > 0 ? `${macroCount} macro salvate` : "Pronta");
        }
    }

    async function fetchMacros() {
        try {
            const r = await fetch("/api/macros");
            const data = await r.json();
            setUi(data.recording, (data.macros || []).length);
            renderMacros(data.macros || []);
        } catch (e) {
            console.error("Errore caricamento macro:", e);
        }
    }

    function renderMacros(names) {
        if (!list) return;
        list.innerHTML = "";
        if (!names.length) {
            list.innerHTML = `<div class="macro-empty">Nessuna macro salvata</div>`;
            return;
        }
        names.forEach((name) => {
            const row = document.createElement("div");
            row.className = "macro-item";
            const safeName = escapeHtml(name);
            row.innerHTML = `
                <span class="macro-item-name" title="${safeName}">${safeName}</span>
                <div class="macro-item-actions">
                    <button class="btn btn-accent macro-replay" data-name="${safeName}">Riproduci</button>
                    <button class="btn macro-delete" data-name="${safeName}">Cancella</button>
                </div>
            `;
            row.querySelector(".macro-replay").addEventListener("click", () => replayMacro(name));
            row.querySelector(".macro-delete").addEventListener("click", () => deleteMacro(name));
            list.appendChild(row);
        });
    }

    async function replayMacro(name) {
        try {
            const r = await fetch(`/api/macro/${encodeURIComponent(name)}/replay`, { method: "POST" });
            const data = await r.json();
            if (data.ok) {
                toast(`Replay macro "${name}" avviato`, "success");
            } else {
                toast(`Macro "${name}" non trovata`, "error");
            }
        } catch (e) {
            toast("Errore replay macro: " + e.message, "error");
        }
    }

    async function deleteMacro(name) {
        try {
            await fetch(`/api/macro/${encodeURIComponent(name)}`, { method: "DELETE" });
            await fetchMacros();
            toast(`Macro "${name}" cancellata`, "success");
        } catch (e) {
            toast("Errore cancellazione macro: " + e.message, "error");
        }
    }

    if (btnRecord) {
        btnRecord.addEventListener("click", () => {
            if (!state.focusedSerial) {
                const target = state.devices.find((d) => d.status === "online" && d.selected);
                if (target) {
                    wsSend({ action: "focus", serial: target.serial });
                }
            }
            wsSend({ action: "macro_record", recording: true });
            setUi(true, 0);
            toast("Registrazione macro avviata", "success");
        });
    }

    if (btnStop) {
        btnStop.addEventListener("click", () => {
            const name = inputName ? inputName.value.trim() : "";
            wsSend({ action: "macro_record", recording: false, name });
            if (inputName) inputName.value = "";
            setTimeout(fetchMacros, 100);
            toast("Registrazione macro fermata", "success");
        });
    }

    fetchMacros();
}

function initBookmakers() {
    const grid = document.getElementById("bookmakerGrid");
    const search = document.getElementById("bookmakerSearch");
    const addForm = document.getElementById("bookmakerAdd");
    const btnToggle = document.getElementById("btnToggleBookmakerAdd");
    const inputName = document.getElementById("newBookmakerName");
    const inputUrl = document.getElementById("newBookmakerUrl");
    const btnSave = document.getElementById("btnSaveBookmaker");
    if (!grid) return;

    const defaults = [
        { name: "ADMIRALBET", url: "https://www.admiralbet.it" },
        { name: "BET365", url: "https://www.bet365.it" },
        { name: "BETFAIR", url: "https://www.betfair.it" },
        { name: "BETFLAG", url: "https://www.betflag.it" },
        { name: "BETPASSION", url: "https://www.betpassion.it" },
        { name: "BETSSON", url: "https://www.betsson.it" },
        { name: "BETWIN360", url: "https://www.betwin360.it" },
        { name: "BWIN", url: "https://www.bwin.it" },
        { name: "Betpoint", url: "https://www.betpoint.it" },
        { name: "DOMUSBET", url: "https://www.domusbet.it" },
        { name: "EPLAY24", url: "https://www.eplay24.it" },
        { name: "EUROBET", url: "https://www.eurobet.it" },
        { name: "FASTBET", url: "https://www.fastbet.it" },
        { name: "GIOCA7", url: "https://www.gioca7.it" },
        { name: "GIOCODIGITALE", url: "https://www.giocodigitale.it" },
        { name: "GOLDBET", url: "https://www.goldbet.it" },
        { name: "LEOVEGAS", url: "https://www.leovegas.it" },
        { name: "LOTTOMATICA", url: "https://www.lottomatica.it" },
        { name: "MARATHONBET", url: "https://www.marathonbet.it" },
        { name: "MYLOTTERY", url: "https://www.mylottery.it" },
        { name: "NETBET", url: "https://www.netbet.it" },
        { name: "PLANETWIN365", url: "https://www.planetwin365.it" },
        { name: "POKERSTARS", url: "https://www.pokerstars.it" },
        { name: "QUIGIOCO", url: "https://www.quigioco.it" },
        { name: "SISAL", url: "https://www.sisal.it" },
        { name: "SNAI", url: "https://www.snai.it" },
        { name: "SPORTBET", url: "https://www.sportbet.it" },
        { name: "SPORTIUM", url: "https://www.sportium.it" },
        { name: "STAKE", url: "https://www.stake.com" },
        { name: "STANLEYBET", url: "https://www.stanleybet.it" },
        { name: "STARCASINO", url: "https://www.starcasino.it" },
        { name: "STARVEGAS", url: "https://www.starvegas.it" },
        { name: "STARYES", url: "https://www.staryes.it" },
        { name: "SUNBET", url: "https://www.sunbet.it" },
        { name: "TOTOSI", url: "https://www.totosi.it" },
        { name: "VINCITU", url: "https://www.vincitu.it" },
        { name: "WILLIAM HILL", url: "https://www.williamhill.it" },
        { name: "ZONAGIOCO", url: "https://www.zonagioco.it" },
    ];

    function loadCustom() {
        try {
            return JSON.parse(localStorage.getItem("griddroid_bookmakers") || "[]");
        } catch (e) {
            return [];
        }
    }
    function saveCustom(custom) {
        localStorage.setItem("griddroid_bookmakers", JSON.stringify(custom));
    }
    let custom = loadCustom();
    let all = [...defaults, ...custom];

    function render(list) {
        grid.innerHTML = "";
        if (!list.length) {
            grid.innerHTML = `<div class="bookmaker-empty">Nessun sito trovato</div>`;
            return;
        }
        list.forEach((b) => {
            const row = document.createElement("div");
            row.className = "bookmaker-row";
            const isCustom = custom.some((c) => c.url === b.url && c.name === b.name);
            const deleteBtn = isCustom
                ? `<button class="bookmaker-delete" title="Elimina" data-name="${escapeHtml(b.name)}" data-url="${escapeHtml(b.url)}">×</button>`
                : "";
            row.innerHTML = `
                <span class="bookmaker-name">${escapeHtml(b.name)}</span>
                <div class="bookmaker-actions">
                    <button class="bookmaker-copy" title="Copia URL" data-url="${escapeHtml(b.url)}">⧉</button>
                    ${deleteBtn}
                    <button class="bookmaker-open" data-url="${escapeHtml(b.url)}">Apri</button>
                </div>
            `;
            row.querySelector(".bookmaker-open").addEventListener("click", () => openBookmaker(b.url, b.name));
            row.querySelector(".bookmaker-copy").addEventListener("click", () => {
                navigator.clipboard.writeText(b.url)
                    .then(() => toast("URL copiato", "success"))
                    .catch(() => toast("Errore copia URL", "error"));
            });
            const del = row.querySelector(".bookmaker-delete");
            if (del) {
                del.addEventListener("click", () => {
                    custom = custom.filter((c) => !(c.name === b.name && c.url === b.url));
                    saveCustom(custom);
                    all = [...defaults, ...custom];
                    applySearch();
                    toast("Sito rimosso", "success");
                });
            }
            grid.appendChild(row);
        });
    }

    function applySearch() {
        const q = (search ? search.value : "").trim().toLowerCase();
        const filtered = all.filter(
            (b) => b.name.toLowerCase().includes(q) || b.url.toLowerCase().includes(q)
        );
        render(filtered);
    }

    if (search) search.addEventListener("input", applySearch);

    if (btnToggle && addForm) {
        btnToggle.addEventListener("click", () => {
            addForm.style.display = addForm.style.display === "none" ? "block" : "none";
        });
    }

    if (btnSave && inputName && inputUrl) {
        btnSave.addEventListener("click", () => {
            const name = (inputName.value || "").trim();
            let url = (inputUrl.value || "").trim();
            if (!name || !url) {
                toast("Compila nome e URL", "warn");
                return;
            }
            if (!/^https?:\/\//i.test(url)) url = "https://" + url;
            const newB = { name, url };
            if (!custom.some((c) => c.name === name && c.url === url)) {
                custom.push(newB);
                saveCustom(custom);
                all = [...defaults, ...custom];
                applySearch();
                inputName.value = "";
                inputUrl.value = "";
                if (addForm) addForm.style.display = "none";
                toast("Sito aggiunto", "success");
            } else {
                toast("Sito già presente", "warn");
            }
        });
    }

    applySearch();

    const btnPayPalApp = document.getElementById("btnOpenPayPalApp");
    if (btnPayPalApp) {
        btnPayPalApp.addEventListener("click", () => {
            const cmd = "monkey -p com.paypal.android.p2pmobile -c android.intent.category.LAUNCHER 1";
            sendInputCommand(cmd, "PayPal app");
        });
    }
}

// =====================================================================
// Gruppi
// =====================================================================

const STORAGE_GROUPS_KEY = "griddroid_groups";

function loadStoredGroups() {
    try {
        const raw = localStorage.getItem(STORAGE_GROUPS_KEY);
        return raw ? JSON.parse(raw) : [];
    } catch (e) {
        return [];
    }
}

function saveStoredGroups(groups) {
    localStorage.setItem(STORAGE_GROUPS_KEY, JSON.stringify([...new Set(groups)].sort()));
}

function getAllGroups() {
    const stored = new Set(loadStoredGroups());
    state.devices.forEach((d) => (d.tags || []).forEach((t) => stored.add(t)));
    return [...stored].sort();
}

function addGroup(name) {
    name = (name || "").trim();
    if (!name) return;
    const groups = loadStoredGroups();
    if (!groups.includes(name)) {
        groups.push(name);
        saveStoredGroups(groups);
        renderGroups();
        renderAssignDevice();
        toast(`Gruppo "${name}" creato`, "success");
    } else {
        toast("Gruppo già esistente", "warn");
    }
}

function removeGroup(name) {
    let groups = loadStoredGroups();
    groups = groups.filter((g) => g !== name);
    saveStoredGroups(groups);

    // Rimuove il tag anche da tutti i dispositivi
    state.devices.forEach((d) => {
        if ((d.tags || []).includes(name)) {
            d.tags = (d.tags || []).filter((t) => t !== name);
            wsSend({ action: "tags", serial: d.serial, tags: d.tags });
        }
    });

    renderGroups();
    renderAssignDevice();
    renderGrid();
    toast(`Gruppo "${name}" rimosso`, "success");
}

function selectGroup(name) {
    state.devices.forEach((d) => {
        const inGroup = (d.tags || []).includes(name);
        d.selected = inGroup;
        wsSend({ action: "select", serial: d.serial, selected: inGroup });
    });
    renderGrid();
    renderPhoneSelection();
    toast(`Selezionati dispositivi in "${name}"`, "success");
}

function filterGroup(name) {
    state.searchText = name;
    state.searchMode = "group";
    const searchInput = document.getElementById("deviceSearchInput");
    if (searchInput) searchInput.value = name;
    const searchMode = document.getElementById("deviceSearchMode");
    if (searchMode) searchMode.value = "group";
    renderGrid();
}

function renderGroups() {
    const list = document.getElementById("groupList");
    if (!list) return;
    const groups = getAllGroups();
    const stored = new Set(loadStoredGroups());
    const counts = state.devices.reduce((acc, d) => {
        (d.tags || []).forEach((t) => {
            acc[t] = (acc[t] || 0) + 1;
        });
        return acc;
    }, {});
    if (!groups.length) {
        list.innerHTML = `<div class="group-empty">Nessun gruppo. Crea il primo sopra.</div>`;
        return;
    }
    list.innerHTML = groups
        .map(
            (g) => `
        <div class="group-row">
            <span class="group-name">${escapeHtml(g)} <span class="group-count">(${counts[g] || 0})</span></span>
            <div class="group-actions">
                <button class="group-btn" data-action="select" data-group="${escapeHtml(g)}">Seleziona</button>
                <button class="group-btn" data-action="filter" data-group="${escapeHtml(g)}">Filtra</button>
                ${stored.has(g) ? `<button class="group-btn group-btn-delete" data-action="delete" data-group="${escapeHtml(g)}">×</button>` : ""}
            </div>
        </div>
    `
        )
        .join("");
    list.querySelectorAll("button[data-action='select']").forEach((btn) => {
        btn.addEventListener("click", () => selectGroup(btn.dataset.group));
    });
    list.querySelectorAll("button[data-action='filter']").forEach((btn) => {
        btn.addEventListener("click", () => filterGroup(btn.dataset.group));
    });
    list.querySelectorAll("button[data-action='delete']").forEach((btn) => {
        btn.addEventListener("click", () => removeGroup(btn.dataset.group));
    });
}

function renderAssignDevice() {
    const sel = document.getElementById("assignDevice");
    if (!sel) return;
    const prev = sel.value;
    const opts = state.devices
        .map((d) => `<option value="${escapeHtml(d.serial)}">${escapeHtml(d.display_name || d.serial)}</option>`)
        .join("");
    sel.innerHTML = `<option value="">-- Scegli telefono --</option>` + opts;
    if (state.devices.some((d) => d.serial === prev)) sel.value = prev;
    renderAssignGroups();
}

function renderAssignGroups() {
    const sel = document.getElementById("assignDevice");
    const list = document.getElementById("assignGroupList");
    if (!sel || !list) return;
    const serial = sel.value;
    const dev = state.devices.find((d) => d.serial === serial);
    const deviceGroups = dev ? (dev.tags || []) : [];
    const allGroups = getAllGroups();
    if (!allGroups.length) {
        list.innerHTML = `<div class="group-empty">Crea prima un gruppo.</div>`;
        return;
    }
    list.innerHTML = allGroups
        .map(
            (g) => `
        <label class="assign-group-row">
            <input type="checkbox" class="assign-group-checkbox" value="${escapeHtml(g)}" ${deviceGroups.includes(g) ? "checked" : ""} />
            <span class="assign-group-name">${escapeHtml(g)}</span>
        </label>
    `
        )
        .join("");
}

function saveDeviceGroups() {
    const sel = document.getElementById("assignDevice");
    const list = document.getElementById("assignGroupList");
    if (!sel || !list) return;
    const serial = sel.value;
    if (!serial) {
        toast("Scegli un dispositivo", "warn");
        return;
    }
    const groups = [...list.querySelectorAll("input:checked")].map((cb) => cb.value);
    const dev = state.devices.find((d) => d.serial === serial);
    if (dev) dev.tags = groups;
    wsSend({ action: "tags", serial, tags: groups });
    renderGrid();
    renderGroups();
    renderAssignDevice();
    toast(`Gruppi salvati per ${escapeHtml(dev?.display_name || serial)}`, "success");
}

function initGroups() {
    const input = document.getElementById("newGroupName");
    const btn = document.getElementById("btnCreateGroup");
    const sel = document.getElementById("assignDevice");
    const save = document.getElementById("btnSaveAssignment");

    if (btn && input) {
        const create = () => {
            addGroup(input.value);
            input.value = "";
        };
        btn.addEventListener("click", create);
        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                create();
            }
        });
    }

    if (sel) {
        sel.addEventListener("change", renderAssignGroups);
    }
    if (save) {
        save.addEventListener("click", saveDeviceGroups);
    }
    renderAssignDevice();
}

// =====================================================================
// Selezione telefoni
// =====================================================================

function renderPhoneSelection() {
    const list = document.getElementById("phoneSelectionList");
    if (!list) return;
    if (!state.devices.length) {
        list.innerHTML = `<div class="phone-list-empty">Nessun dispositivo</div>`;
        return;
    }
    list.innerHTML = state.devices
        .map(
            (d) => `
        <label class="phone-list-row" title="${escapeHtml(d.display_name || d.serial)}">
            <input type="checkbox" class="phone-list-checkbox" data-serial="${escapeHtml(d.serial)}" ${d.selected ? "checked" : ""} />
            <span class="phone-list-name">${escapeHtml(d.display_name || d.serial)}</span>
        </label>
    `
        )
        .join("");
    list.querySelectorAll("input[type=checkbox]").forEach((cb) => {
        cb.addEventListener("change", () => {
            const dev = state.devices.find((d) => d.serial === cb.dataset.serial);
            if (dev) dev.selected = cb.checked;
            wsSend({ action: "select", serial: cb.dataset.serial, selected: cb.checked });
            renderGrid();
        });
    });
}

function initSelection() {
    const btnAll = document.getElementById("btnSelectAll");
    const btnNone = document.getElementById("btnDeselectAll");
    if (btnAll) {
        btnAll.addEventListener("click", () => {
            state.devices.forEach((d) => {
                d.selected = true;
                wsSend({ action: "select", serial: d.serial, selected: true });
            });
            renderGrid();
            renderPhoneSelection();
        });
    }
    if (btnNone) {
        btnNone.addEventListener("click", () => {
            state.devices.forEach((d) => {
                d.selected = false;
                wsSend({ action: "select", serial: d.serial, selected: false });
            });
            renderGrid();
            renderPhoneSelection();
        });
    }
}

function initAccordion() {
    const sidebar = document.getElementById("sidebar");
    if (!sidebar) return;
    const sections = sidebar.querySelectorAll(".sidebar-section");

    sections.forEach((sec) => {
        const h4 = sec.querySelector("h4");
        if (!h4) return;
        h4.addEventListener("click", () => {
            const wasActive = sec.classList.contains("active");
            sections.forEach((s) => s.classList.remove("active"));
            if (!wasActive) sec.classList.add("active");
        });
    });

    if (sections.length && !sidebar.querySelector(".sidebar-section.active")) {
        sections[0].classList.add("active");
    }
}

// =====================================================================
// Search
// =====================================================================

function initSearch() {
    const searchInput = document.getElementById("deviceSearchInput");
    const searchMode = document.getElementById("deviceSearchMode");

    if (searchInput) {
        searchInput.addEventListener("input", () => {
            state.searchText = searchInput.value;
            renderGrid();
        });
    }

    if (searchMode) {
        searchMode.addEventListener("change", () => {
            state.searchMode = searchMode.value;
            renderGrid();
        });
    }
}

// =====================================================================
// Init
// =====================================================================

document.addEventListener("DOMContentLoaded", () => {
    connectWebSocket();
    initSidebar();
    initAccordion();
    initBulkActions();
    initScriptPanel();
    initHeaderButtons();
    initSearch();
    initDragSelect();
    initLogPanel();
    initZoomControls();
    initMacro();
    initBookmakers();
    initSettings();
    initGroups();
    initSelection();
    initResultModal();
    // Carica la versione dell'app
    fetch("/api/version").then(r => r.json()).then(d => {
        const el = document.getElementById("versionBadge");
        if (el && d.version) el.textContent = `v${d.version}`;
    }).catch(() => {});
});
