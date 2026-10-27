// inkboard page. Two boards:
// - Sketch keeps a stroke model, draws new segments as they arrive and redraws the whole page
//   only on a WebSocket snapshot, resize, clear or a display-scale change. Its image export
//   (Copy image / Save PNG) draws the same model again on an off-screen canvas.
// - Screen shows the tablet's own screen: the server says "screen ready", the page fetches the
//   frame and draws it; its export is that frame, with the menu painted white unless Copy menu
//   is ticked.
// Naming: "board" is Sketch / Screen, "view" is Vertical / Horizontal, and `screen` below is the
// Sketch drawing target; the tablet's screen image is always called a "frame".
"use strict";

(() => {
    const PORTRAIT_ASPECT = 3 / 4;      // sheet width / height in the Vertical view (tablet in portrait)
    const PEN_WIDTH = 0.0035;           // full-pressure line width as a fraction of the sheet's short side
    const MIN_FACTOR = 0.25;            // width at zero pressure, as a fraction of full width
    // Image export, described in docs/architecture.md "Image export": the rM2 screen size in pixels.
    const EXPORT_SHORT = 1404;
    const EXPORT_LONG = 1872;
    const NOTICE_MS = 2500;
    const RECONNECT_MS = [500, 1000, 2000, 3000];

    const canvas = document.getElementById("board");
    const sheet = document.getElementById("sheet");
    const stage = sheet.parentElement;
    const ctx = canvas.getContext("2d");
    const chip = document.getElementById("status");
    const chipGlyph = chip.querySelector(".chip-glyph");
    const chipText = chip.querySelector(".chip-text");
    const count = document.getElementById("count");
    const clearBtn = document.getElementById("clear");
    const confirmBox = document.getElementById("confirm");
    const rotateBtn = document.getElementById("rotate");
    const copyBtn = document.getElementById("copy");
    const saveBtn = document.getElementById("save");
    const notice = document.getElementById("notice");
    const noticeGlyph = notice.querySelector(".notice-glyph");
    const noticeText = notice.querySelector(".notice-text");
    const sketchBtn = document.getElementById("board-sketch");
    const screenBtn = document.getElementById("board-screen");
    const refreshBtn = document.getElementById("refresh");
    const copyMenuBox = document.getElementById("copy-menu");
    const frameCanvas = document.getElementById("frame-board");
    const frameCtx = frameCanvas.getContext("2d");
    const frameState = document.getElementById("frame-state");
    const frameGlyph = frameState.querySelector(".frame-glyph");
    const frameText = frameState.querySelector(".frame-text");
    // The tablet's frame, described in docs/architecture.md "Screen export": portrait, RGBA, from the server.
    const FRAME_W = 1404;
    const FRAME_H = 1872;
    const FRAME_URL = "/api/v1/screen/frame";

    const strokes = new Map();          // id -> { tool, pts: [[u, v, p], ...] } in portrait page units
    let socket = null;
    let attempt = 0;
    let ink = "#f0f6fc";
    let exportInk = "#111111";
    let exportPaper = "#ffffff";
    // A drawing target: { ctx, w, h, ink, view } with w and h in the target's own units.
    // The screen target is rebuilt by fit(); the image export makes its own.
    let screen = null;

    // ---- orientation -------------------------------------------------------------------
    // Strokes always arrive in portrait page units. The Horizontal view is for the tablet in
    // landscape mode: the sheet is shown landscape and strokes are turned to stay upright.
    // The tablet in reMarkable's own landscape mode is the portrait page turned a quarter turn
    // clockwise. Measured on a reMarkable 2: counter-clockwise drew strokes upside down.
    const HORIZONTAL_CLOCKWISE = true;
    const STORAGE_KEY = "inkboard.view";
    let view = loadView();

    function loadView() {
        try {
            return window.localStorage.getItem(STORAGE_KEY) === "horizontal" ? "horizontal" : "vertical";
        } catch {
            return "vertical";
        }
    }

    function saveView() {
        try {
            window.localStorage.setItem(STORAGE_KEY, view);
        } catch {
            // private window or blocked storage: the choice just is not remembered
        }
    }

    // Board (Sketch / Screen) and the Copy menu choice are remembered the same way.
    const BOARD_KEY = "inkboard.board";
    const COPY_MENU_KEY = "inkboard.copyMenu";
    let board = loadSetting(BOARD_KEY) === "screen" ? "screen" : "sketch";
    let copyMenu = loadSetting(COPY_MENU_KEY) === "1";  // unticked unless chosen: exports leave the menu out

    function loadSetting(key) {
        try {
            return window.localStorage.getItem(key);
        } catch {
            return null;
        }
    }

    function saveSetting(key, value) {
        try {
            window.localStorage.setItem(key, value);
        } catch {
            // as saveView
        }
    }

    function place(t, pt) {
        const [u, v] = pt;
        if (t.view === "vertical") return [u * t.w, v * t.h];
        const [x, y] = HORIZONTAL_CLOCKWISE ? [1 - v, u] : [v, 1 - u];
        return [x * t.w, y * t.h];
    }

    function showView() {
        const next = view === "vertical" ? "horizontal" : "vertical";
        rotateBtn.textContent = view === "vertical" ? "Vertical" : "Horizontal";
        rotateBtn.setAttribute("aria-label", `View: ${view}. Switch to ${next}`);
        rotateBtn.title = `Switch to ${next} (tablet in ${next === "vertical" ? "portrait" : "landscape"} mode)`;
        sheet.dataset.view = view;
    }
    // (fit() redraws both boards after a view change, so the frame turns with the strokes.)

    rotateBtn.addEventListener("click", () => {
        view = view === "vertical" ? "horizontal" : "vertical";
        saveView();
        showView();
        fit();
        // Turning the view means the tablet turned, which no pen stroke reports: in the Screen
        // view the last frame still shows the old mode, so ask for a new one.
        if (board === "screen") sendScreen("refresh");
    });

    // ---- canvas sizing ---------------------------------------------------------------

    function fit() {
        const aspect = view === "vertical" ? PORTRAIT_ASPECT : 1 / PORTRAIT_ASPECT;
        const box = stage.getBoundingClientRect();
        const pad = 2;
        let height = Math.max(100, box.height - 2 * 16 - pad);
        let width = height * aspect;
        if (width > box.width - 2 * 16 - pad) {
            width = Math.max(75, box.width - 2 * 16 - pad);
            height = width / aspect;
        }
        const cssWidth = Math.floor(width);
        const cssHeight = Math.floor(height);
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.round(cssWidth * dpr);
        canvas.height = Math.round(cssHeight * dpr);
        canvas.style.width = cssWidth + "px";      // CSSOM, allowed under style-src 'self'
        canvas.style.height = cssHeight + "px";
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        screen = { ctx, w: cssWidth, h: cssHeight, ink, view };
        redraw();
        fitFrame(cssWidth, cssHeight, dpr);
    }

    function watchScale() {
        const query = window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
        query.addEventListener("change", () => { fit(); watchScale(); }, { once: true });
    }

    // ---- drawing ---------------------------------------------------------------------

    function widthFor(t, p) {
        const shortSide = Math.min(t.w, t.h);
        return Math.max(0.6, shortSide * PEN_WIDTH * (MIN_FACTOR + (1 - MIN_FACTOR) * p));
    }

    function dot(t, pt) {
        const [x, y] = place(t, pt);
        t.ctx.beginPath();
        t.ctx.fillStyle = t.ink;
        t.ctx.arc(x, y, widthFor(t, pt[2]) / 2, 0, Math.PI * 2);
        t.ctx.fill();
    }

    function segment(t, a, b) {
        const [ax, ay] = place(t, a);
        const [bx, by] = place(t, b);
        t.ctx.beginPath();
        t.ctx.strokeStyle = t.ink;
        t.ctx.lineCap = "round";
        t.ctx.lineJoin = "round";
        t.ctx.lineWidth = widthFor(t, (a[2] + b[2]) / 2);
        t.ctx.moveTo(ax, ay);
        t.ctx.lineTo(bx, by);
        t.ctx.stroke();
    }

    function drawFrom(t, stroke, start) {
        if (stroke.tool !== "pen") return;   // eraser strokes are kept, not drawn (see ROADMAP, eraser drawing)
        const pts = stroke.pts;
        for (let i = Math.max(start, 0); i < pts.length; i++) {
            if (i === 0) dot(t, pts[0]); else segment(t, pts[i - 1], pts[i]);
        }
    }

    // Strokes only, never clears: the screen clears first, the image export paints paper after.
    function drawAll(t) {
        for (const stroke of strokes.values()) drawFrom(t, stroke, 0);
    }

    function redraw() {
        screen.ctx.clearRect(0, 0, screen.w, screen.h);
        drawAll(screen);
    }

    let penStrokes = 0;

    function updateCount() {
        let n = 0;
        for (const s of strokes.values()) if (s.tool === "pen") n++;
        penStrokes = n;
        const label = `${n} stroke${n === 1 ? "" : "s"}`;
        count.textContent = label;
        canvas.setAttribute("aria-label", `Drawing board with ${label}`);
        updateControls();
    }

    // The export buttons follow whatever board is showing.
    function updateControls() {
        const empty = board === "sketch" ? penStrokes === 0 : frameImage === null;
        const what = board === "sketch" ? "the drawing" : "the tablet screen";
        const nothing = board === "sketch" ? "Nothing to export yet" : "No screen frame yet";
        saveBtn.disabled = empty;
        copyBtn.disabled = empty || !canCopy;
        saveBtn.title = empty ? nothing : `Save ${what} as a PNG`;
        copyBtn.title = !canCopy ? "This browser cannot copy images - use Save PNG"
            : empty ? nothing : `Copy ${what} as an image`;
    }

    // ---- image export (Copy image / Save PNG) ----------------------------------------
    // The page's own copy of the strokes, drawn again at the tablet's screen size in the
    // current view. "snapshot" is the WebSocket message for the full page; this is "export".

    const canCopy = Boolean(navigator.clipboard && navigator.clipboard.write && window.ClipboardItem)
        && (typeof ClipboardItem.supports !== "function" || ClipboardItem.supports("image/png"));

    function exportCanvas() {
        const [w, h] = view === "vertical" ? [EXPORT_SHORT, EXPORT_LONG] : [EXPORT_LONG, EXPORT_SHORT];
        const out = document.createElement("canvas");
        out.width = w;
        out.height = h;
        const x = out.getContext("2d");
        drawAll({ ctx: x, w, h, ink: exportInk, view });
        x.globalCompositeOperation = "destination-over";   // paper behind the ink: opaque, and wipes stay white
        x.fillStyle = exportPaper;
        x.fillRect(0, 0, w, h);
        return out;
    }

    function exportBlob() {
        const source = board === "sketch" ? exportCanvas() : exportFrameCanvas();
        return new Promise((resolve, reject) => {
            source.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("empty"))), "image/png");
        });
    }

    // What the notice says after a copy or save (it fits the notice's 30-character slot): the
    // Screen export names its frame time, or says the menu stayed because it was not recognised.
    function exportNote(verb) {
        const done = verb === "copy" ? "Copied" : "Saved";
        if (board === "sketch") return [verb === "copy" ? "Copied image" : "Saved PNG", ""];
        if (!copyMenu && frameInfo.menu.length === 0) return [`${done} - menu kept`, "menu layout not recognised"];
        return [`${done} screen ${clock(frameInfo.at)}`, ""];
    }

    function stamp() {
        const d = new Date();
        const p = (n) => String(n).padStart(2, "0");
        return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
    }

    let noticeTimer = 0;

    function notify(kind, text, detail) {
        window.clearTimeout(noticeTimer);
        notice.dataset.kind = kind;
        noticeGlyph.textContent = kind === "ok" ? "✓" : "✕";
        noticeText.textContent = text;
        notice.title = detail || "";       // an error name or a short reason, never image data
        noticeTimer = window.setTimeout(() => {
            delete notice.dataset.kind;
            noticeGlyph.textContent = "";
            noticeText.textContent = "";
            notice.title = "";
        }, NOTICE_MS);
    }

    copyBtn.addEventListener("click", async () => {
        try {
            // The Promise goes into the ClipboardItem so the write stays inside the click.
            // The frame is already in memory, so nothing is fetched inside the click.
            await navigator.clipboard.write([new ClipboardItem({ "image/png": exportBlob() })]);
            notify("ok", ...exportNote("copy"));
        } catch (err) {
            notify("fail", "Copy failed - use Save PNG", err && err.name);
        }
    });

    saveBtn.addEventListener("click", async () => {
        try {
            const url = URL.createObjectURL(await exportBlob());
            const link = document.createElement("a");
            link.href = url;
            link.download = board === "sketch" ? `inkboard-${stamp()}.png` : `inkboard-screen-${stamp()}.png`;
            link.click();
            window.setTimeout(() => URL.revokeObjectURL(url), 10000);
            notify("ok", ...exportNote("save"));
        } catch (err) {
            notify("fail", "Save failed", err && err.name);
        }
    });

    // ---- Screen board: the tablet's own screen --------------------------------------------
    // The server grabs a frame when the pen rests (or on Refresh) and sends "screen ready n";
    // the page fetches the newest frame, one fetch at a time, and keeps it only in memory.

    let frameImage = null;     // portrait 1404 x 1872 canvas holding the latest frame
    let frameInfo = null;      // { n, at, layout, menu: [[x, y, w, h], ...] } from the frame's headers
    let fetching = false;
    let fetchAgain = false;
    let frameCss = { w: 0, h: 0, dpr: 1 };

    function clock(iso) {
        const d = new Date(iso);
        const p = (n) => String(n).padStart(2, "0");
        return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }

    function showFrameState(state, text) {
        frameState.dataset.state = state;
        frameGlyph.textContent = { ready: "●", updating: "◌", error: "✕" }[state] || "○";
        frameText.textContent = text;
        frameState.title = text;
        frameState.setAttribute("aria-live", state === "error" ? "polite" : "off");  // errors only
        frameCanvas.classList.toggle("stale", state === "error");
    }

    function fitFrame(cssWidth, cssHeight, dpr) {
        frameCss = { w: cssWidth, h: cssHeight, dpr };
        frameCanvas.width = Math.round(cssWidth * dpr);
        frameCanvas.height = Math.round(cssHeight * dpr);
        frameCanvas.style.width = cssWidth + "px";
        frameCanvas.style.height = cssHeight + "px";
        drawFrame();
    }

    // Draw the portrait frame into a target of w x h: as is in Vertical, a quarter turn
    // clockwise in Horizontal (the same turn the strokes get).
    function paintFrame(x, source, w, h) {
        x.save();
        x.imageSmoothingQuality = "high";
        if (view === "vertical") {
            x.drawImage(source, 0, 0, w, h);
        } else if (HORIZONTAL_CLOCKWISE) {
            x.translate(w, 0);
            x.rotate(Math.PI / 2);
            x.drawImage(source, 0, 0, h, w);
        } else {
            x.translate(0, h);
            x.rotate(-Math.PI / 2);
            x.drawImage(source, 0, 0, h, w);
        }
        x.restore();
    }

    function drawFrame() {
        frameCtx.setTransform(1, 0, 0, 1, 0, 0);
        frameCtx.clearRect(0, 0, frameCanvas.width, frameCanvas.height);
        if (!frameImage) return;
        frameCtx.setTransform(frameCss.dpr, 0, 0, frameCss.dpr, 0, 0);
        paintFrame(frameCtx, frameImage, frameCss.w, frameCss.h);
    }

    // The Screen export: the frame at full size, menu painted paper-white unless Copy menu is
    // ticked (before turning, so the rectangles stay in the frame's own coordinates).
    function exportFrameCanvas() {
        const flat = document.createElement("canvas");
        flat.width = FRAME_W;
        flat.height = FRAME_H;
        const f = flat.getContext("2d");
        f.drawImage(frameImage, 0, 0);
        if (!copyMenu) {
            f.fillStyle = exportPaper;
            for (const [x, y, w, h] of frameInfo.menu) f.fillRect(x, y, w, h);
        }
        if (view === "vertical") return flat;
        const out = document.createElement("canvas");
        out.width = FRAME_H;
        out.height = FRAME_W;
        paintFrame(out.getContext("2d"), flat, FRAME_H, FRAME_W);
        return out;
    }

    function parseMenu(header) {
        if (!header) return [];
        return header.split(";").map((rect) => rect.split(",").map(Number));
    }

    async function fetchFrame() {
        if (fetching) { fetchAgain = true; return; }
        fetching = true;
        try {
            const response = await fetch(FRAME_URL, { cache: "no-store" });
            if (!response.ok) return;  // 404: nothing yet, or the page just left the Screen view
            const n = Number(response.headers.get("X-Inkboard-Frame-N"));
            if (frameInfo && n <= frameInfo.n) return;
            const bytes = new Uint8ClampedArray(await response.arrayBuffer());
            if (bytes.length !== FRAME_W * FRAME_H * 4) throw new Error("size");
            const image = frameImage || document.createElement("canvas");
            image.width = FRAME_W;
            image.height = FRAME_H;
            image.getContext("2d").putImageData(new ImageData(bytes, FRAME_W, FRAME_H), 0, 0);
            frameImage = image;
            frameInfo = {
                n,
                at: response.headers.get("X-Inkboard-Frame-At"),
                layout: response.headers.get("X-Inkboard-Frame-Layout"),
                menu: parseMenu(response.headers.get("X-Inkboard-Frame-Menu")),
            };
            frameCanvas.setAttribute("aria-label", `Tablet screen at ${clock(frameInfo.at)}`);
            showFrameState("ready", `Screen ${clock(frameInfo.at)}`);
            drawFrame();
            updateControls();
        } catch (err) {
            showFrameState("error", "Screen: the frame could not be loaded");
            frameState.title = (err && err.name) || "";
        } finally {
            fetching = false;
            if (fetchAgain) { fetchAgain = false; fetchFrame(); }
        }
    }

    function onScreen(msg) {
        if (msg.state === "updating") {
            showFrameState("updating", "Updating…");
        } else if (msg.state === "ready") {
            fetchFrame();
        } else if (msg.state === "error") {
            const last = frameInfo ? ` (last frame ${clock(frameInfo.at)})` : "";
            showFrameState("error", `Screen: ${msg.detail}${last}`);
        }
    }

    function sendScreen(action) {
        if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "screen", action }));
    }

    function showBoard() {
        document.body.dataset.board = board;
        sketchBtn.setAttribute("aria-pressed", String(board === "sketch"));
        screenBtn.setAttribute("aria-pressed", String(board === "screen"));
        canvas.hidden = board !== "sketch";
        frameCanvas.hidden = board !== "screen";
        if (board === "screen" && !confirmBox.hidden) {
            confirmBox.hidden = true;  // Clear belongs to Sketch
            clearBtn.hidden = false;
        }
        updateControls();
    }

    function setBoard(next) {
        if (next === board) return;
        board = next;
        saveSetting(BOARD_KEY, board);
        showBoard();
        sendScreen(board === "screen" ? "on" : "off");
    }

    sketchBtn.addEventListener("click", () => setBoard("sketch"));
    screenBtn.addEventListener("click", () => setBoard("screen"));
    refreshBtn.addEventListener("click", () => sendScreen("refresh"));
    copyMenuBox.addEventListener("change", () => {
        copyMenu = copyMenuBox.checked;
        saveSetting(COPY_MENU_KEY, copyMenu ? "1" : "0");
    });
    // A hidden tab stops asking the tablet; it asks again when it is shown.
    document.addEventListener("visibilitychange", () => {
        if (board === "screen") sendScreen(document.hidden ? "off" : "on");
    });

    // ---- status chip -----------------------------------------------------------------

    const GLYPHS = { connected: "●", connecting: "◌", down: "✕", offline: "○" };

    function showStatus(state, text) {
        chip.dataset.state = state;
        chipGlyph.textContent = GLYPHS[state] || "○";
        chipText.textContent = text;
        chip.title = text;
    }

    const LINK_NAMES = { usb: "USB", wifi: "Wi-Fi" };

    function onStatus(msg) {
        if (msg.source === "connected") {
            // Over the tablet: "Connected · USB" or "Connected · Wi-Fi <address>"; replay: its detail.
            const link = LINK_NAMES[msg.link];
            const where = link ? ` · ${link}${msg.link === "wifi" && msg.address ? ` ${msg.address}` : ""}` : "";
            showStatus("connected", msg.detail ? `Connected${where} - ${msg.detail}` : `Connected${where}`);
        } else if (msg.source === "connecting") {
            showStatus("connecting", msg.detail ? `Connecting - ${msg.detail}` : "Connecting to tablet…");
        } else {
            const detail = msg.detail || "tablet unreachable";
            showStatus("down", msg.retrying ? `Down - ${detail} (retrying)` : `Down - ${detail}`);
        }
    }

    // ---- messages --------------------------------------------------------------------

    function onMessage(event) {
        const msg = JSON.parse(event.data);
        switch (msg.type) {
            case "snapshot":
                strokes.clear();
                for (const s of msg.strokes) strokes.set(s.id, { tool: s.tool, pts: s.pts });
                redraw();
                updateCount();
                break;
            case "stroke_start": {
                const stroke = { tool: msg.tool, pts: msg.pts.slice() };
                strokes.set(msg.id, stroke);
                drawFrom(screen, stroke, 0);
                updateCount();
                break;
            }
            case "points": {
                const stroke = strokes.get(msg.id);
                if (!stroke) break;
                const start = stroke.pts.length;
                for (const p of msg.pts) stroke.pts.push(p);
                drawFrom(screen, stroke, start);
                break;
            }
            case "stroke_end":
                break;
            case "cleared":
                strokes.clear();
                redraw();
                updateCount();
                break;
            case "status":
                onStatus(msg);
                break;
            case "screen":
                onScreen(msg);
                break;
        }
    }

    // ---- connection ------------------------------------------------------------------

    function connect() {
        const scheme = location.protocol === "https:" ? "wss" : "ws";
        socket = new WebSocket(`${scheme}://${location.host}/api/v1/ws`);
        socket.addEventListener("open", () => {
            attempt = 0;
            // The server forgets a page's Screen view when its socket closes: ask again.
            if (board === "screen" && !document.hidden) sendScreen("on");
        });
        socket.addEventListener("message", onMessage);
        socket.addEventListener("close", () => {
            socket = null;
            showStatus("offline", "Disconnected from inkboard - reconnecting…");
            const delay = RECONNECT_MS[Math.min(attempt, RECONNECT_MS.length - 1)];
            attempt++;
            window.setTimeout(connect, delay);
        });
    }

    // ---- clear with confirmation -----------------------------------------------------

    function askClear(open) {
        confirmBox.hidden = !open;
        clearBtn.hidden = open;
        (open ? document.getElementById("confirm-no") : clearBtn).focus();
    }

    clearBtn.addEventListener("click", () => askClear(true));
    document.getElementById("confirm-no").addEventListener("click", () => askClear(false));
    document.getElementById("confirm-yes").addEventListener("click", () => {
        if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "clear" }));
        askClear(false);
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !confirmBox.hidden) askClear(false); });

    // ---- start -----------------------------------------------------------------------

    const tokens = getComputedStyle(document.documentElement);
    ink = tokens.getPropertyValue("--ink").trim() || ink;
    exportInk = tokens.getPropertyValue("--export-ink").trim() || exportInk;
    exportPaper = tokens.getPropertyValue("--export-paper").trim() || exportPaper;
    copyMenuBox.checked = copyMenu;
    showBoard();
    updateCount();
    showView();
    new ResizeObserver(fit).observe(stage);
    watchScale();
    fit();
    connect();
})();
