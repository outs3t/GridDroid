/* GridDroid – Frontend Application */

// =====================================================================
// Stato globale
// =====================================================================

const state = {
    devices: [],
    broadcastMode: false,
    focusedSerial: null,
    fullscreenSerial: null,
    lastTap: {}, // serial -> {x, y} ultimo tap reale (target auto-click)
    soloSerials: null, // Set di seriali da mostrare; null = mostra tutti
    logCount: 0,
    ws: null,
    gridCols: 15,
    gridGap: 14,
    feedZoom: 1.0,
    searchText: "",
    searchMode: "name",
    activeGroupFilter: null,
    showPlayed: localStorage.getItem("griddroid_show_played") === "1",
    showSkipped: localStorage.getItem("griddroid_show_skipped") === "1",
    sortBy: localStorage.getItem("griddroid_sort_by") || "az",
    // AutoWatch: apre il WS video solo per le celle visibili in griglia
    // (+ quella in fullscreen). Il server continua a far girare scrcpy.
    autoWatch: localStorage.getItem("griddroid.autoWatch") !== "0",
    visibleSerials: new Set(),
    // Modalita' video: 'jpeg' = decode nativo sul server via ffmpeg
    // (architettura Panda: il browser non vede mai H264 -> niente MSE,
    // niente backlog decoder, niente reset). 'h264' = WebCodecs nel
    // browser, 'mse' = <video> con remuxer fMP4.
    videoMode: ["h264", "mse", "jpeg"].includes(localStorage.getItem("griddroid.videoMode"))
        ? localStorage.getItem("griddroid.videoMode")
        : "h264",
};

// =====================================================================
// WebSocket
// =====================================================================

// Log diagnostici stream: attivare con window.DEBUG_STREAM = true in console
const DEBUG_STREAM = () => window.DEBUG_STREAM === true;

let wsReconnectDelay = 2000;

function connectWebSocket() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws`;
    const ws = new WebSocket(url);
    state.ws = ws;

    ws.onopen = () => {
        wsReconnectDelay = 2000;
        console.log("WebSocket connesso");
        // Dopo una riconnessione riasserisce il tier focus: se eravamo
        // in fullscreen il server potrebbe aver perso lo stato (restart
        // app), e il device tornerebbe al profilo griglia.
        if (state.fullscreenSerial) {
            fetch(`/api/devices/${state.fullscreenSerial}/stream-focus?on=1`, {
                method: "POST",
            }).catch(() => { });
        }
    };

    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            if (msg.type === "devices") {
                updateDevicesState(msg);
            } else if (msg.type === "log") {
                appendLog(msg.data);
            }
        } catch (e) {
            console.error("WS parse error:", e);
        }
    };

    ws.onclose = () => {
        // Backoff esponenziale + jitter: evita tempeste di riconnessione su VPN
        const delay = wsReconnectDelay + Math.random() * 1000;
        console.log(`WebSocket disconnesso, riconnessione tra ${Math.round(delay / 1000)}s...`);
        setTimeout(connectWebSocket, delay);
        wsReconnectDelay = Math.min(wsReconnectDelay * 2, 30000);
    };

    ws.onerror = () => {
        ws.close();
    };
}

let lastDevicesJson = "";
function updateDevicesState(msg) {
    const json = JSON.stringify(msg);
    if (json === lastDevicesJson) return;
    lastDevicesJson = json;
    state.devices = msg.data || [];
    state.broadcastMode = msg.broadcast;
    state.focusedSerial = msg.focused;
    try {
        renderGrid();
    } catch (e) {
        console.error("Errore renderGrid:", e);
    }
    updateHeader();
}

async function pollDevices() {
    try {
        const r = await fetch("/api/devices");
        if (!r.ok) return;
        const msg = await r.json();
        updateDevicesState(msg);
    } catch (e) {
        console.error("Errore polling devices:", e);
    }
}

function wsSend(obj) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify(obj));
    }
}

// Flag antiricorsione: se /api/client-log non e' raggiungibile, la fetch
// rigetta e l'handler unhandledrejection la re-inoltrerebbe a remoteLog,
// creando un loop infinito di "Failed to fetch". Con questo flag i reject
// della fetch di logging vengono ignorati silenziosamente.
let _remoteLogFailing = false;

function remoteLog(level, message, serial) {
    if (_remoteLogFailing) return;
    try {
        fetch("/api/client-log", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ level, message: String(message), serial }),
        }).catch(() => {
            // La fetch di logging e' fallita (server giu' / rete assente).
            // Silenziamo i tentativi successivi per evitare il loop
            // autoreferenziale con l'handler unhandledrejection.
            _remoteLogFailing = true;
            // Riprova fra 10s: se il server torna, riprendiamo a loggare.
            setTimeout(() => { _remoteLogFailing = false; }, 10000);
        });
    } catch (e) {}
}

(function setupClientLogger() {
    const origError = console.error;
    console.error = function(...args) {
        origError.apply(console, args);
        const text = args.map((a) => {
            if (a instanceof Error) return a.stack || a.message;
            if (typeof a === "object") return JSON.stringify(a);
            return String(a);
        }).join(" ");
        remoteLog("error", text, null);
    };

    const origWarn = console.warn;
    console.warn = function(...args) {
        origWarn.apply(console, args);
        const text = args.map((a) => {
            if (a instanceof Error) return a.stack || a.message;
            if (typeof a === "object") return JSON.stringify(a);
            return String(a);
        }).join(" ");
        remoteLog("warn", text, null);
    };

    window.onerror = function(message, source, lineno, colno, error) {
        const stack = error && (error.stack || error.message) ? ` : ${error.stack || error.message}` : "";
        remoteLog("error", `${message} @ ${source}:${lineno}:${colno}${stack}`, null);
    };

    window.addEventListener("unhandledrejection", (ev) => {
        const reason = ev.reason instanceof Error ? (ev.reason.stack || ev.reason.message) : String(ev.reason);
        remoteLog("error", `Unhandled rejection: ${reason}`, null);
    });
})();

// =====================================================================
// Rendering Griglia
// =====================================================================

// Device visibili in griglia: filtro gruppo attivo + ricerca +
// giocati/skipati + "solo questi". Condivisa fra renderGrid e Ctrl+A:
// la selezione totale copre solo i device in vista, non tutto il farm.
function getVisibleDevices() {
    let devices = [...state.devices];

    // Filtro per gruppo attivo
    if (state.activeGroupFilter && state.activeGroupFilter !== "__all__") {
        devices = devices.filter((dev) => (dev.tags || []).includes(state.activeGroupFilter));
    }

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

    // Mostra/Nascondi giocati e non giocati
    devices = devices.filter((dev) => {
        if (dev.played && !state.showPlayed) return false;
        if (dev.skipped && !state.showSkipped) return false;
        return true;
    });

    // Filtro "mostra solo questi": tiene solo i seriali selezionati
    if (state.soloSerials) {
        devices = devices.filter((dev) => state.soloSerials.has(dev.serial));
    }
    return devices;
}

function renderGrid() {
    const grid = document.getElementById("deviceGrid");
    const container = document.getElementById("gridContainer");
    // Preserva lo scroll: il re-render non deve riportare la vista in cima
    const scrollTop = container ? container.scrollTop : 0;
    const scrollLeft = container ? container.scrollLeft : 0;
    let devices = getVisibleDevices();

    // Ordinamento
    if (state.sortBy === "az") {
        devices.sort((a, b) => (a.display_name || "").localeCompare(b.display_name || ""));
    } else if (state.sortBy === "online") {
        devices.sort((a, b) =>
            (b.status === "online" ? 1 : 0) - (a.status === "online" ? 1 : 0) ||
            (a.display_name || "").localeCompare(b.display_name || "")
        );
    } else if (state.sortBy === "offline") {
        devices.sort((a, b) =>
            (a.status === "online" ? 1 : 0) - (b.status === "online" ? 1 : 0) ||
            (a.display_name || "").localeCompare(b.display_name || "")
        );
    } else {
        // Ordine manuale (numero crescente), poi A-Z
        devices.sort((a, b) => (a.order || 0) - (b.order || 0) || (a.display_name || "").localeCompare(b.display_name || ""));
    }

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

    try {
        let pos = 0;
        devices.forEach((dev) => {
            seenSerials.add(dev.serial);
            let cell = existingMap[dev.serial];

            let card;
            if (!cell) {
                // Device in fullscreen: la sua cella vive in <body>, fuori
                // dalla griglia — non creare un duplicato. La aggiorniamo
                // comunque cosi' stream, riconnessioni WS e overlay
                // continuano a funzionare mentre e' a schermo intero.
                const fsCell = state.fullscreenSerial === dev.serial
                    && document.querySelector(
                        `.device-cell.fullscreen-cell[data-serial="${dev.serial}"]`
                    );
                if (fsCell) {
                    updateDeviceCell(fsCell, dev);
                    pos++; // la card-shell conserva il suo slot in griglia
                    return;
                }
                cell = createDeviceCell(dev);
                card = wrapDeviceCard(cell, dev);
                awObserveCell(cell);
            } else {
                card = cell.parentElement;
            }

            // Sposta la card solo se non e' gia' nella posizione attesa:
            // appendChild incondizionato forzava un reflow di tutta la
            // griglia a ogni aggiornamento di stato, anche senza cambi
            // di ordinamento.
            if (grid.children[pos] !== card) {
                grid.insertBefore(card, grid.children[pos] || null);
            }
            pos++;
            updateDeviceCell(cell, dev);
        });
    } catch (e) {
        console.error("Errore durante il rendering delle celle:", e);
    }

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
            awUnobserveCell(cell);
            card?.remove();
        }
    });

    // Guscio vuoto (solo nome) lasciato da una vecchia fullscreen: una
    // .device-card senza .device-cell va rimossa — la shell del device
    // in fullscreen (.fs-shell) invece conserva lo slot in griglia.
    grid.querySelectorAll(".device-card:not(.fs-shell)").forEach((card) => {
        if (!card.querySelector(".device-cell")) card.remove();
    });

    renderGroups();
    renderAssignDevice();
    renderPhoneSelection();

    // Ripristina lo scroll dopo il reflow (sincrono + frame successivo:
    // lo scroll anchoring del browser puo' spostarlo dopo il layout)
    if (container) {
        container.scrollTop = scrollTop;
        container.scrollLeft = scrollLeft;
        requestAnimationFrame(() => {
            container.scrollTop = scrollTop;
            container.scrollLeft = scrollLeft;
        });
    }
}

// In modalita' 'mse' la cella ha solo il <video> (remuxer fMP4 nativo).
// Altrimenti canvas + WebCodecs.
const USE_WEBCODECS = state.videoMode !== 'mse';

function createDeviceCell(dev) {
    const cell = document.createElement("div");
    cell.className = "device-cell";
    cell.dataset.serial = dev.serial;

    const feedCanvas = '<canvas class="device-feed" style="display:none" width="0" height="0"></canvas>';
    const feedVideo = '<video class="device-feed" playsinline muted autoplay style="display:none"></video>';
    const feedTags = USE_WEBCODECS ? `${feedCanvas}${feedVideo}` : `${feedVideo}`;

    cell.innerHTML = `
        <input type="checkbox" class="device-select" title="Seleziona per broadcast" />
        ${feedTags}
        <div class="device-feed-placeholder">
            <div class="icon">📱</div>
            <span>Nessuno stream</span>
        </div>
        <div class="device-toolbar">
            <div class="toolbar-left">
                <button class="toolbar-btn" data-action="screenshot" title="Screenshot"><svg viewBox="0 0 24 24"><path d="M9 3l-1.8 2H4a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-3.2L15 3H9zm3 5a4.5 4.5 0 1 1 0 9 4.5 4.5 0 0 1 0-9zm0 2a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z"/></svg></button>
                <button class="toolbar-btn" data-action="screen_toggle" title="Accendi/Spegni schermo"><svg viewBox="0 0 24 24"><path d="M12 2a1 1 0 0 1 1 1v8a1 1 0 0 1-2 0V3a1 1 0 0 1 1-1zm5.7 3.3a1 1 0 0 1 0 1.4 7 7 0 1 1-11.4 0 1 1 0 1 1 1.4-1.4 5 5 0 1 0 8.6 0 1 1 0 0 1 1.4 0z"/></svg></button>
            </div>
            <div class="toolbar-center">
                <button class="toolbar-btn nav-btn" data-action="recent_apps" title="App recenti"><svg viewBox="0 0 24 24"><rect x="6.5" y="6.5" width="11" height="11" rx="1.5" fill="none" stroke="currentColor" stroke-width="2"/></svg></button>
                <button class="toolbar-btn nav-btn" data-action="home" title="Home"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="6" fill="none" stroke="currentColor" stroke-width="2"/></svg></button>
                <button class="toolbar-btn nav-btn" data-action="back" title="Indietro"><svg viewBox="0 0 24 24"><path d="M15.5 5.5l-6 6.5 6 6.5" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
            </div>
            <div class="toolbar-right">
                <button class="toolbar-btn" data-action="rotate" title="Rotazione"><svg viewBox="0 0 24 24"><path d="M12 5V2L7 6l5 4V7a5 5 0 1 1-5 5H5a7 7 0 1 0 7-7z"/></svg></button>
                <button class="toolbar-btn" data-action="fullscreen" title="Schermo intero"><svg viewBox="0 0 24 24"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
                <button class="toolbar-btn" data-action="stream_toggle" title="Avvia/Ferma stream"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></button>
                <button class="toolbar-btn" data-action="open_native_viewer" title="Finestra nativa"><svg viewBox="0 0 24 24"><path d="M18 3a1 1 0 0 1 1 1v4a1 1 0 1 1-2 0V6.414l-4.293 4.293a1 1 0 0 1-1.414-1.414L17.586 5H15a1 1 0 1 1 0-2h3zM5 7a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V14a1 1 0 1 1 2 0v4a3 3 0 0 1-3 3H5a3 3 0 0 1-3-3V8a3 3 0 0 1 3-3h4a1 1 0 1 1 0 2H5z" fill="currentColor"/></svg></button>
                <button class="toolbar-btn" data-action="quality" title="Qualita' stream"><svg viewBox="0 0 24 24"><path d="M12 15.5A3.5 3.5 0 0 1 8.5 12 3.5 3.5 0 0 1 12 8.5a3.5 3.5 0 0 1 3.5 3.5 3.5 3.5 0 0 1-3.5 3.5M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10 10-4.5 10-10S17.5 2 12 2z"/></svg></button>
            </div>
        </div>
        <div class="device-quality-panel" style="display:none;">
            <button class="q-preset" data-preset='{"maxSize":480,"maxFps":2,"bitRate":400000}' title="Panda: 480p 2fps 400k">P</button>
            <button class="q-preset" data-preset='{"maxSize":720,"maxFps":10,"bitRate":1500000}' title="Medio: 720p 10fps 1.5M">M</button>
            <button class="q-preset" data-preset='{"maxSize":1080,"maxFps":20,"bitRate":6000000}' title="Alta: 1080p 20fps 6M">H</button>
            <input type="number" class="q-size" placeholder="size" min="240" max="1920" />
            <input type="number" class="q-fps" placeholder="fps" min="1" max="60" />
            <input type="number" class="q-bitrate" placeholder="bitrate" min="50000" />
            <button class="q-save">Salva</button>
        </div>
    `;

    // Click sul feed → focus; Ctrl+click → selezione multipla
    // (lo schermo intero si apre solo dal menu contestuale, niente gesture)
    cell.addEventListener("click", (e) => {
        if (e.target.closest(".toolbar-btn") || e.target.closest(".device-select")) return;
        if (e.ctrlKey || e.metaKey) {
            e.stopPropagation();
            toggleDeviceSelection(dev.serial);
            return;
        }
        // Togli il focus dalla barra di ricerca (o altro input attivo) così la
        // tastiera globale viene inviata al device e non rimane nel box di testo.
        const active = document.activeElement;
        if (active && active !== cell && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.tagName === "SELECT")) {
            active.blur();
        }
        wsSend({ action: "focus", serial: dev.serial });
    });

    // Checkbox selezione
    const checkbox = cell.querySelector(".device-select");
    checkbox.addEventListener("change", () => {
        const next = checkbox.checked;
        if (dev) dev.selected = next;
        wsSend({ action: "select", serial: dev.serial, selected: next });
    });

    // Toolbar actions
    cell.querySelectorAll(".toolbar-btn").forEach((btn) => {
        btn.addEventListener("click", (e) => {
            e.stopPropagation();
            handleToolbarAction(btn.dataset.action, dev.serial, cell);
        });
    });

    // Pannello qualità stream per singolo device
    const qSave = cell.querySelector(".q-save");
    const qPanel = cell.querySelector(".device-quality-panel");
    if (qSave && qPanel) {
        qPanel.querySelectorAll(".q-preset").forEach((btn) => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const p = JSON.parse(btn.dataset.preset || "{}");
                cell.querySelector(".q-size").value = p.maxSize || "";
                cell.querySelector(".q-fps").value = p.maxFps || "";
                cell.querySelector(".q-bitrate").value = p.bitRate || "";
                setDeviceStreamParams(dev.serial, { maxSize: p.maxSize, maxFps: p.maxFps, bitRate: p.bitRate });
                qPanel.style.display = "none";
            });
        });
        qSave.addEventListener("click", (e) => {
            e.stopPropagation();
            const maxSize = parseInt(cell.querySelector(".q-size").value, 10) || 0;
            const maxFps = parseInt(cell.querySelector(".q-fps").value, 10) || 0;
            const bitRate = parseInt(cell.querySelector(".q-bitrate").value, 10) || 0;
            setDeviceStreamParams(dev.serial, { maxSize, maxFps, bitRate });
            qPanel.style.display = "none";
        });
    }

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
    const nameSize = Math.max(4, (dev.display_name || dev.serial).length + 2);
    label.innerHTML = `
        <div class="device-label-row">
            <input type="text" class="device-name" spellcheck="false" title="Clicca per rinominare" value="${escapeHtml(dev.display_name)}" size="${nameSize}" />
            <input type="number" class="device-order" title="Ordine" value="${dev.order || 0}" min="0" step="1" />
            <input type="color" class="device-color" title="Colore etichetta" value="${escapeHtml(dev.label_color || "#888888")}" />
            <span class="status-dot ${dev.status}"></span>
        </div>
        <div class="device-saldo">${dev.saldo ? `€ ${escapeHtml(String(dev.saldo))}` : ""}</div>
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

    const colorEl = label.querySelector(".device-color");
    if (colorEl) {
        colorEl.addEventListener("change", (e) => {
            const color = e.target.value;
            wsSend({ action: "label_color", serial: dev.serial, color });
            colorEl.style.boxShadow = color ? `0 0 0 2px ${color}` : "none";
        });
        if (dev.label_color) {
            colorEl.style.boxShadow = `0 0 0 2px ${dev.label_color}`;
        }
    }

    const orderEl = label.querySelector(".device-order");
    if (orderEl) {
        orderEl.addEventListener("change", (e) => {
            const order = parseInt(e.target.value, 10) || 0;
            wsSend({ action: "order", serial: dev.serial, order });
        });
    }

    card.appendChild(label);
    card.appendChild(cell);

    // Tasto destro sul device = tasto Indietro del telefono (come
    // scrcpy): va a focus + selezionati con la stessa regola dei tap.
    // Shift+tasto destro = menu contestuale GridDroid.
    card.addEventListener("contextmenu", (e) => {
        if (e.shiftKey) {
            showDeviceContextMenu(e, dev.serial);
            return;
        }
        e.preventDefault();
        wsSend({ action: "focus", serial: dev.serial });
        wsSend({ action: "keyevent", keycode: 4 }); // KEYCODE_BACK
    });

    // Hover = focus immediato (stile Panda): il telefono sotto il mouse
    // e' gia' focalizzato, cosi' il primo click agisce subito senza
    // dover prima selezionare. Ctrl+click resta la selezione multipla.
    card.addEventListener("mouseenter", () => {
        if (state.focusedSerial !== dev.serial) {
            wsSend({ action: "focus", serial: dev.serial });
        }
    });

    // Ctrl+click sulla card → selezione multipla (ignora nome, toolbar e checkbox)
    card.addEventListener("click", (e) => {
        if (e.ctrlKey || e.metaKey) {
            if (e.target.closest(".device-name") || e.target.closest(".toolbar-btn") || e.target.closest(".device-select")) return;
            e.stopPropagation();
            toggleDeviceSelection(dev.serial);
        }
    });

    return card;
}

