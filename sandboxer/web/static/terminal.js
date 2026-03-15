/* xterm.js <-> WebSocket glue for sandboxer terminal sessions. */
(function () {
  "use strict";

  var sandboxName = window.SANDBOX_NAME;
  var statusDot = document.getElementById("status-dot");
  var statusText = document.getElementById("status-text");
  var container = document.getElementById("terminal-container");

  var term = new window.Terminal({
    cursorBlink: true,
    fontSize: 14,
    fontFamily: "'Menlo', 'DejaVu Sans Mono', 'Courier New', monospace",
    theme: {
      background: "#0f172a",
      foreground: "#e2e8f0",
      cursor: "#06b6d4",
      selectionBackground: "#334155",
    },
  });

  var fitAddon = new window.FitAddon.FitAddon();
  term.loadAddon(fitAddon);

  var webLinksAddon = new window.WebLinksAddon.WebLinksAddon();
  term.loadAddon(webLinksAddon);

  term.open(container);
  fitAddon.fit();

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

    // Forward auth cookie automatically; add token param if present.
    var params = new URLSearchParams(location.search);
    var token = params.get("token");
    if (token) {
      url += "?token=" + encodeURIComponent(token);
    }

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
    if ("ontouchstart" in window || navigator.maxTouchPoints > 0) {
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
        term.focus();
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(new TextEncoder().encode(key));
        }
      } else if (ctrl) {
        // Ctrl+<char> = char code - 96
        var code = ctrl.charCodeAt(0) - 96;
        term.focus();
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(new Uint8Array([code]));
        }
      } else if (esc) {
        term.focus();
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(new TextEncoder().encode(esc));
        }
      }
    });
  }

  connect();
})();
