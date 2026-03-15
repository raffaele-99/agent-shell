/* xterm.js <-> WebSocket glue for sandboxer terminal sessions. */
(function () {
  "use strict";

  var sandboxName = window.SANDBOX_NAME;
  var statusDot = document.getElementById("status-dot");
  var statusText = document.getElementById("status-text");
  var container = document.getElementById("terminal-container");

  var isMobile = ("ontouchstart" in window || navigator.maxTouchPoints > 0);

  var term = new window.Terminal({
    cursorBlink: true,
    fontSize: isMobile ? 12 : 14,
    fontFamily: "'Menlo', 'DejaVu Sans Mono', 'Courier New', monospace",
    theme: {
      background: "#0f172a",
      foreground: "#e2e8f0",
      cursor: "#06b6d4",
      selectionBackground: "#334155",
    },
    // Mobile: prevent xterm from swallowing touch events needed for keyboard
    allowProposedApi: true,
  });

  var fitAddon = new window.FitAddon.FitAddon();
  term.loadAddon(fitAddon);

  var webLinksAddon = new window.WebLinksAddon.WebLinksAddon();
  term.loadAddon(webLinksAddon);

  term.open(container);
  fitAddon.fit();

  // Mobile: ensure tapping the terminal focuses the hidden textarea so the
  // on-screen keyboard appears and xterm receives keystrokes.
  if (isMobile) {
    container.addEventListener("touchstart", function () {
      term.focus();
      // xterm.js uses a hidden <textarea> for input — make sure it's focused
      var ta = container.querySelector(".xterm-helper-textarea");
      if (ta) ta.focus();
    });
  }

  var ws = null;
  var reconnectDelay = 1000;
  var maxReconnectDelay = 16000;

  var statusColors = {
    connecting: "bg-amber-500",
    connected: "bg-emerald-500",
    disconnected: "bg-red-500",
  };

  function setStatus(state, text) {
    statusDot.className = "w-2 h-2 rounded-full " + (statusColors[state] || "bg-slate-500");
    statusText.textContent = text;
  }

  function sendResize() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: "resize",
        rows: term.rows,
        cols: term.cols,
      }));
    }
  }

  function connect() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    var url = proto + "//" + location.host + "/ws/terminal/" + sandboxName;

    // Build query params: auth token + mode (shell or agent).
    var qp = [];
    var token = window.WS_TOKEN || new URLSearchParams(location.search).get("token");
    if (token) qp.push("token=" + encodeURIComponent(token));
    var mode = window.WS_MODE || "shell";
    if (mode !== "shell") qp.push("mode=" + encodeURIComponent(mode));
    if (qp.length) url += "?" + qp.join("&");

    setStatus("connecting", "Connecting...");
    ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";

    ws.onopen = function () {
      setStatus("connected", "Connected");
      reconnectDelay = 1000;
      sendResize();
    };

    ws.onmessage = function (ev) {
      if (ev.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(ev.data));
      } else {
        term.write(ev.data);
      }
    };

    ws.onclose = function () {
      setStatus("disconnected", "Disconnected — reconnecting...");
      setTimeout(function () {
        reconnectDelay = Math.min(reconnectDelay * 2, maxReconnectDelay);
        connect();
      }, reconnectDelay);
    };

    ws.onerror = function () {
      ws.close();
    };
  }

  // Terminal -> WebSocket.
  term.onData(function (data) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(new TextEncoder().encode(data));
    }
  });

  // Handle resize.
  window.addEventListener("resize", function () {
    fitAddon.fit();
    sendResize();
  });

  new ResizeObserver(function () {
    fitAddon.fit();
    sendResize();
  }).observe(container);

  // Mobile keyboard helper row.
  var kbdHelper = document.getElementById("kbd-helper");
  if (kbdHelper) {
    // Show on touch devices.
    if (isMobile) {
      kbdHelper.classList.remove("hidden");
      kbdHelper.classList.add("flex");
    }

    kbdHelper.addEventListener("click", function (e) {
      var btn = e.target.closest("button");
      if (!btn) return;

      var key = btn.getAttribute("data-key");
      var ctrl = btn.getAttribute("data-ctrl");
      var esc = btn.getAttribute("data-esc");

      if (key) {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(new TextEncoder().encode(key));
        }
      } else if (ctrl) {
        var code = ctrl.charCodeAt(0) - 96;
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(new Uint8Array([code]));
        }
      } else if (esc) {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(new TextEncoder().encode(esc));
        }
      }

      term.focus();
    });
  }

  connect();
})();
