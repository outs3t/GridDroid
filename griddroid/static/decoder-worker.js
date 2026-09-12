let decoder = null;
let pendingFrames = 0;

function findStartCode(b, start) {
    const n = b.length;
    let i = start;
    while (i + 2 < n) {
        if (b[i] === 0 && b[i + 1] === 0) {
            if (b[i + 2] === 1) return i;
            if (b[i + 2] === 0 && i + 3 < n && b[i + 3] === 1) return i;
        }
        i += 1;
    }
    return -1;
}

function parseSpsPpsFromAnnexB(data) {
    let sps = null;
    let pps = null;
    let i = 0;
    while (i + 4 < data.length) {
        const start = findStartCode(data, i);
        if (start === -1) break;
        i = start;
        const header = data[i + 2] === 1 ? 3 : 4;
        const j = i + header;
        let end = findStartCode(data, j);
        if (end === -1) end = data.length;
        while (end > j && data[end - 1] === 0) end--;
        const nal = data.slice(j, end);
        const type = nal[0] & 0x1f;
        if (type === 7 && !sps) sps = nal;
        else if (type === 8 && !pps) pps = nal;
        if (sps && pps) break;
        i = end;
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
    buf[4] = 0xff;
    buf[5] = 0xe1;
    buf[6] = (sps.length >> 8) & 0xff;
    buf[7] = sps.length & 0xff;
    buf.set(sps, 8);
    const o = 8 + sps.length;
    buf[o] = 1;
    buf[o + 1] = (pps.length >> 8) & 0xff;
    buf[o + 2] = pps.length & 0xff;
    buf.set(pps, o + 3);
    return buf;
}

function annexBToAVCC(data) {
    const nalStarts = [];
    for (let i = 0; i + 3 < data.length; i++) {
        if (data[i] === 0 && data[i + 1] === 0) {
            if (data[i + 2] === 1) {
                nalStarts.push(i);
                i += 2;
            } else if (data[i + 2] === 0 && data[i + 3] === 1) {
                nalStarts.push(i);
                i += 3;
            }
        }
    }
    if (nalStarts.length === 0) return null;
    let total = 0;
    for (let idx = 0; idx < nalStarts.length; idx++) {
        const start = nalStarts[idx];
        const header = data[start + 2] === 1 ? 3 : 4;
        const nextStart = idx + 1 < nalStarts.length ? nalStarts[idx + 1] : data.length;
        let end = nextStart;
        while (end > start + header && data[end - 1] === 0) end--;
        const len = end - start - header;
        total += 4 + len;
    }
    const result = new Uint8Array(total);
    let offset = 0;
    for (let idx = 0; idx < nalStarts.length; idx++) {
        const start = nalStarts[idx];
        const header = data[start + 2] === 1 ? 3 : 4;
        const nextStart = idx + 1 < nalStarts.length ? nalStarts[idx + 1] : data.length;
        let end = nextStart;
        while (end > start + header && data[end - 1] === 0) end--;
        const len = end - start - header;
        result[offset] = (len >> 24) & 0xff;
        result[offset + 1] = (len >> 16) & 0xff;
        result[offset + 2] = (len >> 8) & 0xff;
        result[offset + 3] = len & 0xff;
        result.set(data.subarray(start + header, end), offset + 4);
        offset += 4 + len;
    }
    return result;
}

self.onmessage = (event) => {
    const { type, payload } = event.data;
    if (type === 'init') {
        if (decoder) {
            try { decoder.close(); } catch (e) {}
        }
        decoder = null;
        pendingFrames = 0;
        return;
    }
    if (type === 'decode') {
        handleDecode(payload);
    }
};

async function handleDecode(payload) {
    const { data, isKey } = payload;
    if (!decoder) {
        if (!isKey) return;
        const spspps = parseSpsPpsFromAnnexB(data);
        if (!spspps.sps || !spspps.pps) return;
        const desc = buildAvcDescription(spspps.sps, spspps.pps);
        if (!desc) return;
        const profile = spspps.sps[1].toString(16).padStart(2, '0');
        const constraints = spspps.sps[2].toString(16).padStart(2, '0');
        const level = spspps.sps[3].toString(16).padStart(2, '0');
        const codec = `avc1.${profile}${constraints}${level}`;
        decoder = new VideoDecoder({
            output: (frame) => {
                try {
                    if (typeof createImageBitmap !== 'undefined') {
                        createImageBitmap(frame).then((bm) => {
                            self.postMessage({
                                type: 'frame',
                                bitmap: bm,
                                codedWidth: frame.codedWidth,
                                codedHeight: frame.codedHeight,
                                displayWidth: frame.displayWidth,
                                displayHeight: frame.displayHeight,
                            }, [bm]);
                        }).catch((err) => {
                            console.error('[Decoder] createImageBitmap:', err);
                            // Fallback: transfer VideoFrame stesso
                            try {
                                self.postMessage({
                                    type: 'frame',
                                    frame: frame,
                                    codedWidth: frame.codedWidth,
                                    codedHeight: frame.codedHeight,
                                }, [frame]);
                            } catch (e) {
                                frame.close();
                            }
                        }).finally(() => {
                            try { frame.close(); } catch (e) {}
                        });
                    } else {
                        self.postMessage({
                            type: 'frame',
                            frame: frame,
                            codedWidth: frame.codedWidth,
                            codedHeight: frame.codedHeight,
                        }, [frame]);
                    }
                } catch (err) {
                    console.error('[Decoder] output error:', err);
                    try { frame.close(); } catch (e) {}
                }
                pendingFrames--;
            },
            error: (err) => {
                console.error('[Decoder] VideoDecoder error:', err);
                self.postMessage({ type: 'error', message: String(err) });
            }
        });
        try {
            await decoder.configure({
                codec: codec,
                description: desc,
                hardwareAcceleration: 'prefer-hardware',
            });
            self.postMessage({ type: 'ready' });
        } catch (err) {
            console.error('[Decoder] configure error:', err);
            self.postMessage({ type: 'error', message: String(err) });
        }
        return;
    }
    if (!decoder) return;

    const avcc = annexBToAVCC(data);
    if (!avcc) return;

    // Latenza zero: se il decoder e' indietro, scartiamo i delta.
    if (pendingFrames > 2 && !isKey) return;
    if (pendingFrames > 3 && isKey) {
        try { await decoder.flush(); } catch (e) {}
        if (pendingFrames > 4) {
            try { decoder.close(); } catch (e) {}
            decoder = null;
            pendingFrames = 0;
            return;
        }
    }

    pendingFrames++;
    const chunk = new EncodedVideoChunk({
        type: isKey ? 'key' : 'delta',
        timestamp: performance.now() * 1000,
        duration: 0,
        data: avcc,
    });
    try {
        await decoder.decode(chunk);
    } catch (err) {
        console.error('[Decoder] decode error:', err);
        pendingFrames--;
    }
}