function getTargetSerials(serial) {
    const dev = state.devices.find((d) => d.serial === serial);
    if (dev && dev.selected) {
        return state.devices.filter((d) => d.selected).map((d) => d.serial);
    }
    return [serial];
}

function toggleDeviceSelection(serial) {
    const dev = state.devices.find((d) => d.serial === serial);
    if (!dev) return;
    dev.selected = !dev.selected;
    wsSend({ action: "select", serial, selected: dev.selected });
    renderGrid();
    renderPhoneSelection();
}

function getContextTargetSerials(serial) {
    const dev = state.devices.find((d) => d.serial === serial);
    const selected = state.devices.filter((d) => d.selected);
    // Se ci sono altri dispositivi selezionati, applica a quelli; altrimenti solo al cliccato
    if (selected.length > 0 && selected.some((d) => d.serial === serial)) {
        return selected.map((d) => d.serial);
    }
    return [serial];
}

function addDevicesToGroup(serials, groupName) {
    groupName = (groupName || "").trim();
    if (!groupName) return;
    const stored = loadStoredGroups();
    if (!stored.includes(groupName)) {
        stored.push(groupName);
        saveStoredGroups(stored);
    }
    serials.forEach((s) => {
        const d = state.devices.find((dev) => dev.serial === s);
        if (d) {
            const tags = new Set(d.tags || []);
            tags.add(groupName);
            d.tags = [...tags];
            wsSend({ action: "tags", serial: s, tags: d.tags });
        }
    });
    renderGroups();
    renderGrid();
    renderAssignDevice();
    toast(`${serials.length} telefono/i aggiunti a "${groupName}"`, "success");
}

function removeDevicesFromGroup(serials, groupName) {
    groupName = (groupName || "").trim();
    if (!groupName) return;
    let removed = 0;
    serials.forEach((s) => {
        const d = state.devices.find((dev) => dev.serial === s);
        if (d && (d.tags || []).includes(groupName)) {
            d.tags = d.tags.filter((t) => t !== groupName);
            wsSend({ action: "tags", serial: s, tags: d.tags });
            removed++;
        }
    });
    if (!removed) return;
    // Se il gruppo tolto era il filtro attivo e non ha piu' membri, la
    // vista resterebbe vuota: meglio spegnere il filtro.
    if (
        state.activeGroupFilter === groupName &&
        !state.devices.some((d) => (d.tags || []).includes(groupName))
    ) {
        state.activeGroupFilter = null;
    }
    renderGroups();
    renderGrid();
    renderAssignDevice();
    toast(`${removed} telefono/i rimossi da "${groupName}"`, "success");
}

function createContextGroupForSelection(serials) {
    const name = window.prompt("Nome del nuovo gruppo:");
    if (name) addDevicesToGroup(serials, name);
}

function showDeviceContextMenu(e, serial) {
    e.preventDefault();
    const menu = document.getElementById("deviceContextMenu");
    if (!menu) return;
    // Il menu deve stare su <body>: dentro contenitori con overflow/stacking
    // context il position:fixed viene clippato e il menu si taglia.
    if (menu.parentElement !== document.body) document.body.appendChild(menu);
    menu.dataset.serial = serial;

    const targets = getContextTargetSerials(serial);
    const targetCount = targets.length;

    // Aggiorna etichette
    const setPlayedItem = menu.querySelector('[data-action="set-played"]');
    if (setPlayedItem) {
        setPlayedItem.textContent = targetCount === 1 ? "Segna come giocato" : `Segna ${targetCount} come giocati`;
    }
    const setSkippedItem = menu.querySelector('[data-action="set-skipped"]');
    if (setSkippedItem) {
        setSkippedItem.textContent = targetCount === 1 ? "Segna come non giocato" : `Segna ${targetCount} come non giocati`;
    }
    const autoclickItem = menu.querySelector('[data-action="autoclick"]');
    if (autoclickItem) {
        const dev = state.devices.find((d) => d.serial === serial);
        autoclickItem.textContent = dev && dev.autoclick
            ? "Ferma auto-click"
            : "Auto-click sull'ultimo punto toccato";
    }
    const soloItem = menu.querySelector('[data-action="solo"]');
    if (soloItem) {
        soloItem.textContent = targetCount === 1 ? "Mostra solo questo" : `Mostra solo questi ${targetCount}`;
    }
    const removeItem = menu.querySelector('[data-action="remove-device"]');
    if (removeItem) {
        removeItem.textContent = targetCount === 1 ? "Elimina dispositivo" : `Elimina ${targetCount} dispositivi`;
    }

    // Lista gruppi esistenti
    const groupList = document.getElementById("contextGroupList");
    const allGroups = getAllGroups();
    const stored = new Set(loadStoredGroups());
    if (groupList) {
        if (!allGroups.length) {
            groupList.innerHTML = `<div class="command-palette-empty" style="padding:8px 14px;font-size:11px;">Nessun gruppo</div>`;
        } else {
            groupList.innerHTML = allGroups
                .map(
                    (g) => `
                <div class="context-menu-item" data-group="${escapeHtml(g)}">
                    <span>${escapeHtml(g)}</span>
                    <div class="group-actions">
                        ${stored.has(g) ? `<button class="group-btn group-btn-delete" data-action="delete" data-group="${escapeHtml(g)}" title="Elimina gruppo">×</button>` : ""}
                    </div>
                </div>
            `
                )
                .join("");
            groupList.querySelectorAll('[data-group]').forEach((row) => {
                row.addEventListener("click", () => {
                    if (row.dataset.group) addDevicesToGroup(targets, row.dataset.group);
                    hideDeviceContextMenu();
                });
            });
            groupList.querySelectorAll('button[data-action="delete"]').forEach((btn) => {
                btn.addEventListener("click", (ev) => {
                    ev.stopPropagation();
                    if (confirm(`Rimuovere il gruppo "${btn.dataset.group}"?`)) removeGroup(btn.dataset.group);
                    hideDeviceContextMenu();
                });
            });
        }
    }

    // "Rimuovi da gruppo": solo i gruppi di cui almeno un device target
    // fa parte — inutile proporre gruppi dove non e' membro.
    const removeList = document.getElementById("contextGroupRemoveList");
    if (removeList) {
        const memberGroups = new Set();
        targets.forEach((s) => {
            const d = state.devices.find((dev) => dev.serial === s);
            (d?.tags || []).forEach((t) => memberGroups.add(t));
        });
        const groups = [...memberGroups].sort();
        if (!groups.length) {
            removeList.innerHTML = `<div class="command-palette-empty" style="padding:8px 14px;font-size:11px;">Nessun gruppo</div>`;
        } else {
            removeList.innerHTML = groups
                .map(
                    (g) => `
                <div class="context-menu-item" data-rmgroup="${escapeHtml(g)}">
                    <span>${escapeHtml(g)}</span>
                    <span class="group-btn" title="Rimuovi da questo gruppo">−</span>
                </div>
            `
                )
                .join("");
            removeList.querySelectorAll("[data-rmgroup]").forEach((row) => {
                row.addEventListener("click", () => {
                    if (row.dataset.rmgroup) removeDevicesFromGroup(targets, row.dataset.rmgroup);
                    hideDeviceContextMenu();
                });
            });
        }
    }

    // Comandi rapidi: applicati a tutti i device target
    menu.querySelectorAll("[data-cmd]").forEach((item) => {
        item.onclick = () => {
            runContextCommand(item.dataset.cmd, targets);
            hideDeviceContextMenu();
        };
    });

    // Posizione: flip stile menu nativi — se non c'e' spazio sotto il
    // cursore il menu si apre verso l'alto, se non c'e' a destra verso
    // sinistra. Il clamp semplice lasciava il menu tagliato su schermi
    // piccoli quando l'altezza superava il viewport.
    menu.style.display = "flex";
    menu.style.left = "0px";
    menu.style.top = "0px";
    const rect = menu.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let x = e.clientX;
    let y = e.clientY;
    if (x + rect.width > vw - 8) x = Math.max(8, x - rect.width);
    if (y + rect.height > vh - 8) y = Math.max(8, y - rect.height);
    x = Math.max(8, Math.min(x, vw - rect.width - 8));
    y = Math.max(8, Math.min(y, vh - Math.min(rect.height, vh - 16) - 8));
    menu.style.left = `${x}px`;
    menu.style.top = `${y}px`;

    const createBtn = document.getElementById("contextCreateGroup");
    if (createBtn) {
        createBtn.onclick = () => {
            const input = document.getElementById("contextNewGroup");
            const name = input?.value.trim();
            if (name) {
                addDevicesToGroup(targets, name);
                if (input) input.value = "";
            }
            hideDeviceContextMenu();
        };
    }

    const newGroupInput = document.getElementById("contextNewGroup");
    if (newGroupInput) {
        newGroupInput.onkeydown = (ke) => {
            if (ke.key === "Enter") {
                ke.preventDefault();
                createBtn?.click();
            }
        };
        setTimeout(() => newGroupInput.focus(), 0);
    }
}

function hideDeviceContextMenu() {
    const menu = document.getElementById("deviceContextMenu");
    if (menu) menu.style.display = "none";
}

// Comandi rapidi del menu contestuale: applicati a tutti i device target
// (il cliccato, oppure tutta la selezione se il cliccato e' selezionato).
function runContextCommand(cmd, serials) {
    const labels = {
        unlock: "Sblocca schermo",
        lock: "Blocca schermo",
        screen_on: "Accendi schermo",
        vol_up: "Volume +",
        vol_down: "Volume −",
        mute: "Muto",
        rotate: "Ruota schermo",
        restart_stream: "Riavvia stream",
    };
    serials.forEach((serial) => {
        switch (cmd) {
            case "unlock":
                // Wakeup + swipe dal basso: il gesto universale che apre
                // il tastierino PIN (KEYCODE_MENU funzionava solo su
                // poche ROM e solo a schermo acceso).
                wsSend({ action: "unlock_screen", serial });
                break;
            case "lock":
                wsSend({ action: "keyevent", serial, keycode: 26 });
                break;
            case "screen_on":
                wsSend({ action: "screen_on", serial });
                break;
            case "vol_up":
                wsSend({ action: "keyevent", serial, keycode: 24 });
                break;
            case "vol_down":
                wsSend({ action: "keyevent", serial, keycode: 25 });
                break;
            case "mute":
                wsSend({ action: "keyevent", serial, keycode: 164 });
                break;
            case "rotate":
                wsSend({ action: "rotate", serial });
                break;
            case "restart_stream":
                wsSend({ action: "restart_stream", serial });
                break;
        }
    });
    toast(`${labels[cmd] || cmd} → ${serials.length} dispositivo/i`, "success");
}

function updateDeviceCell(cell, dev) {
    // In fullscreen la cella vive in <body>: il parent non e' la card e
    // body.querySelector(".device-name") matcherebbe l'etichetta di un
    // altro device. I lookup su card si fanno solo dentro una vera card.
    const card = cell.parentElement?.classList.contains("device-card")
        ? cell.parentElement
        : null;

    // Nome
    const nameEl = card?.querySelector(".device-name");
    if (nameEl && nameEl !== document.activeElement) {
        nameEl.value = dev.display_name;
        nameEl.size = Math.max(4, (dev.display_name || dev.serial).length + 2);
    }

    // Stato
    const dot = card?.querySelector(".status-dot");
    if (dot) dot.className = `status-dot ${dev.status}`;
    const statusLabel = card?.querySelector(".device-status-label");
    if (statusLabel && statusLabel !== document.activeElement) {
        statusLabel.textContent = getDeviceStatusLabel(dev.status);
        statusLabel.className = `device-status-label status-${dev.status}`;
    }

    // Gruppi / tag: innerHTML riscritto solo se i tag sono cambiati —
    // prima lo faceva a OGNI update di stato (1/s per cella, reflow
    // della griglia continuo con 26 device).
    const tagsEl = card?.querySelector(".device-tags");
    if (tagsEl) {
        const tags = dev.tags || [];
        const joined = tags.join(",");
        if (tagsEl.dataset.tags !== joined) {
            tagsEl.dataset.tags = joined;
            tagsEl.innerHTML = tags
                .slice(0, 5)
                .map((t) => `<span class="device-tag">${escapeHtml(t)}</span>`)
                .join("");
            if (tags.length > 5) {
                tagsEl.innerHTML += `<span class="device-tag">+${tags.length - 5}</span>`;
            }
        }
    }

    // Etichetta laterale del fullscreen (il nome non sta nella cella)
    if (cell._fsLeftLabel) {
        cell._fsLeftLabel.textContent = dev.display_name || dev.serial;
    }

    // Classi celle
    cell.classList.toggle("focused", dev.serial === state.focusedSerial);
    cell.classList.toggle("offline", dev.status !== "online");
    cell.classList.toggle("autoclick", !!dev.autoclick);
    cell.classList.toggle("selected", dev.selected);

    // Checkbox
    const checkbox = cell.querySelector(".device-select");
    if (checkbox) checkbox.checked = dev.selected;

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
        // Con AutoWatch attivo il WS parte solo se la cella e' visibile
        // (o in fullscreen); le celle fuori vista restano in pausa.
        if (shouldWatch(dev.serial)) {
            // Avvia WebSocket binario per stream a latenza minima (con cooldown)
            const retryAt = parseInt(feed.dataset.wsRetryAt, 10) || 0;
            const ready = !feed.dataset.wsActive || (feed.dataset.wsActive !== dev.serial && Date.now() > retryAt);
            if (ready) {
                startStreamWs(feed, dev.serial);
            }
        }

        const streamBtn = cell.querySelector('[data-action="stream_toggle"]');
        if (streamBtn) streamBtn.innerHTML = '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>';
    } else {
        stopStreamWs(feed);

        const streamBtn = cell.querySelector('[data-action="stream_toggle"]');
        if (streamBtn) streamBtn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>';
    }
}

// =====================================================================
// Stream WebSocket (latenza minima)
// =====================================================================

const streamSessions = {};

// ---------------------------------------------------------------------
// AutoWatch: il WS video resta aperto solo per le celle visibili nella
// griglia (con margine) e per quella in fullscreen. Le celle che escono
// dallo schermo vengono messe in pausa dopo un breve debounce; il server
// continua comunque a far girare scrcpy per tutti i device.
// ---------------------------------------------------------------------

let _awObserver = null;
const _awTimers = {}; // serial -> timeout di messa in pausa

function shouldWatch(serial) {
    if (!_awObserver) return true; // niente IntersectionObserver: guarda tutto
    return !state.autoWatch
        || state.fullscreenSerial === serial
        || state.visibleSerials.has(serial);
}

// Debounce 1500ms: se la cella torna visibile il timer viene annullato.
function awSchedulePause(cell, serial) {
    if (_awTimers[serial]) clearTimeout(_awTimers[serial]);
    _awTimers[serial] = setTimeout(() => {
        delete _awTimers[serial];
        if (state.visibleSerials.has(serial) || state.fullscreenSerial === serial) return;
        const feed = cell.querySelector('.device-feed');
        if (feed && feed.dataset.wsActive === serial) {
            // H264/JPEG: il WS RESTA APERTO — si smette solo di decodificare
            // e disegnare. Prima stopStreamWs chiudeva la sessione: al
            // ritorno in vista il resubscribe trovava il GOP a meta' e
            // chiedeva un keyframe => reset_video = re-init di cattura +
            // encoder sul telefono. Con gli scroll della griglia erano
            // ~12 reset/min su 26 device. Ora il rientro e' immediato e
            // gratis: il prossimo frame viene disegnato e basta.
            if (state.videoMode === 'mse') {
                // MSE non puo' sospendere: il SourceBuffer si riempirebbe.
                stopStreamWs(feed);
            } else {
                feed._awPaused = true;
            }
            const ph = cell.querySelector('.device-feed-placeholder');
            if (ph) {
                const icon = ph.querySelector('.icon');
                const txt = ph.querySelector('span');
                if (icon) icon.textContent = '⏸';
                if (txt) txt.textContent = 'In pausa (fuori vista)';
                ph.style.display = 'flex';
            }
        }
    }, 1500);
}

function awObserveCell(cell) {
    if (_awObserver) _awObserver.observe(cell);
}

function awUnobserveCell(cell) {
    const serial = cell.dataset.serial;
    if (_awObserver) _awObserver.unobserve(cell);
    if (serial) {
        state.visibleSerials.delete(serial);
        if (_awTimers[serial]) { clearTimeout(_awTimers[serial]); delete _awTimers[serial]; }
    }
}

function initAutoWatch() {
    if (!("IntersectionObserver" in window)) return;
    const root = document.getElementById('gridContainer');
    _awObserver = new IntersectionObserver((entries) => {
        for (const entry of entries) {
            const cell = entry.target;
            const serial = cell.dataset.serial;
            if (!serial) continue;
            if (entry.isIntersecting) {
                state.visibleSerials.add(serial);
                if (_awTimers[serial]) { clearTimeout(_awTimers[serial]); delete _awTimers[serial]; }
                if (!state.autoWatch) continue;
                // Cella appena diventata visibile: se il WS era rimasto
                // aperto in pausa (_awPaused) basta riattivare il disegno —
                // ripresa immediata, nessun resubscribe, nessun keyframe.
                const dev = state.devices.find((d) => d.serial === serial);
                const feed = cell.querySelector('.device-feed');
                if (feed) feed._awPaused = false;
                if (dev && dev.streaming && feed && feed.dataset.wsActive !== serial) {
                    const retryAt = parseInt(feed.dataset.wsRetryAt, 10) || 0;
                    if (Date.now() > retryAt) startStreamWs(feed, serial);
                }
            } else {
                state.visibleSerials.delete(serial);
                if (!state.autoWatch) continue;
                awSchedulePause(cell, serial);
            }
        }
    }, { root, rootMargin: '200px 0px', threshold: 0 });
    // Osserva anche le celle gia' presenti nel DOM
    document.querySelectorAll('.device-cell').forEach(awObserveCell);
}

// Modalita' Remota: questo browser riceve solo keyframe (?lite=1).
// Per-browser, non tocca gli altri client. Default ON fuori da localhost.
function remoteLiteMode() {
    const s = localStorage.getItem("griddroid_remote_lite");
    if (s !== null) return s === "1";
    return !["localhost", "127.0.0.1", "::1"].includes(location.hostname);
}

function findStartCode(b, start) {
    // Trova il prossimo start code Annex-B (00 00 01 o 00 00 00 01).
    // indexOf(1) gira in C: il loop byte-per-byte costava ~ms per frame —
    // i byte 0x01 sono rari nel payload, i candidati sono pochi e ognuno
    // verifica solo i due byte precedenti.
    for (let i = Math.max(start + 2, 2); i < b.length;) {
        i = b.indexOf(1, i);
        if (i < 0) return -1;
        if (b[i - 1] === 0 && b[i - 2] === 0) {
            const p = i - 2;
            // Se il byte prima e' 0 il vero inizio e' p-1 (forma 4 byte).
            return (p > start && b[p - 1] === 0) ? p - 1 : p;
        }
        i += 1;
    }
    return -1;
}

function parseSpsPpsFromAnnexB(data) {
    let sps = null, pps = null;
    let i = 0;
    while (i + 4 < data.length) {
        const p = findStartCode(data, i);
        if (p < 0) break;
        const scLen = data[p + 2] === 1 ? 3 : 4;
        if (p + scLen >= data.length) break;
        const nalType = data[p + scLen] & 0x1F;
        const nxt = findStartCode(data, p + scLen);
        const j = nxt < 0 ? data.length : nxt;
        const nalData = data.subarray(p + scLen, j);
        if (nalType === 7) sps = nalData;
        else if (nalType === 8) pps = nalData;
        if (sps && pps) break;
        i = j;
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
    // Confini via findStartCode (indexOf in C): gira a ogni frame nel
    // percorso senza Worker.
    for (let i = 0;;) {
        const p = findStartCode(data, i);
        if (p < 0) break;
        nalStarts.push({ pos: p, scLen: data[p + 2] === 1 ? 3 : 4 });
        i = p + 3;
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

// =====================================================================
// Native MSE remuxer: sostituisce JMuxer per qualita' piena a 1080/1440.
// JMuxer batcha i frame (flushingTime), pulisce il buffer (clearBuffer)
// e gestisce male i cambi di risoluzione. Questo remuxer costruisce fMP4
// nativo: init segment da SPS/PPS + media segment per ogni frame, zero
// latenza, nessun batching.
// =====================================================================

function _fmp4Box(type, payload) {
    const len = 8 + (payload ? payload.length : 0);
    const buf = new Uint8Array(len);
    new DataView(buf.buffer).setUint32(0, len);
    buf[4] = type.charCodeAt(0);
    buf[5] = type.charCodeAt(1);
    buf[6] = type.charCodeAt(2);
    buf[7] = type.charCodeAt(3);
    if (payload) buf.set(payload, 8);
    return buf;
}

function _fmp4Concat(...boxes) {
    let total = 0;
    for (const b of boxes) total += b.length;
    const out = new Uint8Array(total);
    let off = 0;
    for (const b of boxes) { out.set(b, off); off += b.length; }
    return out;
}

// Init segment: ftyp + moov con avcC da SPS/PPS.
function _fmp4InitSegment(sps, pps) {
    const avcC = buildAvcDescription(sps, pps);
    if (!avcC) return null;

    const ftyp = _fmp4Box('ftyp', new Uint8Array([
        0x69,0x73,0x6F,0x6D, 0x00,0x00,0x00,0x01,
        0x69,0x73,0x6F,0x6D, 0x61,0x76,0x63,0x31,
    ]));

    const mvhd = _fmp4Box('mvhd', new Uint8Array([
        0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0x03,0xE8, 0,0,0,0,
        0,1,0,0, 0x01,0,0,0, 0,0,0,0, 0,0,0,0,
        0,1,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0,
        0,1,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0,
        0x40,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,2,
    ]));

    const tkhd = _fmp4Box('tkhd', new Uint8Array([
        0,0,0,7, 0,0,0,0, 0,0,0,0, 0,0,0,1, 0,0,0,0,
        0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0,
        0,1,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0,
        0,1,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0,
        0x40,0,0,0, 0,0,0,0, 0,0,0,0,
    ]));

    const mdhd = _fmp4Box('mdhd', new Uint8Array([
        0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0x03,0xE8, 0,0,0,0, 0x55,0xC4,0,0,
    ]));

    const hdlr = _fmp4Box('hdlr', new Uint8Array([
        0,0,0,0, 0,0,0,0, 0x76,0x69,0x64,0x65,
        0,0,0,0, 0,0,0,0, 0,0,0,0, 0,
    ]));

    const vmhd = _fmp4Box('vmhd', new Uint8Array([0,0,0,1, 0,0,0,0, 0,0,0,0]));
    const url_ = _fmp4Box('url ', new Uint8Array([0,0,0,1]));
    const dref = _fmp4Box('dref', _fmp4Concat(new Uint8Array([0,0,0,0, 0,0,0,1]), url_));
    const dinf = _fmp4Box('dinf', dref);

    // avc1 sample entry: width/height = 0 (MSE li ricava dallo stream)
    const avc1Hdr = new Uint8Array([
        0,0,0,0, 0,0,0,1,
        0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0,
        0,0,0,0, 0,0,0,0,
        0,0x48,0,0, 0,0x48,0,0, 0,0,0,0, 0,1,
        0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
        0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
        0,0x18, 0xFF,0xFF,
    ]);
    const avc1 = _fmp4Box('avc1', _fmp4Concat(avc1Hdr, _fmp4Box('avcC', avcC)));

    const stsd = _fmp4Box('stsd', _fmp4Concat(new Uint8Array([0,0,0,0, 0,0,0,1]), avc1));
    const stts = _fmp4Box('stts', new Uint8Array([0,0,0,0, 0,0,0,0]));
    const stsc = _fmp4Box('stsc', new Uint8Array([0,0,0,0, 0,0,0,0]));
    const stsz = _fmp4Box('stsz', new Uint8Array([0,0,0,0, 0,0,0,0, 0,0,0,0]));
    const stco = _fmp4Box('stco', new Uint8Array([0,0,0,0, 0,0,0,0]));
    const stbl = _fmp4Box('stbl', _fmp4Concat(stsd, stts, stsc, stsz, stco));
    const minf = _fmp4Box('minf', _fmp4Concat(vmhd, dinf, stbl));
    const mdia = _fmp4Box('mdia', _fmp4Concat(mdhd, hdlr, minf));
    const trak = _fmp4Box('trak', _fmp4Concat(tkhd, mdia));
    const trex = _fmp4Box('trex', new Uint8Array([0,0,0,0, 0,0,0,1, 0,0,0,1, 0,0,0,0, 0,0,0,0, 0,0,0,0]));
    const mvex = _fmp4Box('mvex', trex);
    const moov = _fmp4Box('moov', _fmp4Concat(mvhd, trak, mvex));

    return _fmp4Concat(ftyp, moov);
}

// Media segment: moof + mdat per un singolo frame AVCC.
// Struttura fissa: moof = 92 byte, mdat header = 8 byte, data_offset = 100.
function _fmp4MediaSegment(seq, avcc, timestamp, duration) {
    const mfhd = _fmp4Box('mfhd', new Uint8Array([
        0,0,0,0,
        (seq >>> 24)&0xFF, (seq >>> 16)&0xFF, (seq >>> 8)&0xFF, seq&0xFF,
    ]));
    const tfhd = _fmp4Box('tfhd', new Uint8Array([0,0,0,0x20, 0,0,0,1]));
    const tfdt = _fmp4Box('tfdt', new Uint8Array([
        0,0,0,0,
        (timestamp >>> 24)&0xFF, (timestamp >>> 16)&0xFF, (timestamp >>> 8)&0xFF, timestamp&0xFF,
    ]));
    // trun flags 0x000301: data-offset + sample-duration + sample-size
    const trunData = new Uint8Array(20);
    const tv = new DataView(trunData.buffer);
    tv.setUint32(0, 0x000301);
    tv.setUint32(4, 1);          // sample_count
    tv.setInt32(8, 100);         // data_offset = moof.length(92) + mdat_header(8)
    tv.setUint32(12, duration);  // sample_duration
    tv.setUint32(16, avcc.length); // sample_size
    const trun = _fmp4Box('trun', trunData);
    const traf = _fmp4Box('traf', _fmp4Concat(tfhd, tfdt, trun));
    const moof = _fmp4Box('moof', _fmp4Concat(mfhd, traf));
    const mdat = _fmp4Box('mdat', avcc);
    return _fmp4Concat(moof, mdat);
}

// Crea un remuxer MSE nativo per un elemento <video>.
// onReady: callback quando il MediaSource e' aperto.
// Ritorna { feed, destroy }.
function createMseRemuxer(videoEl, onReady, onError) {
    const ms = new MediaSource();
    videoEl.src = URL.createObjectURL(ms);

    let sb = null;
    let inited = false;
    let failed = false;
    let seq = 0;
    let ts = 0;
    let queue = [];
    let lastSps = null;
    let lastPps = null;
    const FRAME_DUR = 50; // 20fps @ timescale 1000ms

    function _append(data) {
        if (sb && !sb.updating) {
            try { sb.appendBuffer(data); } catch (e) { onError?.(e); }
        } else {
            queue.push(data);
        }
    }

    function _flush() {
        while (queue.length > 0 && sb && !sb.updating) {
            try { sb.appendBuffer(queue.shift()); } catch (e) { onError?.(e); break; }
        }
        // Mantieni solo gli ultimi 2s di buffer per latenza minima
        if (sb && !sb.updating && sb.buffered.length > 0) {
            const end = sb.buffered.end(sb.buffered.length - 1);
            const start = sb.buffered.start(0);
            if (end - start > 2) {
                try { sb.remove(start, end - 2); } catch (e) {}
            }
        }
    }

    function _init(sps, pps) {
        const initSeg = _fmp4InitSegment(sps, pps);
        if (!initSeg) return false;
        const profile = sps[1].toString(16).padStart(2, '0');
        const constraints = sps[2].toString(16).padStart(2, '0');
        const level = sps[3].toString(16).padStart(2, '0');
        const codec = `avc1.${profile}${constraints}${level}`;

        if (sb) {
            try { ms.removeSourceBuffer(sb); } catch (e) {}
            sb = null; inited = false; queue = [];
        }
        // Codec reale dallo stream, poi fallback canonici: alcuni browser
        // accettano solo stringhe "note" (42E01E Baseline, 4D401F Main,
        // 640028 High) anche quando il profilo dichiarato e' valido.
        const candidates = [codec, 'avc1.42E01E', 'avc1.4D401F', 'avc1.640028'];
        let lastErr = null;
        for (const c of candidates) {
            try {
                sb = ms.addSourceBuffer(`video/mp4; codecs="${c}"`);
                break;
            } catch (e) { lastErr = e; sb = null; }
        }
        if (!sb) {
            // Nessun codec accettato: inutile riprovare a ogni keyframe
            // (errore per frame nel log). Fallisce una volta e basta.
            failed = true;
            onError?.(lastErr);
            return false;
        }
        try {
            sb.mode = 'segments';
            sb.addEventListener('updateend', _flush);
            sb.addEventListener('error', (e) => onError?.(e));
        } catch (e) { onError?.(e); return false; }
        _append(initSeg);
        inited = true;
        lastSps = sps; lastPps = pps;
        return true;
    }

    function feed(isKey, h264Data) {
        if (failed) return;
        const spspps = parseSpsPpsFromAnnexB(h264Data);
        if (isKey && spspps.sps && spspps.pps) {
            const changed = !lastSps || !lastPps ||
                lastSps.length !== spspps.sps.length ||
                lastPps.length !== spspps.pps.length ||
                lastSps.some((b, i) => b !== spspps.sps[i]) ||
                lastPps.some((b, i) => b !== spspps.pps[i]);
            if (changed || !inited) _init(spspps.sps, spspps.pps);
        }
        if (!inited) return;
        const avcc = annexBToAVCC(h264Data);
        if (!avcc) return;
        seq++;
        const seg = _fmp4MediaSegment(seq, avcc, ts, FRAME_DUR);
        ts += FRAME_DUR;
        _append(seg);
    }

    function destroy() {
        queue = [];
        if (sb) { try { ms.removeSourceBuffer(sb); } catch (e) {} sb = null; }
        inited = false;
    }

    ms.addEventListener('sourceopen', () => onReady?.());
    return { feed, destroy };
}

const _frameBuffers = new Map();

function scheduleCanvasDraw(feedEl, serial, source, width, height) {
    // Cella in pausa AutoWatch (fuori vista): il worker continua a
    // decodificare per non spezzare la catena dei delta, ma il disegno
    // viene saltato — il frame va comunque chiuso.
    if (feedEl._awPaused) {
        try { source.close(); } catch (e) {}
        return;
    }
    // Frame precedente non ancora disegnato: va chiuso subito, altrimenti
    // il VideoFrame resta aperto e blocca il pool del decoder hw.
    const prev = _frameBuffers.get(serial);
    if (prev && prev.source !== source) {
        try { prev.source.close(); } catch (e) {}
    }
    _frameBuffers.set(serial, { source, width, height });
    if (feedEl._drawScheduled) return;
    feedEl._drawScheduled = true;
    requestAnimationFrame(() => {
        feedEl._drawScheduled = false;
        const f = _frameBuffers.get(serial);
        if (!f) return;
        _frameBuffers.delete(serial);
        const ctx = feedEl.getContext('2d', { alpha: false });
        if (!ctx) return;
        if (feedEl.width !== f.width || feedEl.height !== f.height) {
            feedEl.width = f.width;
            feedEl.height = f.height;
        }
        ctx.drawImage(f.source, 0, 0, f.width, f.height);
        try { f.source.close(); } catch (e) {}
        feedEl.style.display = 'block';
        const placeholder = feedEl.parentElement.querySelector('.device-feed-placeholder');
        if (placeholder) placeholder.style.display = 'none';
    });
}

function startStreamWs(feedEl, serial) {
    stopStreamWs(feedEl);
    feedEl._awPaused = false;

    const useWorker = feedEl.tagName === "CANVAS" && typeof VideoDecoder !== "undefined" && typeof Worker !== "undefined";
    const useWebCodecs = feedEl.tagName === "CANVAS" && typeof VideoDecoder !== "undefined" && typeof Worker === "undefined";

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

    const session = { ws: null, feedEl, mseRemuxer: null, decoder: null, ctx: null, pts: 0, configured: false, gotKey: false };
    // Modalita' JPEG: il server decodifica con ffmpeg e manda JPEG pronti
    // (flag 0x02). Il browser disegna senza decodificare H264: serve solo
    // un canvas, niente worker/decoder/MSE.
    const jpegMode = state.videoMode === 'jpeg';
    if (jpegMode && feedEl.tagName !== 'CANVAS') {
        setPlaceholder('Modalità JPEG richiede canvas', '⚠️');
        feedEl.dataset.wsRetryAt = Date.now() + 5000;
        return;
    }
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const wsParams = new URLSearchParams();
    if (remoteLiteMode()) wsParams.set('lite', '1');
    if (jpegMode) wsParams.set('mode', 'jpeg');
    const wsQs = wsParams.toString() ? `?${wsParams.toString()}` : "";
    const ws = new WebSocket(`${protocol}//${location.host}/ws/stream/${serial}${wsQs}`);
    ws.binaryType = "arraybuffer";
    session.ws = ws;
    session.jpegMode = jpegMode;

    if (!jpegMode && useWorker) {
        const worker = new Worker('/static/decoder-worker.js?v=126');
        let gotKey = false;
        worker.onmessage = (event) => {
            const msg = event.data;
            if (msg.type === 'ready') {
                console.log(`[Worker] decoder ready ${serial}`);
            } else if (msg.type === 'frame') {
                if (msg.bitmap) {
                    scheduleCanvasDraw(feedEl, serial, msg.bitmap, msg.codedWidth, msg.codedHeight);
                } else if (msg.frame) {
                    scheduleCanvasDraw(feedEl, serial, msg.frame, msg.codedWidth, msg.codedHeight);
                }
            } else if (msg.type === 'needkey') {
                // Il decoder ha perso frame: chiediamo al server un keyframe
                // fresco invece di restare congelati sull'ultimo frame buono.
                if (ws.readyState === WebSocket.OPEN) {
                    try { ws.send('k'); } catch (e) { }
                }
            } else if (msg.type === 'error') {
                console.error(`[Worker] ${serial}:`, msg.message);
                ws.close();
            }
        };
        worker.onerror = (err) => {
            console.error(`[Worker] ${serial}:`, err);
            ws.close();
        };
        session.worker = worker;
    }

    if (!jpegMode && useWebCodecs) {
        session.ctx = feedEl.getContext("2d", { alpha: false });
    } else if (!jpegMode && !useWorker && typeof MediaSource === "undefined") {
        console.error("MediaSource non supportato");
        setPlaceholder('Errore player MSE', '⚠️');
        return;
    }

    ws.onopen = () => {
        setPlaceholder('Connessione in corso...', '⏳');
    };

    ws.onmessage = (event) => {
        const data = new Uint8Array(event.data);
        if (data.length < 2) return;

        // Flag 0x02: frame JPEG dal transcoder server-side
        if (data[0] === 2) {
            // In pausa AutoWatch: i JPEG sono autoconsistenti, si possono
            // scartare interamente — zero CPU di decodifica e la ripresa
            // resta immediata perche' il WS resta aperto.
            if (feedEl._awPaused) return;
            createImageBitmap(new Blob([data.subarray(1)], { type: 'image/jpeg' }))
                .then((bm) => scheduleCanvasDraw(feedEl, serial, bm, bm.width, bm.height))
                .catch((err) => console.error(`[JPEG] ${serial}:`, err));
            return;
        }

        const isKey = data[0] === 1;
        const h264Data = data.subarray(1);

        if (useWorker) {
            if (session.worker) {
                session.worker.postMessage({
                    type: 'decode',
                    payload: { isKey, data: h264Data },
                });
            }
            return;
        }

        if (useWebCodecs) {
            if (!session.configured) {
                const spspps = parseSpsPpsFromAnnexB(h264Data);
                if (!spspps.sps || !spspps.pps) {
                    // senza SPS/PPS non possiamo ancora configurare il decoder
                    return;
                }
                const desc = buildAvcDescription(spspps.sps, spspps.pps);
                if (!desc) return;
                const profile = spspps.sps[1].toString(16).padStart(2, '0');
                const constraints = spspps.sps[2].toString(16).padStart(2, '0');
                const level = spspps.sps[3].toString(16).padStart(2, '0');
                const codec = `avc1.${profile}${constraints}${level}`;
                try {
                    session.decoder = new VideoDecoder({
                        output: (frame) => {
                            if (!frame) return;
                            if (feedEl.width !== frame.codedWidth || feedEl.height !== frame.codedHeight) {
                                feedEl.width = frame.codedWidth;
                                feedEl.height = frame.codedHeight;
                            }
                            if (session.ctx) {
                                session.ctx.drawImage(frame, 0, 0, feedEl.width, feedEl.height);
                            }
                            frame.close();
                            feedEl.style.display = 'block';
                            setPlaceholder('', '', false);
                        },
                        error: (err) => {
                            console.error(`VideoDecoder ${serial}:`, err);
                            ws.close();
                        },
                    });
                    session.decoder.configure({ codec, description: desc, hardwareAcceleration: "prefer-hardware" });
                    session.configured = true;
                } catch (e) {
                    console.error(`Config VideoDecoder ${serial}:`, e);
                    ws.close();
                    return;
                }
            }
            if (!session.configured) return;

            const avcc = annexBToAVCC(h264Data);
            if (!avcc) return;
            // Dopo un delta scartato la catena e' rotta: i delta successivi
            // non decodificano, aspettiamo il keyframe richiesto al server.
            if (session.needKey && !isKey) return;
            try {
                // Latenza zero: se il decoder e' indietro, scartiamo i
                // delta. Se anche i keyframe si accumulano (> 6), forziamo
                // un reset del decoder per evitare che il buffer interno
                // cresca (causa del lag di 1s in fullscreen).
                // Soglie rilassate: con 20+ stream il decoder hw va in
                // backlog per picchi brevi e ogni richiesta keyframe costa
                // un reset_video (re-init completa della cattura).
                if (session.decoder.decodeQueueSize > 6) {
                    if (!isKey) {
                        // Chiediamo un keyframe (throttle 2s) invece di
                        // restare corrotti fino al prossimo errore.
                        session.needKey = true;
                        const now = Date.now();
                        if (!session.lastKeyReq || now - session.lastKeyReq > 2000) {
                            session.lastKeyReq = now;
                            try { ws.send('k'); } catch (e) { }
                        }
                        return;
                    }
                    // Reset soft del decoder: flush, poi se non basta chiudi
                    // e riconfigura al prossimo keyframe.
                    try {
                        session.decoder.flush();
                        if (session.decoder.decodeQueueSize > 10) {
                            session.configured = false;
                            session.gotKey = false;
                            session.decoder.close();
                            session.decoder = null;
                            return; // aspetta un nuovo keyframe per riconfigurare
                        }
                    } catch (e) {
                        console.warn(`Decoder reset ${serial}:`, e);
                    }
                }
                session.pts += 500_000;
                const chunk = new EncodedVideoChunk({ type: isKey ? "key" : "delta", timestamp: session.pts, duration: 0, data: avcc });
                session.decoder.decode(chunk);
                if (isKey) session.needKey = false;
            } catch (e) {
                console.error(`Decode ${serial}:`, e);
            }
        } else {
            // MSE nativo: remuxer fMP4 senza JMuxer (qualita' piena a 1080/1440).
            // JMuxer batcha/pulisce buffer e degrada le alte risoluzioni.
            if (!session.mseRemuxer) {
                try {
                    if (DEBUG_STREAM()) console.log(`[MSE] init remuxer nativo ${serial}`);
                    session.mseRemuxer = createMseRemuxer(feedEl, () => {
                        feedEl.style.display = 'block';
                        setPlaceholder('', '', false);
                        feedEl.play().catch(() => {});
                    }, (err) => {
                        console.error(`MSE remuxer ${serial}:`, err);
                    });
                } catch (err) {
                    console.error(`Inizializzazione MSE ${serial}:`, err);
                    setPlaceholder('Errore player MSE', '⚠️');
                    return;
                }
            }
            if (!session.gotKey) {
                if (!isKey) return;
                session.gotKey = true;
            }
            try {
                session.mseRemuxer.feed(isKey, h264Data);
            } catch (err) {
                console.error(`Feed MSE ${serial}:`, err);
            }
        }
    };

    ws.onclose = (ev) => {
        // ffmpeg mancante sul server: torniamo a H264 e riconnettiamo.
        if (session.jpegMode && ev && ev.reason && ev.reason.indexOf('ffmpeg') !== -1) {
            toast('ffmpeg non disponibile: torno a H264', 'warn');
            state.videoMode = 'h264';
            localStorage.setItem('griddroid.videoMode', 'h264');
            location.reload();
            return;
        }
        setPlaceholder('Connessione persa', '📵');
        if (streamSessions[serial] === session) {
            feedEl.dataset.wsActive = "";
            // Jitter: su VPN tanti stream che riprovano insieme saturano la rete
            feedEl.dataset.wsRetryAt = Date.now() + 3000 + Math.random() * 3000;
            delete streamSessions[serial];
        }
        if (session.mseRemuxer) {
            try { session.mseRemuxer.destroy(); } catch (e) { }
            session.mseRemuxer = null;
        }
        if (session.decoder) {
            try { session.decoder.close(); } catch (e) { }
        }
        // Senza terminate il worker (e il suo VideoDecoder hw) restava vivo
        // a ogni riconnessione: con 20+ telefoni si accumulavano decoder zombie.
        if (session.worker) {
            try { session.worker.terminate(); } catch (e) { }
            session.worker = null;
        }
    };

    ws.onerror = (e) => {
        console.error(`WS stream ${serial}:`, e);
        setPlaceholder('Errore connessione', '⚠️');
        ws.close();
    };

    feedEl.dataset.wsActive = serial;
    feedEl.dataset.wsRetryAt = "";
    streamSessions[serial] = session;
}

// Riavvia tutte le sessioni stream attive: usato quando cambia un flag
// per-browser (modalita' remota, modalita' video) che va applicato subito.
function restartAllFeeds() {
    for (const feed of document.querySelectorAll(".device-feed[data-ws-active]")) {
        const serial = feed.dataset.wsActive;
        if (!serial) continue;
        stopStreamWs(feed);
        feed.dataset.wsRetryAt = "";
        startStreamWs(feed, serial);
    }
}

function stopStreamWs(feedEl) {
    feedEl._awPaused = false;
    const serial = feedEl.dataset.wsActive;
    const session = serial && streamSessions[serial];
    // La sessione registrata puo' appartenere a un'altra cella dello
    // stesso serial (es. duplicato creato mentre il device era in
    // fullscreen): chiuderla lascerebbe quella cella con wsActive
    // valorizzato ma stream morto e senza riconnessione. Si chiude solo
    // la sessione che appartiene davvero a questo feed.
    if (session && session.feedEl === feedEl) {
        try {
            // Chiudere un WS ancora in CONNECTING genera un warning in console
            if (session.ws.readyState === WebSocket.CONNECTING) {
                session.ws.onopen = () => session.ws.close();
            } else {
                session.ws.close();
            }
        } catch (e) { }
        try {
            if (session.mseRemuxer) session.mseRemuxer.destroy();
            if (session.decoder) session.decoder.close();
            if (session.worker) { session.worker.terminate(); }
        } catch (e) { }
        delete streamSessions[serial];
        const pending = _frameBuffers.get(serial);
        if (pending) {
            try { pending.source.close(); } catch (e) {}
            _frameBuffers.delete(serial);
        }
    }
    feedEl.dataset.wsActive = "";
    feedEl.dataset.wsRetryAt = Date.now() + 3000 + Math.random() * 3000;
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
 * Il video usa object-fit: contain, quindi è centrato con bande
 * nere (letterbox): senza compensarle il tocco risulta sfalsato.
 */
function feedCoords(feedEl, ev) {
    const vw = feedEl.videoWidth || feedEl.width;
    const vh = feedEl.videoHeight || feedEl.height;
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
    let lastPointerDownTime = 0;
    const DBLCLICK_THRESHOLD = 320; // ms

    // Invia i movimenti al massimo una volta per frame: evita di saturare
    // il WebSocket mantenendo il drag perfettamente fluido.
    function flushMove() {
        moveScheduled = false;
        if (!dragging || !pendingMove) return;
        wsSend(pendingMove);
        pendingMove = null;
    }

    // Il click default sul <video> alterna play/pause: lo disabilitiamo,
    // altrimenti ogni tocco congela lo stream MSE.
    feedEl.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        if (typeof feedEl.play === "function") {
            feedEl.play().catch(() => {});
        }
    });

    feedEl.addEventListener("pointerdown", (ev) => {
        if (ev.button !== 0 || ev.ctrlKey || ev.metaKey) return;
        const c = feedCoords(feedEl, ev);
        if (!c) return;

        // Rilascia il focus da input attivi (barra ricerca ecc.):
        // preventDefault qui sotto blocca il blur automatico del browser,
        // e il click sulla cella non arriva (stopPropagation sul feed) —
        // senza questo la tastiera restava intrappolata nel box di testo.
        const active = document.activeElement;
        if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.tagName === "SELECT")) {
            active.blur();
        }

        ev.preventDefault();

        // Se il secondo click di un doppio clic arriva troppo presto,
        // ignoralo: evita il doppio-tap che può bloccare/spegnere lo schermo.
        const now = Date.now();
        if (now - lastPointerDownTime < DBLCLICK_THRESHOLD) {
            lastPointerDownTime = 0;
            return;
        }
        lastPointerDownTime = now;

        feedEl.setPointerCapture(ev.pointerId);
        dragging = true;

        // Ultimo punto toccato: usato come target dell'auto-clicker
        state.lastTap[serial] = { x: c.x, y: c.y };

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

    // Rotella del mouse → scroll nativo sul telefono.
    // Alt+rotellina lascia l'evento alla pagina per scrollare la griglia.
    // I delta vengono accumulati e ridotti: uno scatto di rotellina non
    // deve mandare un evento a piena intensità (scroll troppo veloce).
    const SCROLL_SPEED = 0.35; // frazione di scroll per scatto standard
    let accX = 0, accY = 0;
    feedEl.addEventListener("wheel", (ev) => {
        if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
        const c = feedCoords(feedEl, ev);
        if (!c) return;
        ev.preventDefault();

        accX += -ev.deltaX;
        accY += -ev.deltaY;
        // Soglia minima: ignora micro-delta dei trackpad ad alta risoluzione
        if (Math.abs(accX) < 40 && Math.abs(accY) < 40) return;

        const hscroll = Math.max(-1, Math.min(1, (accX / 100) * SCROLL_SPEED));
        const vscroll = Math.max(-1, Math.min(1, (accY / 100) * SCROLL_SPEED));
        accX = 0;
        accY = 0;

        wsSend({ action: "focus", serial: serial });
        wsSend({
            action: "scroll",
            x: c.x, y: c.y, w: c.w, h: c.h,
            hscroll: hscroll,
            vscroll: vscroll,
        });
    }, { passive: false });

}

// Tastiera globale → input text / keyevent
document.addEventListener("keydown", (e) => {
    // Ignora se il focus è su un input o textarea
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT") return;

    // Copia / incolla / taglia: Ctrl (o Cmd) + C/V/X
    if (e.ctrlKey || e.metaKey) {
        const k = e.key.toLowerCase();
        if (k === "c" || k === "v" || k === "x") {
            e.preventDefault();
            sendClipboardShortcut(k);
            return;
        }
        if (k === "a") {
            e.preventDefault();
            if (e.shiftKey) deselectAllDevices();
            else selectAllDevices();
            return;
        }
        if (k === "k") {
            e.preventDefault();
            openCommandPalette();
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
        // Ctrl+V: prova a incollare il testo degli appunti del PC (o prompt su HTTP remoto)
        const text = await readFromClipboard();
        if (text !== null) {
            if (text.trim()) {
                wsSend({ action: "text", text });
                toast("Testo incollato sul dispositivo", "success");
            }
            return;
        }
        // fallback: manda il Ctrl+V nativo del dispositivo
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
    const dev = state.devices.find((d) => d.serial === serial);
    switch (action) {
        case "fullscreen":
            toggleFullscreen(serial, cell);
            break;
        case "screen_toggle":
            if (dev && dev.screen_on) {
                wsSend({ action: "screen_off", serial });
            } else {
                wsSend({ action: "screen_on", serial });
            }
            break;
        case "screenshot":
            takeScreenshot(serial);
            break;
        case "rotate":
            wsSend({ action: "rotate", serial });
            break;
        case "stream_toggle":
            if (dev && dev.streaming) {
                wsSend({ action: "stop_stream", serial });
            } else {
                wsSend({ action: "start_stream", serial });
            }
            break;
        case "open_native_viewer":
            wsSend({ action: "open_native_viewer", serial });
            break;
        case "home":
            wsSend({ action: "keyevent", serial, keycode: 3 });
            break;
        case "back":
            wsSend({ action: "keyevent", serial, keycode: 4 });
            break;
        case "recent_apps":
            wsSend({ action: "keyevent", serial, keycode: 187 });
            break;
        case "quality": {
            const panel = cell.querySelector(".device-quality-panel");
            panel.style.display = panel.style.display === "none" ? "flex" : "none";
            break;
        }
    }
}

// Gestione parametri stream per singolo device (fullscreen/zoom).
function setDeviceStreamParams(serial, { maxSize, maxFps, bitRate } = {}) {
    if (!serial) return;
    const qs = new URLSearchParams();
    if (maxSize) qs.set("max_size", maxSize);
    if (maxFps) qs.set("max_fps", maxFps);
    if (bitRate) qs.set("bit_rate", bitRate);
    fetch(`/api/devices/${serial}/stream-quality?${qs.toString()}`, {
        method: "POST",
    }).catch(() => { });
}

function exitFullscreen() {
    // Tier focus off: il device torna al profilo griglia (restart lato
    // server, debounced). Solo se era davvero il device in fullscreen.
    if (state.fullscreenSerial) {
        fetch(`/api/devices/${state.fullscreenSerial}/stream-focus?on=0`, {
            method: "POST",
        }).catch(() => { });
    }
    document.querySelectorAll(".fullscreen-cell").forEach((c) => {
        if (c._fsDrag) {
            document.removeEventListener("pointermove", c._fsDrag.onMove);
            document.removeEventListener("pointerup", c._fsDrag.onUp);
            c._fsDrag.toolbar.classList.remove("dragging");
            c._fsDrag = null;
        }
        const feed = c.querySelector(".device-feed");
        if (feed) {
            feed.style.transform = "";
            feed.style.transformOrigin = "";
            feed.style.transition = "";
        }
        c.style.width = "";
        c.style.height = "";
        c.style.top = "";
        c.style.left = "";
        c.style.transform = "";
        c.classList.remove("fullscreen-cell");
        // Mentre eravamo in fullscreen, renderGrid puo' aver creato una
        // nuova cella per lo stesso serial (perche' questa era in body).
        // Se esiste gia', rimuoviamo questa vecchia invece di re-inserirla
        // (altrimenti il device compare due volte nella griglia).
        const existing = document.querySelector(
            `.device-cell[data-serial="${c.dataset.serial}"]:not(.fullscreen-cell)`
        );
        if (existing) {
            // La nuova cella e' gia' in griglia: ferma lo stream di questa
            // e scartala. L'AutoWatch della nuova cella e' gestito da renderGrid.
            const feed = c.querySelector(".device-feed");
            if (feed) stopStreamWs(feed);
            c.remove();
            // La card-shell svuotata (senza cella) va rimossa: il device
            // e' gia' rappresentato dalla cella esistente.
            if (c._fsParent && !c._fsParent.querySelector(".device-cell")) {
                c._fsParent.remove();
            }
        } else if (c._fsParent && document.contains(c._fsParent)) {
            // Ripristina la card-shell nascosta all'ingresso del fullscreen
            c._fsParent.classList.remove("fs-shell");
            c._fsParent.style.display = "";
            c._fsParent.insertBefore(c, c._fsNext && c._fsNext.parentNode === c._fsParent ? c._fsNext : null);
        } else {
            document.getElementById("deviceGrid")?.appendChild(c);
        }
        c._fsParent = null;
        c._fsNext = null;
        // Torna in griglia: l'AutoWatch deve rivalutare la visibilita'.
        // (solo se la cella e' ancora nel DOM: se era duplicata e' stata
        // rimossa sopra, e la nuova cella in griglia ha gia' il suo observer)
        if (document.contains(c)) awObserveCell(c);
    });
    document.getElementById("fullscreenBackdrop")?.remove();
    document.querySelectorAll(".fs-left-label").forEach((el) => el.remove());
    document.querySelectorAll(".fs-right-panel").forEach((el) => el.remove());
    state.fullscreenSerial = null;
    // Non rinegoziare la risoluzione automaticamente: evita riavvio stream.
    // Gli altri feed erano stati fermati all'ingresso per liberare il
    // decoder: azzero il cooldown di retry e forzo un renderGrid cosi' i
    // tile visibili ripartono subito (non al prossimo update di stato,
    // che puo' arrivare anche fra ~10s).
    document.querySelectorAll(".device-feed").forEach((f) => {
        if (!f.dataset.wsActive) f.dataset.wsRetryAt = "";
    });
    renderGrid();
}

function toggleFullscreen(serial, cell) {
    if (state.fullscreenSerial === serial) {
        exitFullscreen();
    } else {
        exitFullscreen();
        const dev = state.devices.find((d) => d.serial === serial);

        const backdrop = document.createElement("div");
        backdrop.id = "fullscreenBackdrop";
        backdrop.className = "fullscreen-backdrop";
        backdrop.addEventListener("click", exitFullscreen);
        document.body.appendChild(backdrop);

        cell._fsParent = cell.parentNode;
        cell._fsNext = cell.nextSibling;
        document.body.appendChild(cell);
        cell.classList.add("fullscreen-cell");
        state.fullscreenSerial = serial;
        // La card resterebbe in griglia come guscio vuoto (solo il
        // nome): la marchiamo e la nascondiamo — renderGrid non crea
        // un duplicato del device mentre e' in fullscreen.
        if (cell._fsParent) {
            cell._fsParent.classList.add("fs-shell");
            cell._fsParent.style.display = "none";
        }

        // Tier focus (modello Panda): il device in fullscreen riceve lo
        // stream a qualita' piena (focus_*), gli altri restano leggeri.
        // Il server riavvia solo questo stream — breve blackout, poi
        // latenza minima e risoluzione piena per lavorare sul device.
        fetch(`/api/devices/${serial}/stream-focus?on=1`, {
            method: "POST",
        }).catch(() => { });

        // In fullscreen libera il decoder degli altri device: fermiamo i
        // loro WebSocket. Il server continua a streammare, ma al ritorno
        // verranno riconnessi. Cosi' la GPU lavora solo per il fullscreen.
        document.querySelectorAll(".device-feed").forEach((feed) => {
            const other = feed.dataset.wsActive;
            if (other && other !== serial) {
                stopStreamWs(feed);
            }
        });

        // Etichetta verticale sinistra con il nome del device
        const leftLabel = document.createElement("div");
        leftLabel.className = "fs-left-label";
        leftLabel.textContent = dev?.display_name || serial;
        leftLabel.title = leftLabel.textContent;
        document.body.appendChild(leftLabel);
        cell._fsLeftLabel = leftLabel;

        // Pannello destro: zoom immediato + qualità/fps stream
        const rightPanel = document.createElement("div");
        rightPanel.className = "fs-right-panel";
        rightPanel.innerHTML = `
            <div class="fs-panel-title">Zoom</div>
            <input type="range" class="fs-zoom-slider" min="1" max="3" step="0.1" value="1">
            <div class="fs-zoom-value">100%</div>
            <div class="fs-panel-title">Qualità</div>
            <select class="fs-quality-select">
                <option value="480">480p</option>
                <option value="720">720p</option>
                <option value="1080">1080p</option>
            </select>
            <div class="fs-panel-title">FPS</div>
            <select class="fs-fps-select">
                <option value="2">2 fps</option>
                <option value="5">5 fps</option>
                <option value="10">10 fps</option>
                <option value="15">15 fps</option>
                <option value="30">30 fps</option>
            </select>
            <button class="fs-apply-btn">Applica stream</button>
            <button class="fs-close-btn">Chiudi</button>
        `;
        document.body.appendChild(rightPanel);
        cell._fsRightPanel = rightPanel;

        // Dimensioni adattive: riserva spazio per i pannelli laterali
        const feed = cell.querySelector(".device-feed");
        const feedW = feed && feed.width > 0 ? feed.width : 540;
        const feedH = feed && feed.height > 0 ? feed.height : 1080;
        const toolbarH = 32;
        const pad = 20;
        const leftPanelW = 180;
        const rightPanelW = 180;
        const availW = window.innerWidth - leftPanelW - rightPanelW - pad * 2;
        const availH = window.innerHeight - pad * 2;
        const scale = Math.min(
            availW / feedW,
            availH / (feedH + toolbarH)
        );
        const cellW = Math.max(320, Math.round(feedW * scale));
        const cellH = Math.max(320, Math.round((feedH + toolbarH) * scale));
        const top = Math.round((window.innerHeight - cellH) / 2);
        const left = leftPanelW + Math.round((window.innerWidth - leftPanelW - rightPanelW - cellW) / 2);
        cell.style.width = cellW + "px";
        cell.style.height = cellH + "px";
        cell.style.top = top + "px";
        cell.style.left = left + "px";

        // Zoom immediato con CSS transform (nessun riavvio stream)
        if (feed) {
            feed.style.transformOrigin = "bottom center";
            feed.style.transition = "transform 0.08s ease";
            feed.style.transform = "scale(1)";
        }

        const zoomSlider = rightPanel.querySelector(".fs-zoom-slider");
        const zoomValue = rightPanel.querySelector(".fs-zoom-value");
        zoomSlider.addEventListener("input", (e) => {
            const z = parseFloat(e.target.value);
            if (feed) feed.style.transform = `scale(${z})`;
            zoomValue.textContent = Math.round(z * 100) + "%";
        });

        const qualitySel = rightPanel.querySelector(".fs-quality-select");
        const fpsSel = rightPanel.querySelector(".fs-fps-select");
        rightPanel.querySelector(".fs-apply-btn").addEventListener("click", () => {
            setDeviceStreamParams(serial, {
                maxSize: parseInt(qualitySel.value, 10),
                maxFps: parseInt(fpsSel.value, 10),
            });
            toast("Qualità stream aggiornata", "success");
        });
        rightPanel.querySelector(".fs-close-btn").addEventListener("click", exitFullscreen);

        // Trascinamento dalla toolbar
        const toolbar = cell.querySelector(".device-toolbar");
        if (toolbar) {
            let startX = 0;
            let startY = 0;
            let startLeft = 0;
            let startTop = 0;
            let dragging = false;

            function onPointerDown(e) {
                if (e.target.closest(".toolbar-btn")) return;
                startX = e.clientX;
                startY = e.clientY;
                startLeft = cell.offsetLeft;
                startTop = cell.offsetTop;
                dragging = true;
                toolbar.classList.add("dragging");
                document.addEventListener("pointermove", onPointerMove);
                document.addEventListener("pointerup", onPointerUp, { once: true });
                e.preventDefault();
            }
            function onPointerMove(e) {
                if (!dragging) return;
                const dx = e.clientX - startX;
                const dy = e.clientY - startY;
                cell.style.left = (startLeft + dx) + "px";
                cell.style.top = (startTop + dy) + "px";
            }
            function onPointerUp() {
                if (!dragging) return;
                dragging = false;
                toolbar.classList.remove("dragging");
                document.removeEventListener("pointermove", onPointerMove);
                document.removeEventListener("pointerup", onPointerUp);
            }

            toolbar.addEventListener("pointerdown", onPointerDown);
            cell._fsDrag = {
                toolbar,
                onMove: onPointerMove,
                onUp: onPointerUp,
            };
        }
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

async function downloadBalancesCsv() {
    try {
        const resp = await fetch("/api/balances/csv");
        if (!resp.ok) throw new Error("CSV non disponibile");
        const blob = await resp.blob();
        // Nome file dal server: saldi_ledger_BET365_2026-09-07.csv
        const cd = resp.headers.get("Content-Disposition") || "";
        const m = cd.match(/filename="?([^";]+)"?/);
        const filename = m ? m[1] : `saldi_ledger_${Date.now()}.csv`;
        // Dialogo "Salva con nome": l'utente sceglie dove salvare il file.
        // Supportato da Chrome/Edge; altrove si ricade sul download classico.
        if (window.showSaveFilePicker) {
            try {
                const handle = await window.showSaveFilePicker({
                    suggestedName: filename,
                    types: [{
                        description: "CSV saldi",
                        accept: { "text/csv": [".csv"] },
                    }],
                });
                const writable = await handle.createWritable();
                await writable.write(blob);
                await writable.close();
                toast(`CSV saldi salvato: ${handle.name}`, "success");
                return;
            } catch (e) {
                if (e && e.name === "AbortError") return; // annullato dall'utente
                // Permesso negato o API non usabile: fallback sotto
            }
        }
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        toast("CSV saldi scaricato", "success");
    } catch (e) {
        toast("Errore download CSV: " + e.message, "error");
    }
}

// Escape per uscire dall'overlay ingrandito
document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.fullscreenSerial) {
        exitFullscreen();
    }
});

// =====================================================================
// Header
// =====================================================================

function updateHeader() {
    const online = state.devices.filter((d) => d.status === "online").length;
    const total = state.devices.length;
    const played = state.devices.filter((d) => d.played).length;
    const skipped = state.devices.filter((d) => d.skipped).length;
    const countEl = document.getElementById("deviceCount");
    if (countEl) {
        let text = `${online}/${total} dispositivi`;
        if (played > 0) text += ` (${played} giocati)`;
        if (skipped > 0) text += ` (${skipped} non giocati)`;
        countEl.textContent = text;
    }

    const btnBroadcast = document.getElementById("btnBroadcast");
    if (btnBroadcast) btnBroadcast.classList.toggle("active", state.broadcastMode);

    const btnResetPlayed = document.getElementById("btnResetPlayed");
    const badgeResetPlayed = document.getElementById("resetPlayedBadge");
    if (btnResetPlayed) {
        btnResetPlayed.disabled = played === 0;
        btnResetPlayed.style.opacity = played > 0 ? "1" : "0.6";
        if (badgeResetPlayed) {
            badgeResetPlayed.textContent = String(played);
            badgeResetPlayed.style.display = played > 0 ? "" : "none";
        }
    }

    const btnResetSkipped = document.getElementById("btnResetSkipped");
    const badgeResetSkipped = document.getElementById("resetSkippedBadge");
    if (btnResetSkipped) {
        btnResetSkipped.disabled = skipped === 0;
        btnResetSkipped.style.opacity = skipped > 0 ? "1" : "0.6";
        if (badgeResetSkipped) {
            badgeResetSkipped.textContent = String(skipped);
            badgeResetSkipped.style.display = skipped > 0 ? "" : "none";
        }
    }

    const btnShowAll = document.getElementById("btnShowAll");
    const badgeShowAll = document.getElementById("showAllBadge");
    if (btnShowAll) {
        const soloActive = state.soloSerials && state.soloSerials.size > 0;
        btnShowAll.style.display = soloActive ? "inline-flex" : "none";
        if (badgeShowAll && soloActive) {
            badgeShowAll.textContent = String(state.soloSerials.size);
        }
    }
}

// =====================================================================
// Right dock + flyouts
// =====================================================================

function initDock() {
    const rightDock = document.getElementById("rightDock");
    if (!rightDock) return;

    rightDock.querySelectorAll(".dock-item").forEach((item) => {
        const target = item.dataset.target;
        const flyout = target ? document.getElementById(target) : item.querySelector(".flyout");
        if (!flyout) return;

        // I click dentro il flyout non devono raggiungere il dock-item:
        // se un handler interno ri-renderizza e stacca il target dal DOM,
        // closest(".flyout") fallirebbe e il click verrebbe letto come toggle.
        flyout.addEventListener("click", (e) => e.stopPropagation());

        item.addEventListener("click", (e) => {
            if (!e.target.isConnected) return;
            if (e.target.closest(".flyout")) return;
            const wasOpen = flyout.classList.contains("active");
            closeAllFlyouts();
            if (!wasOpen) {
                flyout.classList.add("active");
                item.classList.add("active");
            }
            // Il dock-item ha tabindex=0 e resterebbe focalizzato: il suo
            // handler keydown riaprirebbe il flyout a ogni Spazio/Invio
            // mentre si scrive su un telefono. Togliamo il focus.
            item.blur();
        });

        item.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                item.click();
            }
        });
    });

    rightDock.querySelectorAll(".flyout-close").forEach((btn) => {
        btn.addEventListener("click", (e) => {
            e.stopPropagation();
            const flyout = btn.closest(".flyout");
            if (flyout) closeFlyout(flyout);
        });
    });

    document.addEventListener("click", (e) => {
        // Se il click ha rimosso il target dal DOM (es. re-render lista script),
        // closest() non risale più al dock: non e' un click "fuori".
        if (!e.target.isConnected) return;
        if (e.target.closest("#rightDock")) return;
        closeAllFlyouts();
    });

    function closeAllFlyouts() {
        rightDock.querySelectorAll(".flyout.active").forEach((flyout) => closeFlyout(flyout));
    }

    function closeFlyout(flyout) {
        flyout.classList.remove("active");
        const target = flyout.id;
        const item = rightDock.querySelector(`.dock-item[data-target="${target}"]`);
        if (item) item.classList.remove("active");
    }
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
        btnCopy.addEventListener("click", async () => {
            const body = document.getElementById("resultModalBody");
            if (body) {
                const ok = await copyToClipboard(body.textContent);
                toast(ok ? "Copiato" : "Errore copia", ok ? "success" : "error");
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

    const shellOutput = document.getElementById("shellOutput");
    try {
        const resp = await fetch(`/api/bulk/shell?command=${encodeURIComponent(cmd)}`, {
            method: "POST",
        });
        const data = await resp.json();
        let output = "";
        for (const [serial, result] of Object.entries(data)) {
            output += `[${serial}] ${result}\n`;
        }
        toast("Shell eseguita", "success");
        if (shellOutput) shellOutput.textContent = output || "(nessun output)";
    } catch (e) {
        toast("Errore shell: " + e.message, "error");
        if (shellOutput) shellOutput.textContent = "Errore: " + e.message;
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
        if (outputEl) outputEl.textContent = testo;
        if (outputWrap) outputWrap.style.display = "block";
    } catch (e) {
        toast(`Errore: ${e.message}`, "error");
    }
}

// =====================================================================
// Drag selection
// =====================================================================

function initDragSelect() {
    const grid = document.getElementById("deviceGrid");
    const container = document.getElementById("gridContainer");
    if (!grid || !container) return;

    let startX = 0, startY = 0, band = null;
    let pending = false, isDragging = false, additive = false;
    const THRESHOLD = 6; // px di movimento prima di mostrare il rettangolo

    function intersect(r1, r2) {
        return r1.left < r2.right && r1.right > r2.left && r1.top < r2.bottom && r1.bottom > r2.top;
    }

    function cancel() {
        pending = false;
        isDragging = false;
        if (band) { band.remove(); band = null; }
    }

    container.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        // Il drag parte da qualsiasi punto che non sia una card o un
        // elemento interattivo: prima partiva SOLO sui pixel di gap della
        // griglia (e.target === grid), quindi con la griglia piena di
        // telefoni era quasi impossibile farlo partire.
        if (e.target.closest(".device-card, button, input, textarea, a, select, [contenteditable]")) return;
        // Click sulla scrollbar verticale: lasciarlo allo scroll nativo
        const crect = container.getBoundingClientRect();
        if (e.clientX > crect.left + container.clientWidth) return;

        e.preventDefault();
        startX = e.clientX;
        startY = e.clientY;
        additive = e.ctrlKey || e.metaKey;
        pending = true;
    });

    document.addEventListener("mousemove", (e) => {
        if (!pending && !isDragging) return;
        const width = Math.abs(e.clientX - startX);
        const height = Math.abs(e.clientY - startY);
        if (pending) {
            // Sotto soglia e' ancora un click: non creare il rettangolo
            if (width < THRESHOLD && height < THRESHOLD) return;
            pending = false;
            isDragging = true;
            band = document.createElement("div");
            band.className = "rubberband";
            document.body.appendChild(band);
        }
        band.style.left = Math.min(startX, e.clientX) + "px";
        band.style.top = Math.min(startY, e.clientY) + "px";
        band.style.width = width + "px";
        band.style.height = height + "px";
    });

    document.addEventListener("mouseup", () => {
        if (pending) { pending = false; return; }
        if (!isDragging || !band) return;
        isDragging = false;
        const bandRect = band.getBoundingClientRect();
        band.remove();
        band = null;

        const hit = new Set();
        const visibleSerials = new Set();
        grid.querySelectorAll(".device-cell").forEach((cell) => {
            const serial = cell.dataset.serial;
            visibleSerials.add(serial);
            if (intersect(bandRect, cell.getBoundingClientRect())) {
                hit.add(serial);
            }
        });

        if (additive) {
            // Ctrl+drag: aggiunge alla selezione esistente
            hit.forEach((serial) => {
                const dev = state.devices.find((d) => d.serial === serial);
                if (dev && !dev.selected) {
                    wsSend({ action: "select", serial, selected: true });
                }
            });
        } else {
            // Drag normale: la selezione diventa quella del rettangolo,
            // ma solo tra i device visibili (quelli filtrati fuori non
            // vengono toccati).
            state.devices.forEach((dev) => {
                if (!visibleSerials.has(dev.serial)) return;
                const want = hit.has(dev.serial);
                if (!!dev.selected !== want) {
                    wsSend({ action: "select", serial: dev.serial, selected: want });
                }
            });
        }
    });

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && (pending || isDragging)) cancel();
    });
}

// =====================================================================
// Settings
// =====================================================================

async function initSettings() {
    const maxFps = document.getElementById("maxFps");
    const maxSize = document.getElementById("maxSize");
    const bitRate = document.getElementById("bitRate");
    const focusFps = document.getElementById("focusFps");
    const focusSize = document.getElementById("focusSize");
    const focusBitRate = document.getElementById("focusBitRate");

    const chkStartWithWindows = document.getElementById("chkStartWithWindows");
    const chkStartMinimized = document.getElementById("chkStartMinimized");
    const chkMinimizeToTray = document.getElementById("chkMinimizeToTray");
    const btnSaveStartup = document.getElementById("btnSaveStartup");

    try {
        const r = await fetch("/api/settings");
        const data = await r.json();
        if (maxFps) maxFps.value = data.stream?.max_fps ?? 15;
        if (maxSize) maxSize.value = data.stream?.max_size ?? 2400;
        if (bitRate) bitRate.value = Math.round((data.stream?.bit_rate ?? 4000000) / 1000);
        if (focusFps) focusFps.value = data.stream?.focus_max_fps ?? 0;
        if (focusSize) focusSize.value = data.stream?.focus_max_size ?? 0;
        if (focusBitRate) focusBitRate.value = Math.round((data.stream?.focus_bit_rate ?? 0) / 1000);
        if (chkStartWithWindows) chkStartWithWindows.checked = data.start_with_windows ?? false;
        if (chkStartMinimized) chkStartMinimized.checked = data.start_minimized ?? false;
        if (chkMinimizeToTray) chkMinimizeToTray.checked = data.minimize_to_tray ?? false;
    } catch (e) {}

    if (btnSaveStartup) {
        btnSaveStartup.addEventListener("click", async () => {
            try {
                await fetch("/api/settings", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        start_with_windows: chkStartWithWindows?.checked ?? false,
                        start_minimized: chkStartMinimized?.checked ?? false,
                        minimize_to_tray: chkMinimizeToTray?.checked ?? false,
                    }),
                });
                toast("Impostazioni avvio salvate", "success");
            } catch (e) {
                toast("Errore salvataggio avvio", "error");
            }
        });
    }

    async function saveStream() {
        try {
            await fetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    stream: {
                        max_fps: parseInt(maxFps?.value) || 15,
                        max_size: parseInt(maxSize?.value) || 2400,
                        bit_rate: (parseInt(bitRate?.value) || 4000) * 1000,
                        focus_max_fps: parseInt(focusFps?.value) || 0,
                        focus_max_size: parseInt(focusSize?.value) || 0,
                        focus_bit_rate: (parseInt(focusBitRate?.value) || 0) * 1000,
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
    if (bitRate) bitRate.addEventListener("change", saveStream);
    if (focusFps) focusFps.addEventListener("change", saveStream);
    if (focusSize) focusSize.addEventListener("change", saveStream);
    if (focusBitRate) focusBitRate.addEventListener("change", saveStream);

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

    // Modalità Slot: preset leggero per quando i telefoni renderizzano le
    // slot. Encoder piu' piccolo = meno CPU sul telefono, meno banda USB,
    // meno decode nel browser. I valori normali vengono salvati e
    // ripristinati allo spegnimento della modalita'.
    const SLOT_PRESET = { max_fps: 8, max_size: 360, bit_rate: 400000 };
    const btnSlotMode = document.getElementById("btnSlotMode");
    let slotMode = localStorage.getItem("griddroid_slot_mode") === "1";
    let savedQuality = null;

    const renderSlotMode = () => {
        if (!btnSlotMode) return;
        btnSlotMode.textContent = slotMode
            ? "🎰 Modalità Slot: ON"
            : "🎰 Modalità Slot: OFF";
        btnSlotMode.classList.toggle("btn-accent", slotMode);
    };
    renderSlotMode();

    if (btnSlotMode) {
        btnSlotMode.addEventListener("click", async () => {
            slotMode = !slotMode;
            localStorage.setItem("griddroid_slot_mode", slotMode ? "1" : "0");
            btnSlotMode.disabled = true;
            try {
                let stream;
                if (slotMode) {
                    // Salva la qualita' corrente per ripristinarla dopo
                    savedQuality = {
                        max_fps: parseInt(maxFps?.value) || 15,
                        max_size: parseInt(maxSize?.value) || 2400,
                        bit_rate: (parseInt(bitRate?.value) || 4000) * 1000,
                    };
                    stream = { ...SLOT_PRESET };
                } else {
                    stream = savedQuality || { max_fps: 15, max_size: 2400, bit_rate: 4000000 };
                }
                await fetch("/api/settings", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ stream }),
                });
                if (maxFps) maxFps.value = stream.max_fps;
                if (maxSize) maxSize.value = stream.max_size;
                if (bitRate && stream.bit_rate) bitRate.value = Math.round(stream.bit_rate / 1000);
                await fetch("/api/settings/apply-stream", { method: "POST" });
                renderSlotMode();
                toast(
                    slotMode
                        ? "Modalità Slot attiva: stream alleggeriti su tutti i device"
                        : "Modalità Slot disattivata: qualità ripristinata",
                    "success"
                );
            } catch (e) {
                slotMode = !slotMode;
                localStorage.setItem("griddroid_slot_mode", slotMode ? "1" : "0");
                toast("Errore cambio modalità", "error");
            } finally {
                btnSlotMode.disabled = false;
            }
        });
    }

    // Modalità Panda: replica la config osservata su Panda (touping):
    // 480p, pochi fps, bitrate minimo e soprattutto encoder SOFTWARE
    // OMX.google.h264.encoder che non crasha mai. Stream quasi statici
    // ma sempre vivi — ideale con tanti device su hub USB.
    const PANDA_PRESET = { max_fps: 2, max_size: 480, bit_rate: 50000, software_encoder: true };
    const btnPandaMode = document.getElementById("btnPandaMode");
    let pandaMode = localStorage.getItem("griddroid_panda_mode") === "1";
    let savedQualityPanda = null;

    const renderPandaMode = () => {
        if (!btnPandaMode) return;
        btnPandaMode.textContent = pandaMode
            ? "🐼 Modalità Panda: ON"
            : "🐼 Modalità Panda: OFF";
        btnPandaMode.classList.toggle("btn-accent", pandaMode);
    };
    renderPandaMode();

    if (btnPandaMode) {
        btnPandaMode.addEventListener("click", async () => {
            pandaMode = !pandaMode;
            localStorage.setItem("griddroid_panda_mode", pandaMode ? "1" : "0");
            btnPandaMode.disabled = true;
            try {
                let stream;
                if (pandaMode) {
                    savedQualityPanda = {
                        max_fps: parseInt(maxFps?.value) || 15,
                        max_size: parseInt(maxSize?.value) || 2400,
                        bit_rate: (parseInt(bitRate?.value) || 4000) * 1000,
                        software_encoder: false,
                    };
                    stream = { ...PANDA_PRESET };
                } else {
                    stream = savedQualityPanda || { max_fps: 15, max_size: 2400, bit_rate: 4000000, software_encoder: false };
                }
                await fetch("/api/settings", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ stream }),
                });
                if (maxFps) maxFps.value = stream.max_fps;
                if (maxSize) maxSize.value = stream.max_size;
                if (bitRate && stream.bit_rate) bitRate.value = Math.round(stream.bit_rate / 1000);
                await fetch("/api/settings/apply-stream", { method: "POST" });
                renderPandaMode();
                toast(
                    pandaMode
                        ? "Modalità Panda attiva: encoder software, stream ultra-leggeri"
                        : "Modalità Panda disattivata: qualità ripristinata",
                    "success"
                );
            } catch (e) {
                pandaMode = !pandaMode;
                localStorage.setItem("griddroid_panda_mode", pandaMode ? "1" : "0");
                toast("Errore cambio modalità", "error");
            } finally {
                btnPandaMode.disabled = false;
            }
        });
    }

    // Modalita' Remota: alleggerisce SOLO questo browser (il server manda
    // solo keyframe, ~1 ogni 2s). Pensata per quando la griglia e' aperta
    // da un altro PC in rete: la banda e' il collo di bottiglia, non il
    // telefono. Default ON automatico se l'host non e' localhost.
    const btnRemoteLite = document.getElementById("btnRemoteLite");

    const renderRemoteLite = () => {
        if (!btnRemoteLite) return;
        const on = remoteLiteMode();
        btnRemoteLite.textContent = on
            ? "🌐 Modalità Remota: ON"
            : "🌐 Modalità Remota: OFF";
        btnRemoteLite.classList.toggle("btn-accent", on);
    };
    renderRemoteLite();

    if (btnRemoteLite) {
        btnRemoteLite.addEventListener("click", () => {
            const on = !remoteLiteMode();
            localStorage.setItem("griddroid_remote_lite", on ? "1" : "0");
            renderRemoteLite();
            // Riavvia tutte le sessioni stream attive per applicare il flag
            restartAllFeeds();
            toast(
                on
                    ? "Modalità Remota attiva: solo keyframe, banda ridotta"
                    : "Modalità Remota disattivata: stream completo",
                "success"
            );
        });
    }

    // Modalita' video: H264 decodificato nel browser (WebCodecs) oppure
    // JPEG decodificato sul server con ffmpeg (per WebView2 e browser
    // senza decoder hw). Per-browser, salvato in localStorage.
    const videoModeSelect = document.getElementById("videoModeSelect");
    if (videoModeSelect) {
        videoModeSelect.value = state.videoMode;
        videoModeSelect.addEventListener("change", () => {
            const mode = ["h264", "mse", "jpeg"].includes(videoModeSelect.value)
                ? videoModeSelect.value : 'h264';
            localStorage.setItem("griddroid.videoMode", mode);
            state.videoMode = mode;
            // Le celle vanno ricreate con l'elemento giusto (canvas vs video).
            location.reload();
        });
    }

    // AutoWatch: stream solo per i telefoni visibili in griglia.
    const autoWatchToggle = document.getElementById("autoWatchToggle");
    if (autoWatchToggle) {
        autoWatchToggle.checked = state.autoWatch;
        autoWatchToggle.addEventListener("change", () => {
            state.autoWatch = autoWatchToggle.checked;
            localStorage.setItem("griddroid.autoWatch", state.autoWatch ? "1" : "0");
            if (!state.autoWatch) {
                // Disattivato: shouldWatch() e' sempre true, tutte le celle partono.
                renderGrid();
            } else {
                // Riattivato: le celle fuori vista vengono messe in pausa.
                document.querySelectorAll(".device-cell").forEach((cell) => {
                    const serial = cell.dataset.serial;
                    if (serial && !state.visibleSerials.has(serial) && state.fullscreenSerial !== serial) {
                        awSchedulePause(cell, serial);
                    }
                });
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

    const btnExportConfig = document.getElementById("btnExportConfig");
    if (btnExportConfig) {
        btnExportConfig.addEventListener("click", async () => {
            try {
                // Il server non vede il localStorage del browser: lo
                // mandiamo noi cosi' viaggiano anche i bookmaker custom e
                // tutte le preferenze UI (videoMode, ordinamento, filtri
                // giocati/non giocati, slot/panda mode, ecc.).
                const ls = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    if (k && k.startsWith("griddroid")) ls[k] = localStorage.getItem(k);
                }
                const r = await fetch("/api/settings/export", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ localStorage: ls }),
                });
                const data = await r.json().catch(() => ({}));
                if (!r.ok || !data.ok) throw new Error(data.error || "errore esportazione");
                // Il server salva in .griddroid/backups/ e apre Explorer
                // col file selezionato: il toast conferma il percorso.
                toast(`Configurazione esportata in ${data.path}`, "success", 8000);
            } catch (e) {
                toast("Errore esportazione configurazione", "error");
            }
        });
    }

    const btnImportConfig = document.getElementById("btnImportConfig");
    const importFileInput = document.getElementById("importConfigFile");
    if (btnImportConfig && importFileInput) {
        btnImportConfig.addEventListener("click", () => importFileInput.click());
        importFileInput.addEventListener("change", async () => {
            const file = importFileInput.files && importFileInput.files[0];
            importFileInput.value = "";
            if (!file) return;
            let payload;
            try {
                payload = JSON.parse(await file.text());
            } catch (e) {
                toast("File di configurazione non valido", "error");
                return;
            }
            if (payload.app !== "griddroid" || !payload.files) {
                toast("File non riconosciuto come export GridDroid", "error");
                return;
            }
            if (!confirm("Importare la configurazione? Sovrascrive etichette, tag, giocati, saldi, bookmaker e tutte le impostazioni.")) return;
            try {
                const r = await fetch("/api/settings/import", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ app: "griddroid", files: payload.files }),
                });
                const data = await r.json().catch(() => ({}));
                if (!r.ok || !data.ok) throw new Error(data.error || "errore importazione");
                // Ripristina il localStorage esportato (bookmaker custom
                // e preferenze UI) prima di ricaricare.
                if (payload.localStorage && typeof payload.localStorage === "object") {
                    for (const [k, v] of Object.entries(payload.localStorage)) {
                        if (typeof k === "string" && k.startsWith("griddroid") && typeof v === "string") {
                            try { localStorage.setItem(k, v); } catch (e) {}
                        }
                    }
                }
                toast(`Importati ${data.written ? data.written.length : 0} file — ricarico`, "success");
                setTimeout(() => location.reload(), 800);
            } catch (e) {
                toast(`Errore importazione: ${e.message || e}`, "error");
            }
        });
    }
}

// =====================================================================
// Server info + firewall
// =====================================================================

function initServerInfo() {
    const ticker = document.getElementById("newsTicker");
    const tickerText = document.getElementById("newsTickerText");
    const fwBtn = document.getElementById("btnOpenFirewall");
    const msgEl = document.getElementById("firewallMessage");
    if (!ticker || !tickerText) return;

    fetch("/api/server-info")
        .then((r) => (r.ok ? r.json() : null))
        .then((info) => {
            if (!info) return;
            let text = `GridDroid in esecuzione su <a href="${info.current_url}" target="_blank">${info.current_url}</a>`;
            const local = (info.local_urls || []).filter((u) => u !== info.current_url);
            if (info.host === "0.0.0.0" && local.length) {
                const urls = local.map((u) => `<a href="${u}" target="_blank">${u}</a>`).join(", ");
                text += ` — accesso LAN: ${urls}`;
            } else if (info.host === "127.0.0.1" || info.host === "localhost") {
                text += ` (solo locale; per la LAN avvia con --host 0.0.0.0)`;
            }
            tickerText.innerHTML = text;
            ticker.style.display = "inline-flex";

            if (fwBtn && info.host === "0.0.0.0") {
                fwBtn.style.display = "";
                fwBtn.addEventListener("click", () => {
                    fwBtn.disabled = true;
                    fetch("/api/open-firewall", { method: "POST" })
                        .then((r) => r.json())
                        .then((res) => {
                            toast(res.message || "Fatto", res.ok ? "success" : "error");
                        })
                        .catch(() => {
                            toast("Errore richiesta", "error");
                        })
                        .finally(() => {
                            fwBtn.disabled = false;
                        });
                });
            }
        })
        .catch(() => {});
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

    // Ripristina i dispositivi segnati come non giocati
    const btnResetSkipped = document.getElementById("btnResetSkipped");
    if (btnResetSkipped) {
        btnResetSkipped.addEventListener("click", () => {
            if (confirm("Ripristinare tutti i dispositivi non giocati?")) {
                wsSend({ action: "reset_skipped" });
            }
        });
    }

    // Rimuove il filtro "mostra solo questi" e mostra di nuovo tutti i device
    const btnShowAll = document.getElementById("btnShowAll");
    if (btnShowAll) {
        btnShowAll.addEventListener("click", () => {
            state.soloSerials = null;
            renderGrid();
            updateHeader();
        });
    }

    // Legge il saldo a schermo di ogni device online e lo salva in CSV
    const btnReadBalances = document.getElementById("btnReadBalances");
    if (btnReadBalances) {
        btnReadBalances.addEventListener("click", async () => {
            const serials = state.devices.filter((d) => d.selected).map((d) => d.serial);
            if (!serials.length) {
                toast("Seleziona prima i dispositivi di cui leggere il saldo", "error");
                return;
            }
            btnReadBalances.disabled = true;
            const oldText = btnReadBalances.textContent;
            btnReadBalances.textContent = "Lettura in corso…";
            toast(`Lettura saldi su ${serials.length} device…`, "info");
            // Barra progresso nella barra del log, aggiornata via polling
            const progWrap = document.getElementById("balanceProgress");
            const progBar = document.getElementById("balanceProgressBar");
            const progText = document.getElementById("balanceProgressText");
            const t0 = Date.now();
            if (progWrap) progWrap.style.display = "flex";
            if (progBar) progBar.style.width = "0%";
            if (progText) progText.textContent = `0/${serials.length}`;
            const progTimer = setInterval(async () => {
                try {
                    const r = await fetch("/api/balances/progress");
                    const p = await r.json();
                    const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
                    if (progBar) progBar.style.width = pct + "%";
                    if (progText) {
                        const el = Math.round((Date.now() - t0) / 1000);
                        const eta = p.done ? Math.round((el / p.done) * (p.total - p.done)) : null;
                        progText.textContent = `${p.done}/${p.total}` + (eta ? ` · ~${eta}s` : "");
                    }
                } catch {}
            }, 700);
            try {
                const res = await fetch("/api/balances/read", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ serials }),
                });
                // Un 500 del server risponde testo/HTML, non JSON:
                // res.json() esploderebbe con 'unexpected token'.
                const ct = res.headers.get("content-type") || "";
                if (!res.ok || !ct.includes("json")) {
                    const txt = (await res.text()).slice(0, 200);
                    throw new Error(`server ${res.status}: ${txt || "risposta non JSON"}`);
                }
                const data = await res.json();
                // Saldo sulla card: valore letto o N/D (app chiusa/background)
                (data.results || []).forEach((r) => {
                    const dev = state.devices.find((d) => d.serial === r.serial);
                    if (dev) dev.saldo = r.saldo || "N/D";
                    const cell = document.querySelector(`.device-cell[data-serial="${r.serial}"]`);
                    const el = cell?.parentElement?.querySelector(".device-saldo");
                    if (el) el.textContent = r.saldo ? `€ ${r.saldo}` : "N/D";
                });
                const found = (data.results || []).filter((r) => r.saldo);
                if (found.length) {
                    const lines = found.map((r) => {
                        const meta = [r.bookmaker, r.username].filter(Boolean).join("/");
                        return `${r.nome}: ${r.saldo}${meta ? ` (${meta})` : ""}`;
                    }).join(" — ");
                    if (data.save_error) {
                        toast(`${data.save_error} — saldi letti: ${lines}`, "warn");
                    } else {
                        // Niente auto-download: showSaveFilePicker richiede un
                        // gesto utente recente (~5s dal click) e la lettura dura
                        // troppo — scadrebbe e partirebbe il download automatico
                        // in Download. Toast persistente con bottone: il click
                        // e' un gesto vero e il "Salva con nome" si apre.
                        toast(`${data.saved} saldi salvati in CSV: ${lines}`, "success");
                        const t = document.createElement("div");
                        t.className = "toast success";
                        const b = document.createElement("button");
                        b.className = "btn btn-accent";
                        b.style.marginLeft = "8px";
                        b.textContent = "Scarica CSV";
                        b.onclick = () => { t.remove(); downloadBalancesCsv(); };
                        t.appendChild(document.createTextNode("CSV pronto."));
                        t.appendChild(b);
                        document.getElementById("toastContainer").appendChild(t);
                        setTimeout(() => t.remove(), 30000);
                    }
                } else {
                    toast(`Nessun saldo rilevato a schermo (${(data.results || []).length} device letti)`, "error");
                }
            } catch (e) {
                toast("Errore lettura saldi: " + e.message, "error");
            } finally {
                clearInterval(progTimer);
                if (progBar) progBar.style.width = "100%";
                setTimeout(() => { if (progWrap) progWrap.style.display = "none"; }, 1500);
                btnReadBalances.disabled = false;
                btnReadBalances.textContent = oldText;
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
        const resizeObserver = new ResizeObserver(() => {
            updateGridColumns();
            // NOTA: non chiamare scheduleAdaptiveQuality() qui:
            // il ridimensionamento/zoom deve scalare il CSS, non riavviare
            // lo stream di ogni dispositivo (saturation ADB con 25+ device).
        });
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

    // Ordinamento automatico A-Z attivo di default
    const btnSortInit = document.getElementById("btnSort");
    if (btnSortInit) {
        btnSortInit.classList.toggle("active", state.sortBy === "az");
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
// Clipboard helpers (funzionano anche su HTTP remoto)
// =====================================================================

async function copyToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
    }
    // Fallback con document.execCommand (funziona anche su HTTP non-locale)
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    try {
        const ok = document.execCommand("copy");
        document.body.removeChild(textarea);
        return ok;
    } catch (e) {
        document.body.removeChild(textarea);
        return false;
    }
}

async function readFromClipboard() {
    if (navigator.clipboard && window.isSecureContext) {
        try {
            const text = await navigator.clipboard.readText();
            return text;
        } catch (e) {
            // fall-through al prompt
        }
    }
    const text = window.prompt("Incolla qui il testo da inviare al dispositivo:");
    return text === null ? null : text;
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
    copyToClipboard(text).then((ok) => {
        toast(ok ? "Log copiato negli appunti" : "Errore nella copia del log", ok ? "success" : "error");
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

function toast(message, type = "info", duration = 4000) {
    const container = document.getElementById("toastContainer");
    const div = document.createElement("div");
    div.className = `toast ${type}`;
    div.textContent = message;
    container.appendChild(div);
    setTimeout(() => div.remove(), duration);
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
    // NON rinegoziare la risoluzione dello stream al zoom: basta lo scaling
    // CSS. Riavviare scrcpy per ogni cambio zoom suona la saturazione ADB.
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

// Lista bookmaker di default: condivisa fra la griglia flyout e la
// matrice Saldi (le righe della matrice seguono questa lista + i custom).
const BOOKMAKER_DEFAULTS = [
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

// Bookmaker custom caricati dal server (persistenza in bookmakers.json):
// il localStorage si perdeva' a ogni cambio porta dell'origine — il file
// sul disco invece sopravvive a riavvii, update e cambi porta.
let _customBookmakers = [];

async function loadCustomBookmakers() {
    try {
        const res = await fetch("/api/bookmakers");
        const data = await res.json();
        _customBookmakers = data.custom || [];
    } catch (e) {
        _customBookmakers = [];
    }
    // Migrazione una tantum: custom salvati in localStorage dalle vecchie
    // versioni vengono importati sul server, poi la chiave si svuota.
    try {
        const legacy = JSON.parse(localStorage.getItem("griddroid_bookmakers") || "[]");
        if (legacy.length) {
            const known = new Set(_customBookmakers.map(b => b.url));
            const add = legacy.filter(b => b && b.name && b.url && !known.has(b.url));
            if (add.length) {
                _customBookmakers = [..._customBookmakers, ...add];
                await saveCustomBookmakers(_customBookmakers);
            }
            localStorage.removeItem("griddroid_bookmakers");
        }
    } catch (e) { /* ignora */ }
    return _customBookmakers;
}

// Operazione incrementale sul server (add/del): applica la modifica alla
// lista su disco senza rispedire la lista completa — cosi' due tab aperte
// o una pagina con stato vecchio non cancellano i siti aggiunti altrove.
async function bookmakerOp(op, item) {
    for (let attempt = 0; attempt < 2; attempt++) {
        try {
            const res = await fetch("/api/bookmakers", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ [op]: item }),
            });
            if (res.ok) return true;
        } catch (e) { /* riprova sotto */ }
        await new Promise((r) => setTimeout(r, 1500));
    }
    return false;
}

async function saveCustomBookmakers(list) {
    _customBookmakers = list;
    // Un tentativo + un retry: prima gli errori di rete venivano ingoiati
    // in silenzio e il sito 'spariva' al riavvio senza alcun avviso.
    for (let attempt = 0; attempt < 2; attempt++) {
        try {
            const res = await fetch("/api/bookmakers", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ custom: list }),
            });
            if (res.ok) return;
        } catch (e) { /* riprova sotto */ }
        await new Promise((r) => setTimeout(r, 1500));
    }
    toast("Salvataggio siti non riuscito — riprova", "warn");
}

// Nomi dei bookmaker: default + custom dal server. Serve alla
// matrice Saldi per costruire le righe nella stessa lista della sezione.
function bookmakerNames() {
    return [...BOOKMAKER_DEFAULTS, ..._customBookmakers].map(b => b.name);
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

    const defaults = BOOKMAKER_DEFAULTS;

    // custom viene dal server (bookmakers.json): il localStorage si
    // perdeva al cambio porta dell'origine. Il fetch completa in async
    // e poi la griglia si ridisegna coi siti salvati.
    let custom = _customBookmakers.slice();
    let all = [...defaults, ...custom];
    function syncCustom() {
        _customBookmakers = [...custom];
        all = [...defaults, ...custom];
    }
    loadCustomBookmakers().then(() => {
        // MERGE, non sovrascrivere: un sito aggiunto mentre il GET era
        // ancora in volo verrebbe cancellato dalla risposta — e al
        // salvataggio successivo sparirebbe anche dal file sul server.
        // Era il motivo per cui i bookmaker custom 'sparivano'.
        const known = new Set(custom.map((b) => b.url));
        const extra = _customBookmakers.filter((b) => b && b.url && !known.has(b.url));
        if (extra.length) {
            custom = [...custom, ...extra];
            saveCustomBookmakers(custom);
        }
        all = [...defaults, ...custom];
        applySearch();
    });

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
            row.querySelector(".bookmaker-copy").addEventListener("click", async () => {
                const ok = await copyToClipboard(b.url);
                toast(ok ? "URL copiato" : "Errore copia URL", ok ? "success" : "error");
            });
            const del = row.querySelector(".bookmaker-delete");
            if (del) {
                del.addEventListener("click", async () => {
                    const ok = await bookmakerOp("del", { name: b.name, url: b.url });
                    if (!ok) {
                        toast("Rimozione non riuscita — riprova", "warn");
                        return;
                    }
                    custom = custom.filter((c) => !(c.name === b.name && c.url === b.url));
                    syncCustom();
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
        btnSave.addEventListener("click", async () => {
            const name = (inputName.value || "").trim();
            let url = (inputUrl.value || "").trim();
            if (!name || !url) {
                toast("Compila nome e URL", "warn");
                return;
            }
            if (!/^https?:\/\//i.test(url)) url = "https://" + url;
            const newB = { name, url };
            if (custom.some((c) => c.name === name && c.url === url)) {
                toast("Sito già presente", "warn");
                return;
            }
            const ok = await bookmakerOp("add", newB);
            if (!ok) {
                toast("Salvataggio siti non riuscito — riprova", "warn");
                return;
            }
            custom.push(newB);
            syncCustom();
            applySearch();
            inputName.value = "";
            inputUrl.value = "";
            if (addForm) addForm.style.display = "none";
            toast("Sito aggiunto", "success");
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

    // Se il gruppo eliminato era il filtro attivo, torna a "tutti"
    if (state.activeGroupFilter === name) {
        state.activeGroupFilter = null;
    }
    // Se la ricerca per gruppo puntava al gruppo eliminato, azzerala
    if (state.searchMode === "group" && state.searchText === name) {
        state.searchText = "";
        const searchInput = document.getElementById("deviceSearchInput");
        if (searchInput) searchInput.value = "";
    }

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

function toggleGroupFilter(name) {
    if (state.activeGroupFilter === name) {
        state.activeGroupFilter = null;
    } else {
        state.activeGroupFilter = name;
    }
    renderGroups();
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

    const allActive = !state.activeGroupFilter || state.activeGroupFilter === "__all__";
    const allCount = state.devices.length;

    let html = `
        <div class="group-row">
            <span class="group-name">Tutti i telefoni <span class="group-count">(${allCount})</span></span>
            <div class="group-actions">
                <span class="group-eye ${allActive ? "active" : ""}" data-group="__all__" title="Mostra tutti">👁</span>
                <button class="group-btn" data-action="select" data-group="__all__">Seleziona</button>
            </div>
        </div>
    `;

    html += groups
        .map(
            (g) => `
        <div class="group-row">
            <span class="group-name">${escapeHtml(g)} <span class="group-count">(${counts[g] || 0})</span></span>
            <div class="group-actions">
                <span class="group-eye ${state.activeGroupFilter === g ? "active" : ""}" data-group="${escapeHtml(g)}" title="Filtra">👁</span>
                <button class="group-btn" data-action="select" data-group="${escapeHtml(g)}">Seleziona</button>
                <button class="group-btn" data-action="filter-search" data-group="${escapeHtml(g)}">Cerca</button>
                ${stored.has(g) ? `<button class="group-btn group-btn-delete" data-action="delete" data-group="${escapeHtml(g)}">×</button>` : ""}
            </div>
        </div>
    `
        )
        .join("");

    list.innerHTML = html;

    list.querySelectorAll(".group-eye").forEach((eye) => {
        eye.addEventListener("click", () => toggleGroupFilter(eye.dataset.group));
    });
    list.querySelectorAll("button[data-action='select']").forEach((btn) => {
        btn.addEventListener("click", () => {
            if (btn.dataset.group === "__all__") selectAllDevicesUnfiltered();
            else selectGroup(btn.dataset.group);
        });
    });
    list.querySelectorAll("button[data-action='filter-search']").forEach((btn) => {
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

// ------------------------------------------------------------------
// Pagina saldi: tabella per conto, auto-aggiornamento dal backend
// ------------------------------------------------------------------
let _balancesCache = {};
let _balancesTimer = null;

async function fetchBalances() {
    try {
        const r = await fetch("/api/balances");
        const d = await r.json();
        _balancesCache = d.balances || {};
        renderBalances();
    } catch (e) { /* silenzioso: riprova al prossimo ciclo */ }
}

// Chiave normalizzata del bookmaker: "WILLIAM HILL", "williamhill" e
// "William Hill" devono finire nella stessa riga della matrice.
function _normBookKey(name) {
    return (name || "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

function _fmtEuro(v) {
    return "€ " + v.toLocaleString("it-IT", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    });
}

// Costruisce la mappa celle {riga-book -> {serial -> record}} condivisa
// tra il render della matrice e l'export CSV: stessa identica logica
// (books per-book, fallback top-level, riga ALTRO per saldi orfani).
function _balancesCells() {
    const listNames = bookmakerNames();
    const normToName = {};
    listNames.forEach(n => { normToName[_normBookKey(n)] = n; });

    const cells = {};
    const extraBooks = [];
    const put = (serial, book, rec) => {
        const norm = _normBookKey(book);
        if (!norm) return;
        let rowName = normToName[norm];
        if (!rowName) {
            rowName = book;
            if (!extraBooks.some(e => _normBookKey(e) === norm)) extraBooks.push(book);
        }
        (cells[rowName] ||= {})[serial] = rec;
    };
    for (const [serial, b] of Object.entries(_balancesCache)) {
        let placed = false;
        if (b.books) {
            for (const [book, rec] of Object.entries(b.books)) {
                if (rec.saldo) { put(serial, book, rec); placed = true; }
            }
        }
        if (!placed && b.saldo && b.bookmaker) {
            put(serial, b.bookmaker, b);
            placed = true;
        }
        // Saldo senza book riconosciuto (sito non mappato): finiva in un
        // buco — la cella non compariva da nessuna parte della matrice.
        // Riga dedicata "ALTRO" cosi' il valore resta comunque visibile.
        if (!placed && b.saldo) {
            put(serial, "ALTRO", b);
        }
    }
    const bookRows = [...listNames, ...extraBooks]
        .filter((n, i, a) => a.indexOf(n) === i)
        .sort((a, b) => a.localeCompare(b, "it"));
    return { cells, bookRows };
}

// Export CSV della matrice vista (bookmaker x telefono, con totali):
// separatore ';' e decimali con virgola — si apre diretto in Excel IT.
function downloadMatrixCsv() {
    const { cells, bookRows } = _balancesCells();
    const devices = state.devices.slice();
    const devName = d => d.display_name || d.serial;
    const num = v => {
        const f = parseFloat(v);
        return isNaN(f) ? "" : String(f).replace(".", ",");
    };
    const rows = [];
    rows.push(["BOOK", ...devices.map(devName), "TOTALE"]);
    for (const n of bookRows) {
        const row = cells[n] || {};
        let tot = 0, has = false;
        const cols = devices.map(d => {
            const r = row[d.serial];
            const v = r ? parseFloat(r.saldo) : NaN;
            if (!isNaN(v)) { tot += v; has = true; }
            return r && r.saldo ? num(r.saldo) : "";
        });
        rows.push([n, ...cols, has ? num(tot) : ""]);
    }
    let grand = 0, any = false;
    const foot = devices.map(d => {
        let tot = 0, has = false;
        bookRows.forEach(n => {
            const r = (cells[n] || {})[d.serial];
            const v = r ? parseFloat(r.saldo) : NaN;
            if (!isNaN(v)) { tot += v; has = true; }
        });
        if (has) { grand += tot; any = true; }
        return has ? num(tot) : "";
    });
    rows.push(["TOTALE", ...foot, any ? num(grand) : ""]);
    const esc = s => `"${String(s).replace(/"/g, '""')}"`;
    const csv = "﻿" + rows.map(r => r.map(esc).join(";")).join("\r\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `saldi_matrice_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

function openSaldi() {
    const ov = document.getElementById("saldiOverlay");
    if (!ov) return;
    ov.hidden = false;
    fetchBalances();
}

function closeSaldi() {
    const ov = document.getElementById("saldiOverlay");
    if (ov) ov.hidden = true;
}

// Vista corrente della pagina saldi: 'cards' (una scheda per telefono) o
// 'matrix' (foglio bookmaker x telefono). Persistita in localStorage.
let _saldiView = localStorage.getItem("saldiView") || "cards";

function _saldiAge(rec, now) {
    let stale = false, ageTxt = "";
    if (rec && rec.timestamp) {
        const t = new Date(String(rec.timestamp).replace(" ", "T")).getTime();
        if (!isNaN(t)) {
            const mins = Math.floor((now - t) / 60000);
            stale = mins > 10;
            ageTxt = mins < 1 ? "ora" : mins < 60 ? `${mins}m fa` : `${Math.floor(mins / 60)}h fa`;
        }
    }
    return { stale, ageTxt };
}

function _saldiDelta(rec) {
    if (typeof rec?.diff === "number" && rec.diff !== 0) {
        const up = rec.diff > 0;
        return `<span class="saldi-delta ${up ? "up" : "down"}">` +
            `${up ? "▲" : "▼"} ${_fmtEuro(Math.abs(rec.diff))}</span>`;
    }
    return "";
}

// serial -> [{book, rec}] — i conti letti su quel device, in ordine di
// valore decrescente (i saldi a zero restano in coda).
function _deviceBooks() {
    const out = {};
    for (const [serial, b] of Object.entries(_balancesCache)) {
        const rows = [];
        if (b.books) {
            for (const [book, rec] of Object.entries(b.books)) {
                if (rec.saldo) rows.push({ book, rec });
            }
        }
        if (!rows.length && b.saldo) {
            rows.push({ book: b.bookmaker || "ALTRO", rec: b });
        }
        rows.sort((x, y) =>
            (parseFloat(y.rec.saldo) || 0) - (parseFloat(x.rec.saldo) || 0));
        out[serial] = rows;
    }
    return out;
}

// Vista schede: una card per telefono coi suoi conti — e' il modello
// mentale giusto (persona -> i suoi account), non un foglio Excel con
// centinaia di celle vuote.
function _renderSaldiCards(wrap) {
    const q = (document.getElementById("balanceSearch")?.value || "").trim().toLowerCase();
    const onlyFilled = document.getElementById("saldiOnlyWithBalance")?.checked;
    const now = Date.now();
    const devBooks = _deviceBooks();
    const devices = state.devices.slice();
    const devName = d => d.display_name || d.serial;

    // Ordina le card per totale decrescente: in cima chi ha piu' fondi.
    const totals = {};
    for (const d of devices) {
        totals[d.serial] = (devBooks[d.serial] || []).reduce(
            (acc, r) => acc + (parseFloat(r.rec.saldo) || 0), 0);
    }
    devices.sort((a, b) => (totals[b.serial] || 0) - (totals[a.serial] || 0));

    const cards = [];
    for (const d of devices) {
        const rows = devBooks[d.serial] || [];
        // Ricerca: matcha il nome/seriale del device -> card intera;
        // matcha un book -> card con solo le righe di quel book.
        const devMatch = !q || devName(d).toLowerCase().includes(q)
            || d.serial.toLowerCase().includes(q);
        const bookMatch = q && rows.some(r => r.book.toLowerCase().includes(q));
        if (q && !devMatch && !bookMatch) continue;
        const visRows = (q && bookMatch && !devMatch)
            ? rows.filter(r => r.book.toLowerCase().includes(q))
            : rows;
        if (onlyFilled && !visRows.length) continue;

        let tot = 0, newest = null, allStale = rows.length > 0;
        for (const r of rows) {
            const v = parseFloat(r.rec.saldo);
            if (!isNaN(v)) tot += v;
            const { stale } = _saldiAge(r.rec, now);
            if (!stale) allStale = false;
            const t = r.rec.timestamp;
            if (t && (!newest || t > newest)) newest = t;
        }
        const age = newest ? _saldiAge({ timestamp: newest }, now) : null;

        const rowsHtml = visRows.length ? visRows.map(r => {
            const v = parseFloat(r.rec.saldo);
            const { stale, ageTxt } = _saldiAge(r.rec, now);
            const tip = [r.rec.username, r.rec.timestamp].filter(Boolean).join(" · ");
            const val = isNaN(v) ? escapeHtml(r.rec.saldo) : _fmtEuro(v);
            const zero = !isNaN(v) && v === 0;
            return `<div class="saldi-card-row${stale ? " stale" : ""}${zero ? " zero" : ""}" ` +
                `title="${escapeHtml(tip)}">` +
                `<span class="saldi-card-book">${escapeHtml(r.book)}</span>` +
                `<span class="saldi-card-val">${val}` +
                `${_saldiDelta(r.rec)}` +
                (ageTxt ? `<span class="saldi-age">${ageTxt}</span>` : "") +
                `</span></div>`;
        }).join("") : '<div class="saldi-card-empty">Nessun saldo letto</div>';

        cards.push(`<div class="saldi-card${allStale ? " stale" : ""}">` +
            `<div class="saldi-card-head">` +
            `<span class="saldi-card-name" title="${escapeHtml(d.serial)}">${escapeHtml(devName(d))}</span>` +
            `<span class="saldi-card-total">${rows.length ? _fmtEuro(tot) : "—"}</span>` +
            `</div>` +
            `<div class="saldi-card-rows">${rowsHtml}</div>` +
            `<div class="saldi-card-foot">` +
            `<span>${rows.length} conti</span>` +
            (age?.ageTxt ? `<span class="${age.stale ? "saldi-age-warn" : ""}">agg. ${age.ageTxt}</span>` : "") +
            `</div></div>`);
    }

    wrap.innerHTML = cards.length
        ? cards.join("")
        : '<div class="balances-empty">Nessuna scheda da mostrare con questi filtri.</div>';
}

function _renderSaldiMatrix(wrap) {
    const q = (document.getElementById("balanceSearch")?.value || "").trim().toLowerCase();
    const onlyFilled = document.getElementById("saldiOnlyWithBalance")?.checked;

    // --- Celle: nome-riga -> serial -> {saldo, username, timestamp} ---
    const { cells, bookRows } = _balancesCells();
    const devices = state.devices.slice();
    const devName = d => d.display_name || d.serial;

    // Ricerca: filtra i due assi in modo indipendente — se il testo
    // matcha un book restringe le righe, se matcha un telefono le
    // colonne; se non matcha nessuno dei due, l'asse resta intero.
    const bookHit = q && bookRows.some(n => n.toLowerCase().includes(q));
    const devHit = q && devices.some(d =>
        devName(d).toLowerCase().includes(q) || d.serial.toLowerCase().includes(q));
    let visBooks = bookHit ? bookRows.filter(n => n.toLowerCase().includes(q)) : bookRows;
    let visDevs = devHit ? devices.filter(d =>
        devName(d).toLowerCase().includes(q) || d.serial.toLowerCase().includes(q)) : devices;

    if (onlyFilled) {
        visBooks = visBooks.filter(n => visDevs.some(d => (cells[n] || {})[d.serial]));
        visDevs = visDevs.filter(d => visBooks.some(n => (cells[n] || {})[d.serial]));
    }

    const now = Date.now();
    const cellHtml = rec => {
        if (!rec || !rec.saldo) return '<td class="saldi-cell empty"></td>';
        const v = parseFloat(rec.saldo);
        const { stale, ageTxt } = _saldiAge(rec, now);
        const tip = [rec.username, rec.timestamp].filter(Boolean).join(" · ");
        const val = isNaN(v) ? escapeHtml(rec.saldo) : _fmtEuro(v);
        const user = rec.username
            ? `<span class="saldi-user">${escapeHtml(rec.username)}</span>` : "";
        const delta = _saldiDelta(rec);
        const age = ageTxt ? `<span class="saldi-age">${ageTxt}</span>` : "";
        return `<td class="saldi-cell${stale ? " stale" : ""}" title="${escapeHtml(tip)}">` +
            `<span class="saldi-val">${val}</span>${user}${delta}${age}</td>`;
    };

    const headCells = visDevs.map(d =>
        `<th class="saldi-col" title="${escapeHtml(d.serial)}">${escapeHtml(devName(d))}</th>`).join("");
    const bodyRows = visBooks.map(n => {
        const row = cells[n] || {};
        const tds = visDevs.map(d => cellHtml(row[d.serial])).join("");
        let tot = 0, has = false;
        visDevs.forEach(d => {
            const r = row[d.serial];
            const v = r ? parseFloat(r.saldo) : NaN;
            if (!isNaN(v)) { tot += v; has = true; }
        });
        return `<tr><th class="saldi-row">${escapeHtml(n)}</th>${tds}` +
            `<td class="saldi-cell tot">${has ? _fmtEuro(tot) : ""}</td></tr>`;
    }).join("");

    // Riga TOTALE: somma per colonna + totale generale
    let grand = 0, any = false;
    const footCells = visDevs.map(d => {
        let tot = 0, has = false;
        visBooks.forEach(n => {
            const r = (cells[n] || {})[d.serial];
            const v = r ? parseFloat(r.saldo) : NaN;
            if (!isNaN(v)) { tot += v; has = true; }
        });
        if (has) { grand += tot; any = true; }
        return `<td class="saldi-cell tot">${has ? _fmtEuro(tot) : ""}</td>`;
    }).join("");

    if (!visBooks.length || !visDevs.length) {
        wrap.innerHTML = '<div class="balances-empty">Nessuna cella da mostrare con questi filtri.</div>';
    } else {
        wrap.innerHTML = `<table class="saldi-table">
            <thead><tr>
                <th class="saldi-corner">BOOK \\ TELEFONO</th>${headCells}
                <th class="saldi-col tot">TOTALE</th>
            </tr></thead>
            <tbody>${bodyRows}</tbody>
            <tfoot><tr>
                <th class="saldi-row">TOTALE</th>${footCells}
                <td class="saldi-cell tot grand">${any ? _fmtEuro(grand) : ""}</td>
            </tr></tfoot>
        </table>`;
    }
    return grand;
}

function renderBalances() {
    const cards = document.getElementById("saldiCards");
    const matrix = document.getElementById("balancesTable");
    const summary = document.getElementById("balancesSummary");
    if (!cards || !matrix) return;
    // Overlay chiuso: non ricostruire mille celle a ogni refresh.
    const ov = document.getElementById("saldiOverlay");
    if (ov && ov.hidden) return;

    const isCards = _saldiView === "cards";
    cards.hidden = !isCards;
    matrix.hidden = isCards;
    let grand = 0;
    if (isCards) {
        _renderSaldiCards(cards);
        // Totale generale per il footer (stessa sorgente della matrice).
        for (const rows of Object.values(_deviceBooks())) {
            for (const r of rows) {
                const v = parseFloat(r.rec.saldo);
                if (!isNaN(v)) grand += v;
            }
        }
    } else {
        grand = _renderSaldiMatrix(matrix) || 0;
    }

    if (summary) {
        const { cells } = _balancesCells();
        const n = Object.values(cells).reduce((acc, r) => acc + Object.keys(r).length, 0);
        const devCount = new Set(
            Object.values(cells).flatMap(r => Object.keys(r))
        ).size;
        const latest = Object.values(_balancesCache)
            .map(b => b.timestamp).filter(Boolean).sort().pop();
        const parts = [
            `${n} celle con saldo`,
            `${devCount} telefoni`,
            `${Object.keys(cells).length} book`,
        ];
        if (grand) parts.push(`Totale: ${_fmtEuro(grand)}`);
        if (latest) parts.push(`Ultima lettura: ${latest.slice(11, 16)}`);
        summary.textContent = parts.join(" · ");
    }
}

function initBalances() {
    const table = document.getElementById("balancesTable");
    if (!table) return;
    const dock = document.getElementById("dockSaldi");
    const btnClose = document.getElementById("btnCloseSaldi");
    const btnCsv = document.getElementById("btnBalancesCsv");
    const search = document.getElementById("balanceSearch");
    const onlyFilled = document.getElementById("saldiOnlyWithBalance");
    const btnRefresh = document.getElementById("btnRefreshBalances");
    const btnTop = document.getElementById("btnSaldi");
    if (btnTop) btnTop.addEventListener("click", openSaldi);
    if (dock) {
        dock.addEventListener("click", openSaldi);
        dock.addEventListener("keydown", e => {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openSaldi(); }
        });
    }
    const btnMatrixCsv = document.getElementById("btnMatrixCsv");
    if (btnMatrixCsv) btnMatrixCsv.addEventListener("click", downloadMatrixCsv);
    // Toggle vista schede/matrice — la scelta resta salvata.
    document.querySelectorAll(".saldi-view-btn").forEach(btn => {
        if (btn.dataset.view === _saldiView) {
            btn.classList.add("active");
        } else {
            btn.classList.remove("active");
        }
        btn.addEventListener("click", () => {
            _saldiView = btn.dataset.view;
            localStorage.setItem("saldiView", _saldiView);
            document.querySelectorAll(".saldi-view-btn").forEach(b =>
                b.classList.toggle("active", b === btn));
            renderBalances();
        });
    });
    if (btnClose) btnClose.addEventListener("click", closeSaldi);
    if (btnCsv) btnCsv.addEventListener("click", downloadBalancesCsv);
    if (search) search.addEventListener("input", renderBalances);
    if (onlyFilled) onlyFilled.addEventListener("change", renderBalances);
    if (btnRefresh) btnRefresh.addEventListener("click", fetchBalances);
    document.addEventListener("keydown", e => { if (e.key === "Escape") closeSaldi(); });
    // Auto-refresh: ogni 10s la matrice si aggiorna coi saldi letti in
    // background dal backend (lettura CDP periodica, ~30s per device).
    fetchBalances();
    _balancesTimer = setInterval(fetchBalances, 10000);
}

async function fetchLedgerNicknames() {
    try {
        const res = await fetch("/api/ledger/nicknames");
        const data = await res.json();
        const sel = document.getElementById("ledgerNickname");
        if (!sel) return;
        sel.innerHTML = '<option value="">Seleziona utente...</option>';
        (data.nicknames || []).forEach(n => {
            const opt = document.createElement("option");
            opt.value = n;
            opt.textContent = n;
            sel.appendChild(opt);
        });
    } catch (e) { /* silenzioso */ }
}

function logLedgerSync(text) {
    const el = document.getElementById("ledgerSyncLog");
    if (!el) return;
    const line = document.createElement("div");
    line.textContent = `${new Date().toLocaleTimeString()} ${text}`;
    el.appendChild(line);
    while (el.children.length > 20) el.removeChild(el.firstChild);
    el.scrollTop = el.scrollHeight;
}

async function syncLedgerUser() {
    const sel = document.getElementById("ledgerNickname");
    const nickname = sel?.value;
    if (!nickname) {
        logLedgerSync("Seleziona un utente");
        return;
    }
    logLedgerSync(`Sincronizzo ${nickname}...`);
    try {
        const res = await fetch("/api/ledger/sync", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ nickname }),
        });
        const data = await res.json();
        if (data.ok) {
            logLedgerSync(`OK ${nickname} ${data.bookmaker} ${data.saldo} → Ledger`);
            fetchBalances();
        } else {
            logLedgerSync(`ERRORE ${nickname}: ${data.error}`);
        }
    } catch (e) {
        logLedgerSync(`ERRORE rete: ${e.message}`);
    }
}

async function syncLedgerAll() {
    logLedgerSync("Sincronizzo tutti...");
    try {
        const res = await fetch("/api/ledger/sync", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ all: true }),
        });
        const data = await res.json();
        logLedgerSync(data.message || "completato");
        (data.results || []).forEach(r => {
            const msg = r.ok
                ? `OK ${r.nickname} ${r.bookmaker} ${r.saldo}`
                : `ERRORE ${r.nickname}: ${r.error}`;
            logLedgerSync(msg);
        });
        fetchBalances();
    } catch (e) {
        logLedgerSync(`ERRORE rete: ${e.message}`);
    }
}

function initLedgerSync() {
    const sel = document.getElementById("ledgerNickname");
    const btnUser = document.getElementById("btnSyncLedgerUser");
    const btnAll = document.getElementById("btnSyncLedgerAll");
    if (sel) fetchLedgerNicknames();
    if (btnUser) btnUser.addEventListener("click", syncLedgerUser);
    if (btnAll) btnAll.addEventListener("click", syncLedgerAll);
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
        btnAll.addEventListener("click", selectAllDevices);
    }
    if (btnNone) {
        btnNone.addEventListener("click", deselectAllDevices);
    }
}

function initAccordion() {
    document.querySelectorAll(".flyout .sidebar-section").forEach((sec) => {
        const h4 = sec.querySelector("h4");
        if (!h4) return;
        h4.style.cursor = "pointer";
        h4.tabIndex = 0;
        h4.setAttribute("role", "button");
        h4.addEventListener("click", () => {
            const wasActive = sec.classList.contains("active");
            const flyout = sec.closest(".flyout");
            const siblings = flyout ? flyout.querySelectorAll(".sidebar-section") : [];
            siblings.forEach((s) => s.classList.remove("active"));
            if (!wasActive) sec.classList.add("active");
        });
        h4.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                h4.click();
            }
        });
    });

    document.querySelectorAll(".flyout").forEach((flyout) => {
        const sections = flyout.querySelectorAll(".sidebar-section");
        if (sections.length && !flyout.querySelector(".sidebar-section.active")) {
            sections[0].classList.add("active");
        }
    });
}

// =====================================================================
// Visualizzazione
// =====================================================================

function initViewControls() {
    const sortBy = document.getElementById("deviceSortBy");
    const chkShowPlayed = document.getElementById("chkShowPlayed");
    const chkShowSkipped = document.getElementById("chkShowSkipped");
    const btnSort = document.getElementById("btnSort");

    if (sortBy) {
        sortBy.value = state.sortBy;
        sortBy.addEventListener("change", () => {
            state.sortBy = sortBy.value;
            localStorage.setItem("griddroid_sort_by", state.sortBy);
            renderGrid();
        });
    }

    if (chkShowPlayed) {
        chkShowPlayed.checked = state.showPlayed;
        chkShowPlayed.addEventListener("change", () => {
            state.showPlayed = chkShowPlayed.checked;
            localStorage.setItem("griddroid_show_played", state.showPlayed ? "1" : "0");
            renderGrid();
        });
    }

    if (chkShowSkipped) {
        chkShowSkipped.checked = state.showSkipped;
        chkShowSkipped.addEventListener("change", () => {
            state.showSkipped = chkShowSkipped.checked;
            localStorage.setItem("griddroid_show_skipped", state.showSkipped ? "1" : "0");
            renderGrid();
        });
    }

    if (btnSort) {
        btnSort.classList.toggle("active", state.sortBy === "az");
        btnSort.addEventListener("click", () => {
            state.sortBy = state.sortBy === "az" ? "default" : "az";
            localStorage.setItem("griddroid_sort_by", state.sortBy);
            if (sortBy) sortBy.value = state.sortBy;
            btnSort.classList.toggle("active", state.sortBy === "az");
            renderGrid();
        });
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
// Command Palette
// =====================================================================

const COMMAND_PALETTE_KEY = "griddroid_palette_history";
const PALETTE_TOKEN_LABELS = {
    pin: "PIN (lascia vuoto per solo wake):",
    url: "URL da aprire:",
    testo: "Testo da inserire:",
};
const PALETTE_PREDEFINED = [
    { name: "Sblocca schermo", command: "input keyevent 82 && input text {pin} && input keyevent 66", desc: "chiede il PIN" },
    { name: "Home", command: "input keyevent 3", desc: "tasto home" },
    { name: "Indietro", command: "input keyevent 4", desc: "tasto back" },
    { name: "App recenti", command: "input keyevent 187", desc: "multitasking" },
    { name: "Spegni schermo", command: "input keyevent 26", desc: "tasto power" },
    { name: "Riavvia", command: "reboot", desc: "riavvio dispositivo" },
    { name: "Apri URL", command: "am start -a android.intent.action.VIEW -d {url}", desc: "sito o bookmaker" },
    { name: "Apri Bet365", command: "am start -a android.intent.action.VIEW -d https://www.bet365.com", desc: "browser" },
    { name: "Apri PokerStars", command: "am start -a android.intent.action.VIEW -d https://www.pokerstars.it", desc: "browser" },
    { name: "Apri PayPal", command: "am start -a android.intent.action.VIEW -d https://www.paypal.com", desc: "browser" },
];

async function resolvePaletteCommand(command) {
    const tokenRegex = /\{([a-zA-Z0-9_]+)\}/g;
    const tokens = [...command.matchAll(tokenRegex)].map((m) => m[1]);
    if (!tokens.length) return command;

    const values = {};
    for (const t of tokens) {
        const label = PALETTE_TOKEN_LABELS[t] || `Valore per ${t}:`;
        const val = window.prompt(label);
        if (val === null) return null;
        values[t] = val.trim();
    }

    // Sblocco: PIN vuoto = solo wake
    if (tokens.includes("pin") && values.pin === "") {
        return "input keyevent 82";
    }

    let final = command;
    for (const [t, v] of Object.entries(values)) {
        final = final.replace(new RegExp(`\\{${t}\\}`, "g"), v);
    }
    return final;
}

let commandPaletteEl = null;
let commandPaletteInput = null;
let commandPaletteList = null;
let paletteActiveIndex = -1;
let paletteItems = [];

function loadPaletteHistory() {
    try {
        const raw = localStorage.getItem(COMMAND_PALETTE_KEY);
        if (!raw) return [];
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) return [];
        return parsed
            .map((c) => {
                if (typeof c === "string") return { name: c, command: c };
                const cmd = c.command || c.name || "";
                return { name: c.name || cmd, command: cmd };
            })
            .filter((c) => c.command);
    } catch (e) {
        return [];
    }
}

function savePaletteHistory(name, command) {
    if (!command) return;
    const history = loadPaletteHistory().filter((c) => c.command !== command);
    history.unshift({ name: name || command, command });
    if (history.length > 15) history.pop();
    try {
        localStorage.setItem(COMMAND_PALETTE_KEY, JSON.stringify(history));
    } catch (e) {}
}

function selectAllDevices() {
    // Ctrl+A seleziona i device in vista: con un filtro attivo (gruppo,
    // ricerca, solo-questi) tocca solo quelli, non tutto il farm.
    const visible = getVisibleDevices();
    if (!visible.length) return;
    const wanted = new Set(visible.map((d) => d.serial));
    const filtered = wanted.size !== state.devices.length;
    state.devices.forEach((d) => { d.selected = wanted.has(d.serial); });
    // Con filtro attivo invio la lista esplicita: il 'select_all' nudo
    // marcherebbe selezionati anche i device nascosti dal filtro.
    wsSend(filtered
        ? { action: "select_all", selected: true, serials: [...wanted] }
        : { action: "select_all", selected: true });
    renderGrid();
    renderPhoneSelection();
    toast(
        filtered
            ? `${wanted.size} dispositivi selezionati (vista filtrata)`
            : "Tutti i dispositivi selezionati",
        "success"
    );
}

// Seleziona davvero tutti i device, ignorando i filtri di vista:
// serve al pulsante "Tutti i telefoni" nel pannello Gruppi.
function selectAllDevicesUnfiltered() {
    if (!state.devices.length) return;
    state.devices.forEach((d) => { d.selected = true; });
    wsSend({ action: "select_all", selected: true });
    renderGrid();
    renderPhoneSelection();
    toast("Tutti i dispositivi selezionati", "success");
}

function deselectAllDevices() {
    // Simmetrico a Ctrl+A: con filtro attivo azzera solo i device in
    // vista; senza filtro azzera tutto come prima.
    const visible = getVisibleDevices();
    if (!visible.length) return;
    const wanted = new Set(visible.map((d) => d.serial));
    const filtered = wanted.size !== state.devices.length;
    if (filtered) {
        state.devices.forEach((d) => { if (wanted.has(d.serial)) d.selected = false; });
        wsSend({ action: "select_all", selected: false, serials: [...wanted] });
    } else {
        state.devices.forEach((d) => { d.selected = false; });
        wsSend({ action: "select_all", selected: false });
    }
    renderGrid();
    renderPhoneSelection();
    toast("Selezione azzerata", "success");
}

async function runPaletteCommand(command, name = command) {
    const cmdToRun = await resolvePaletteCommand(command);
    if (!cmdToRun) return;
    savePaletteHistory(name, command);
    const targets = state.devices.filter((d) => d.selected);
    if (!targets.length) {
        toast("Nessun dispositivo selezionato", "warn");
        return;
    }
    const shellOutput = document.getElementById("shellOutput");
    try {
        const resp = await fetch(`/api/bulk/shell?command=${encodeURIComponent(cmdToRun)}`, { method: "POST" });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        let output = "";
        for (const [serial, result] of Object.entries(data)) {
            output += `[${serial}] ${result}\n`;
        }
        toast(`Comando eseguito su ${targets.length} dispositivi`, "success");
        if (shellOutput) shellOutput.textContent = output || "(nessun output)";
    } catch (e) {
        toast("Errore comando: " + e.message, "error");
        if (shellOutput) shellOutput.textContent = "Errore: " + e.message;
    }
}

function renderPalette() {
    if (!commandPaletteList || !commandPaletteInput) return;
    const q = commandPaletteInput.value.trim().toLowerCase();
    const history = loadPaletteHistory().map((c) => ({ name: c.name, command: c.command, desc: "cronologia", history: true }));
    const seen = new Set();
    paletteItems = [];
    for (const c of [...history, ...PALETTE_PREDEFINED]) {
        if (seen.has(c.command)) continue;
        seen.add(c.command);
        if (!q || c.name.toLowerCase().includes(q) || c.command.toLowerCase().includes(q)) {
            paletteItems.push(c);
        }
    }

    commandPaletteList.innerHTML = "";
    if (!paletteItems.length) {
        commandPaletteList.innerHTML = `<div class="command-palette-empty">Nessun comando. Premi Invio per eseguire "${escapeHtml(commandPaletteInput.value.trim())}"</div>`;
        return;
    }
    paletteItems.forEach((c, i) => {
        const div = document.createElement("div");
        div.className = "command-palette-row" + (i === paletteActiveIndex ? " active" : "");
        div.dataset.command = c.command;
        div.dataset.name = c.name;
        div.innerHTML = `<span>${escapeHtml(c.name)}</span><span class="cmd-desc">${escapeHtml(c.desc)}</span>`;
        div.addEventListener("click", () => {
            runPaletteCommand(c.command, c.name);
            closeCommandPalette();
        });
        commandPaletteList.appendChild(div);
    });
}

function openCommandPalette() {
    if (!commandPaletteEl || !commandPaletteInput) return;
    commandPaletteEl.style.display = "flex";
    commandPaletteEl.classList.add("active");
    commandPaletteInput.value = "";
    paletteActiveIndex = -1;
    renderPalette();
    commandPaletteInput.focus();
}

function closeCommandPalette() {
    if (!commandPaletteEl) return;
    commandPaletteEl.classList.remove("active");
    commandPaletteEl.style.display = "none";
    paletteActiveIndex = -1;
}

function initCommandPalette() {
    commandPaletteEl = document.getElementById("commandPalette");
    commandPaletteInput = document.getElementById("commandPaletteInput");
    commandPaletteList = document.getElementById("commandPaletteList");
    if (!commandPaletteEl || !commandPaletteInput || !commandPaletteList) return;

    commandPaletteEl.querySelector(".command-palette-backdrop").addEventListener("click", closeCommandPalette);

    commandPaletteInput.addEventListener("input", () => {
        paletteActiveIndex = -1;
        renderPalette();
    });

    commandPaletteInput.addEventListener("keydown", (e) => {
        const rows = commandPaletteList.querySelectorAll(".command-palette-row");
        if (e.key === "ArrowDown") {
            e.preventDefault();
            paletteActiveIndex = (paletteActiveIndex + 1) % rows.length;
            renderPalette();
            rows[paletteActiveIndex]?.scrollIntoView({ block: "nearest" });
            return;
        }
        if (e.key === "ArrowUp") {
            e.preventDefault();
            paletteActiveIndex = (paletteActiveIndex - 1 + rows.length) % rows.length;
            renderPalette();
            rows[paletteActiveIndex]?.scrollIntoView({ block: "nearest" });
            return;
        }
        if (e.key === "Enter") {
            e.preventDefault();
            if (paletteActiveIndex >= 0 && paletteItems[paletteActiveIndex]) {
                const item = paletteItems[paletteActiveIndex];
                runPaletteCommand(item.command, item.name);
            } else if (commandPaletteInput.value.trim()) {
                const value = commandPaletteInput.value.trim();
                runPaletteCommand(value, value);
            }
            closeCommandPalette();
            return;
        }
        if (e.key === "Escape") {
            e.preventDefault();
            closeCommandPalette();
        }
    });
}

// =====================================================================
// Context Menu Init
// =====================================================================

// Mini-conferma posizionata vicino al punto del click (sostituisce
// window.confirm che appare sempre al centro dello schermo)
function confirmAt(x, y, message, onConfirm) {
    document.getElementById("inlineConfirm")?.remove();
    const box = document.createElement("div");
    box.id = "inlineConfirm";
    box.className = "inline-confirm";
    box.innerHTML = `<div>${message}</div>
        <div class="inline-confirm-btns">
            <button class="btn btn-accent" data-yes>Sì</button>
            <button class="btn" data-no>No</button>
        </div>`;
    document.body.appendChild(box);
    const rect = box.getBoundingClientRect();
    box.style.left = Math.max(8, Math.min(x, window.innerWidth - rect.width - 8)) + "px";
    box.style.top = Math.max(8, Math.min(y, window.innerHeight - rect.height - 8)) + "px";
    box.querySelector("[data-yes]").addEventListener("click", () => { box.remove(); onConfirm(); });
    box.querySelector("[data-no]").addEventListener("click", () => box.remove());
    setTimeout(() => {
        document.addEventListener("click", function h(ev) {
            if (!box.contains(ev.target)) { box.remove(); document.removeEventListener("click", h); }
        });
    }, 0);
}

function initContextMenu() {
    const menu = document.getElementById("deviceContextMenu");
    if (!menu) return;

    menu.addEventListener("click", (e) => {
        const item = e.target.closest('[data-action="set-played"]');
        if (item) {
            const serial = menu.dataset.serial;
            if (!serial) return;
            const targets = getContextTargetSerials(serial);
            const msg = `Segnare ${targets.length === 1 ? "il dispositivo" : targets.length + " dispositivi"} come giocati?`;
            confirmAt(e.clientX, e.clientY, msg, () => {
                targets.forEach((s) => wsSend({ action: "set_played", serial: s, played: true }));
            });
            hideDeviceContextMenu();
            return;
        }
        const skippedItem = e.target.closest('[data-action="set-skipped"]');
        if (skippedItem) {
            const serial = menu.dataset.serial;
            if (!serial) return;
            const targets = getContextTargetSerials(serial);
            const msg = `Segnare ${targets.length === 1 ? "il dispositivo" : targets.length + " dispositivi"} come non giocati?`;
            confirmAt(e.clientX, e.clientY, msg, () => {
                targets.forEach((s) => wsSend({ action: "set_skipped", serial: s, skipped: true }));
            });
            hideDeviceContextMenu();
            return;
        }
        const fsItem = e.target.closest('[data-action="fullscreen"]');
        if (fsItem) {
            const serial = menu.dataset.serial;
            const cell = serial && document.querySelector(`.device-cell[data-serial="${serial}"]`);
            if (cell) toggleFullscreen(serial, cell);
            hideDeviceContextMenu();
            return;
        }
        const acItem = e.target.closest('[data-action="autoclick"]');
        if (acItem) {
            const serial = menu.dataset.serial;
            const dev = serial && state.devices.find((d) => d.serial === serial);
            if (dev) {
                if (dev.autoclick) {
                    wsSend({ action: "autoclick_stop", serial });
                } else {
                    const tap = state.lastTap[serial];
                    if (!tap) {
                        toast("Tocca prima un punto sul telefono: l'auto-clicker cliccherà lì", "error");
                    } else {
                        const interval = parseInt(prompt("Intervallo tra click (ms)?", "1000"), 10) || 1000;
                        const count = parseInt(prompt("Numero di click (0 = infinito)?", "0"), 10) || 0;
                        wsSend({ action: "autoclick_start", serial, x: tap.x, y: tap.y, interval_ms: Math.max(150, interval), count });
                        toast(`Auto-click avviato su ${dev.display_name}`, "success");
                    }
                }
            }
            hideDeviceContextMenu();
            return;
        }
        const soloItem = e.target.closest('[data-action="solo"]');
        if (soloItem) {
            const serial = menu.dataset.serial;
            if (!serial) return;
            const targets = getContextTargetSerials(serial);
            state.soloSerials = new Set(targets);
            renderGrid();
            updateHeader();
            toast(`Mostro solo ${targets.length === 1 ? "1 dispositivo" : targets.length + " dispositivi"}`, "info");
            hideDeviceContextMenu();
        }
        const rmItem = e.target.closest('[data-action="remove-device"]');
        if (rmItem) {
            const serial = menu.dataset.serial;
            if (!serial) return;
            const targets = getContextTargetSerials(serial);
            const msg = targets.length === 1
                ? "Eliminare il dispositivo? Verranno rimossi nome, gruppi, stato giocato e saldo."
                : `Eliminare ${targets.length} dispositivi? Verranno rimossi nome, gruppi, stato giocato e saldi.`;
            confirmAt(e.clientX, e.clientY, msg, () => {
                targets.forEach((s) => wsSend({ action: "remove_device", serial: s }));
            });
            hideDeviceContextMenu();
            return;
        }
    });

    document.addEventListener("click", (e) => {
        if (!e.target.closest("#deviceContextMenu")) hideDeviceContextMenu();
    });

    document.addEventListener("contextmenu", (e) => {
        if (!e.target.closest(".device-card") && !e.target.closest("#deviceContextMenu")) {
            hideDeviceContextMenu();
        }
    });
}

// =====================================================================
// Init
// =====================================================================

document.addEventListener("DOMContentLoaded", () => {
    // Guard MSE: isTypeSupported dice SI anche quando addSourceBuffer
    // poi lancia NotSupportedError (budget decoder esaurito o piattaforma
    // senza pipeline MSE reale). Il test onesto e' creare un MediaSource
    // vero e provare addSourceBuffer: se fallisce passiamo a Nativa.
    const startApp = () => {
        connectWebSocket();
        setInterval(pollDevices, 2000);
        initDock();
        initAccordion();
        initBulkActions();
        initScriptPanel();
        initHeaderButtons();
        initSearch();
        initViewControls();
        initDragSelect();
        initLogPanel();
        initZoomControls();
        initMacro();
        initBookmakers();
        initBalances();
        initLedgerSync();
        initSettings();
        initGroups();
        initSelection();
        initResultModal();
        initServerInfo();
        initCommandPalette();
        initContextMenu();
        initAutoWatch();
        // Carica la versione dell'app
        fetch("/api/version").then(r => r.json()).then(d => {
            const el = document.getElementById("versionBadge");
            if (el && d.version) el.textContent = `v${d.version}`;
        }).catch(() => {});

        // Esito ultimo aggiornamento: toast di conferma o errore al riavvio
        fetch("/api/update/result").then(r => r.json()).then(d => {
            if (!d.pending) return;
            if (d.success) {
                toast(`Aggiornamento riuscito: GridDroid v${d.current}`, "success");
            } else {
                toast(`Aggiornamento a v${d.expected || "?"} NON riuscito — versione attuale v${d.current}`, "error");
            }
        }).catch(() => {});
    };

    if (state.videoMode === 'mse' && typeof MediaSource !== 'undefined') {
        try {
            const testMs = new MediaSource();
            const testVideo = document.createElement('video');
            const bail = setTimeout(() => {
                // sourceopen mai arrivato: MediaSource creato ma rotto.
                state.videoMode = 'jpeg';
                location.reload();
            }, 3000);
            testMs.addEventListener('sourceopen', () => {
                let ok = false;
                try {
                    testMs.addSourceBuffer('video/mp4; codecs="avc1.42E01E"');
                    ok = true;
                } catch (e) { ok = false; }
                clearTimeout(bail);
                if (!ok) {
                    toast('MSE non funzionante su questo browser: passo a Nativa', 'warn');
                    state.videoMode = 'jpeg';
                    location.reload();
                    return;
                }
                startApp();
            }, { once: true });
            // sourceopen scatta solo con il MediaSource attaccato a un video.
            testVideo.src = URL.createObjectURL(testMs);
            return;
        } catch (e) {
            // MediaSource non creabile: Nativa diretta.
            state.videoMode = 'jpeg';
            location.reload();
            return;
        }
    } else if (state.videoMode === 'mse') {
        // MediaSource proprio assente.
        state.videoMode = 'jpeg';
        location.reload();
        return;
    }
    startApp();
});
