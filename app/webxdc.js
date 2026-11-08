// webxdc.js for the WEB: the same index.html that runs in Delta Chat runs in a
// browser with this file beside it. Delta supplies its own webxdc.js and never
// sees this one (`make build` zips without it).
//
// Delta carries an update as email through the chat. Here a websocket to
// web.py carries it instead; web.py keeps the numbered log and answers with
// the Mac's state, the way the bot and apply.py do for the phone.
// The ask: see the settings app working in a web browser, over WebSockets.
//
// LOCKED with a passkey: web.py sends nothing until
// Touch ID or Face ID. This file shows the lock, runs the passkey ceremony on
// the tap, and keeps a resume token for the tab, so a reload is not a new touch.
// On https (the ts.net origin) the token is not kept: after auth-ok the
// page swaps its one-time code for the host-wide vm_session cookie at
// GET auth/session, and the cookie is what resumes. localhost keeps the token.
// The phone's Delta app never loads this file, so it is untouched.
//
// Changing the keyring (add, rename or remove a passkey) needs a STEP-UP touch,
// however the page was unlocked: a cookie unlocks with no touch, so a
// same-origin script could otherwise plant a passkey of its own. Each touch
// gets its own sheet and button, because Safari wants every ceremony to start
// inside a tap: step-up-begin -> "Confirm with Touch ID" -> step-up -> the
// change. Add takes a second tap: the new passkey is made from the addOffer
// that only step-up-ok carries, and its name rides on the passkey-add.
(function () {
  "use strict";
  var ws = null, listener = null, last = 0, outbox = [], readyResolve = null;
  var hello = { selfAddr: "web", selfName: "owner (browser)", host: "" };   // web.py's hello names the host
  var authed = false, pending = null, lock = null, TOKEN = "settings-web-token";
  var change = null;   // the keyring change waiting on its step-up touch: {op, id, name}
  var failed = "";     // why the last auth-finish was refused: the enrol sheet shows it
  var enrolCode = "";  // the code the server pre-checked (enrol-ok): it rides on the enrol
  var HTTPS = location.protocol === "https:";   // cookie on https; the tab token on http only
  function token() { return HTTPS ? null : sessionStorage.getItem(TOKEN); }

  // https: swap auth-ok's one-time code for the vm_session cookie (a ws message
  // cannot set one). Relative, so under /settings/ it asks /settings/auth/session.
  // `return` comes from our own ?return=; the server is the judge of it, and the
  // page only goes where the server echoed back (design.md 'app/webxdc.js').
  function exchange(code) {
    var ret = new URLSearchParams(location.search).get("return");
    var u = "auth/session?code=" + encodeURIComponent(code) + (ret ? "&return=" + encodeURIComponent(ret) : "");
    return fetch(u, { credentials: "same-origin", cache: "no-store" }).then(function (r) {
      if (r.status === 400) {   // a refused return: stay here, unlocked, and say so
        showLock("That return link was refused", "It must be a path on this site, like /talk/. You are in Settings.",
          "OK", hideLock);
        return;
      }
      if (!r.ok) return;        // a stale code (a reconnect raced it): the ws is unlocked anyway
      return r.json().then(function (b) { if (b && b.return) location.replace(b.return); });
    }).catch(function () {});
  }

  function url() {   // relative to the page, so it works at / and under /settings/
    var u = new URL("ws", location.href);
    u.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    return u.href;
  }
  function b64u(buf) {
    var s = "", b = new Uint8Array(buf);
    for (var i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function unb64u(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/"); while (s.length % 4) s += "=";
    var r = atob(s), b = new Uint8Array(r.length);
    for (var i = 0; i < r.length; i++) b[i] = r.charCodeAt(i);
    return b.buffer;
  }
  function send(o) { ws.send(JSON.stringify(o)); }

  // The lock: a sheet over the page until web.py says auth-ok.
  function showLock(title, text, button, onTap, links, onCancel) {
    if (!lock) {
      // In the shell the lock is Settings' own, over its room: Talk beside it stays Talk, with its
      // own "not paired". A page with no Settings room keeps the whole-page lock.
      var room = document.getElementById("room-settings");
      lock = document.createElement("div");
      lock.setAttribute("style", (room ? "position:absolute" : "position:fixed") + ";inset:0;z-index:99;display:flex;align-items:center;" +
        "justify-content:center;background:rgba(0,0,0,.55);font:17px -apple-system,system-ui,sans-serif");
      (room || document.body).appendChild(lock);
    }
    var h = '<div style="background:Canvas;color:CanvasText;border-radius:14px;padding:22px;max-width:320px;text-align:center">' +
      '<div style="font-size:34px">🔒</div><div style="font-weight:600;margin:6px 0">' + title + '</div>' +
      '<div style="opacity:.7;font-size:14px;margin-bottom:14px">' + text + '</div>';
    if (button) h += '<button id="unlock" style="font:inherit;padding:10px 18px;border-radius:10px;border:0;' +
      'background:#0a84ff;color:#fff">' + button + '</button>';
    if (onCancel) h += ' <button id="cancel" style="font:inherit;padding:10px 18px;border-radius:10px;border:0;' +
      'background:rgba(127,127,127,.25);color:inherit">Cancel</button>';
    (links || []).forEach(function (l) { h += '<div style="margin-top:10px"><a href="' + l + '">' + l + '</a></div>'; });
    lock.innerHTML = h + '</div>';
    var b = document.getElementById("unlock"); if (b) b.onclick = onTap;
    var x = document.getElementById("cancel"); if (x) x.onclick = onCancel;
  }
  function hideLock() { if (lock) { lock.remove(); lock = null; } }

  // a new passkey: kind "auth-finish" (the first, or an enrol) or "passkey-add"; `extra`
  // rides on the message (an enrol's {mode: "enrol", code})
  function create(o, kind, ask, extra) {
    return navigator.credentials.create({ publicKey: {
      challenge: unb64u(o.challenge), rp: { id: o.rpId, name: (hello.host ? hello.host + " " : "") + "Settings" },
      user: { id: unb64u(o.user.id), name: o.user.name, displayName: o.user.displayName },
      pubKeyCredParams: [{ type: "public-key", alg: -7 }, { type: "public-key", alg: -257 }],
      authenticatorSelection: { userVerification: "required", residentKey: "preferred" },
      excludeCredentials: (o.exclude || []).map(function (id) { return { type: "public-key", id: unb64u(id) }; }),
      attestation: "none", timeout: 60000 } }).then(function (c) {
      var name = c.authenticatorAttachment === "cross-platform" ? "Security key"   // a YubiKey
        : navigator.userAgent.indexOf("iPhone") >= 0 ? "iPhone" : "Mac";
      // the name rides on the add, so the user can enter it; the
      // passkey already exists on the device by now, so a cancelled prompt keeps the default
      if (ask) name = (prompt("Name this passkey", name) || "").trim() || name;
      var m = { type: kind, mode: "register", id: c.id,
        clientDataJSON: b64u(c.response.clientDataJSON),
        authenticatorData: b64u(c.response.getAuthenticatorData()),
        publicKey: b64u(c.response.getPublicKey()), alg: c.response.getPublicKeyAlgorithm(), name: name };
      for (var k in (extra || {})) m[k] = extra[k];
      send(m);
    });
  }

  function assertion(o) {   // navigator.credentials.get over a login offer (unlock or step-up)
    return navigator.credentials.get({ publicKey: {
      challenge: unb64u(o.challenge), rpId: o.rpId, userVerification: "required", timeout: 60000,
      allowCredentials: o.allow.map(function (id) { return { type: "public-key", id: unb64u(id) }; }) } })
      .then(function (a) {
        return { id: a.id, clientDataJSON: b64u(a.response.clientDataJSON),
          authenticatorData: b64u(a.response.authenticatorData), signature: b64u(a.response.signature) };
      });
  }

  // The step-up: every keyring change asks web.py for a fresh login
  // challenge, and the touch over it buys exactly that one change.
  function stepUp(c) {
    change = c;
    send({ type: "step-up-begin", op: c.op, id: c.id });
  }
  function dropChange() { change = null; hideLock(); }
  function refused(title, why) {
    change = null;
    showLock(title, String(why || "Nothing was changed."), "OK", hideLock);
  }
  function onStepUp(m) {
    var c = change;
    if (m.type === "step-up") {   // the challenge: the touch must start inside its own tap
      if (!c || m.op !== c.op) return;
      showLock("Confirm with Touch ID", c.op === "add" ? "Touch first, then add the new passkey."
        : c.op === "remove" ? "Touch to remove this passkey." : "Touch to rename this passkey.",
        "Confirm", function () {
          assertion(m).then(function (a) { a.type = "step-up"; send(a); hideLock(); })
            .catch(function (e) { refused("Not confirmed", e && e.message || e); });
        }, null, dropChange);
    } else if (m.type === "step-up-ok") {
      if (!c || m.op !== c.op) return;
      if (c.op === "add") {   // a second tap: the new passkey, from the offer this touch bought
        showLock("Now add the new passkey", "Touch ID here, a YubiKey, or another device.", "Add", function () {
          hideLock(); change = null;
          create(m.addOffer, "passkey-add", true).catch(function (e) {
            refused("Not added", e && e.name === "InvalidStateError" ? "This device already has a passkey here."
              : e && e.message || e); });
        }, null, dropChange);
      } else {
        change = null;
        if (c.op === "remove") send({ type: "passkey-remove", id: c.id });
        else send({ type: "passkey-rename", id: c.id, name: c.name });
      }
    } else if (m.type === "step-up-failed") {
      refused("Not confirmed", m.why);
    }
  }

  // Every address enrols its first passkey with the one-time code that `settled
  // enrol` prints on the machine itself, the very first of all included:
  // a locked page is never offered a bare registration (no trust on first use).
  // The code is PRE-CHECKED (enrol-check) before any passkey is made, so a
  // mistyped one leaves no dead passkey on the device; only enrol-ok
  // offers the passkey tap.
  function enrolSheet(why) {
    enrolCode = "";
    showLock("Enrol this device", (why ? "<b>" + esc(why) + "</b><br>" : "") +
      "No passkey for this address yet. On this machine run <code>settled enrol</code>, then type its code. " +
      "The code is checked before any passkey is made." +
      '<div style="margin-top:12px"><input id="enrol-code" autocomplete="one-time-code" autocapitalize="characters" ' +
      'spellcheck="false" placeholder="Enrol code" style="font:inherit;text-align:center;letter-spacing:.12em;' +
      'width:11em;padding:8px;border-radius:8px;border:1px solid rgba(127,127,127,.5)"></div>',
      "Enrol this device", ceremony);
    var i = document.getElementById("enrol-code");
    if (i) i.onkeydown = function (e) { if (e.key === "Enter") ceremony(); };
  }
  function onEnrol(m) {   // the server's answer to enrol-check
    if (m.type === "enrol-ok" && enrolCode && pending && pending.mode === "enrol") {
      // a second tap, not a create() here: Safari wants a gesture for passkeys, and a
      // websocket reply is not one
      showLock("Code accepted", "Now make this device's passkey: Touch ID here, Face ID on the phone.",
        "Make the passkey", enrolCreate);
    } else if (m.type === "enrol-failed") {
      enrolSheet(m.why || "the code was refused");
    }
  }
  function enrolCreate() {   // runs inside the tap, only after enrol-ok
    var o = pending; if (!o || o.mode !== "enrol" || !enrolCode) return;
    // the finish checks the code again and spends it: the pre-check alone enrols nothing
    create(o, "auth-finish", false, { mode: "enrol", code: enrolCode })
      .catch(function (e) { enrolSheet(String(e && e.message || e)); });
  }
  function esc(t) {
    return String(t).replace(/[&<>"']/g, function (c) { return "&#" + c.charCodeAt(0) + ";"; });
  }

  function ceremony() {   // runs inside the tap: Safari wants a gesture for passkeys
    var o = pending; if (!o) return;
    if (o.mode === "enrol") {
      var i = document.getElementById("enrol-code"), code = i ? i.value.trim() : "";
      if (!code) { if (i) i.focus(); return; }   // no code, nothing sent: an empty one costs a try
      enrolCode = code;
      send({ type: "enrol-check", code: code });   // the code first: no passkey is made until enrol-ok
      return;
    }
    assertion(o).then(function (a) { a.type = "auth-finish"; a.mode = "login"; send(a); })
      .catch(function (e) { showLock("Not unlocked", String(e && e.message || e), "Try again", ceremony); });
  }

  function onAuth(m) {
    if (m.type === "auth") {
      var why = failed; pending = m; failed = "";
      if (token()) return;   // resuming: wait for auth-ok or auth-failed
      if (m.mode === "login") showLock("Settings is locked", "Unlock with Touch ID or Face ID.", "Unlock", ceremony);
      else if (m.mode === "enrol") enrolSheet(why);
      else showLock("No passkey for this address", "Unlock on the address that has one, then add this one.", "", null);
    } else if (m.type === "auth-ok") {
      authed = true; hideLock();
      if (!HTTPS) sessionStorage.setItem(TOKEN, m.token);
      else if (m.code) exchange(m.code);
      window.webxdc.auth = { user: m.user || "owner", since: m.since || "", passkeys: m.passkeys || [],
        // each keyring change asks for its own step-up touch first
        rename: function (id, name) { stepUp({ op: "rename", id: id, name: name }); },
        remove: function (id) { stepUp({ op: "remove", id: id }); },
        signOut: function () {   // ends the session for every app on the host (https), or this tab's token (http)
          send({ type: "sign-out" });
          if (!HTTPS) { sessionStorage.removeItem(TOKEN); location.reload(); return; }
          fetch("auth/sign-out", { credentials: "same-origin", cache: "no-store" })
            .catch(function () {}).then(function () { location.reload(); });
        },
        addKey: function () { stepUp({ op: "add" }); } };   // no offer exists until the step-up
      send({ type: "since", serial: last });
      while (outbox.length) ws.send(outbox.shift());
    } else if (m.type === "passkey-added" || m.type === "passkey-renamed") {   // remove answers renamed too
      if (m.why) { refused(m.type === "passkey-added" ? "Not added" : "Not changed", m.why); return; }
      if (window.webxdc.auth) window.webxdc.auth.passkeys = m.passkeys || [];
      location.reload();   // the tab keeps its session; the You page shows the names
    } else if (m.type === "auth-failed") {
      if (m.quiet) { if (!HTTPS) sessionStorage.removeItem(TOKEN); if (pending) onAuth(pending); return; }
      failed = m.why || "";   // a fresh offer follows: an enrol sheet says why
      showLock("Not unlocked", m.why, "Try again", ceremony);
    } else if (m.type === "auth-refused") {
      showLock("Open Settings from its own address", "This address (" + m.origin + ") can't hold a passkey.", "", null, m.open);
    }
  }

  function connect() {
    ws = new WebSocket(url());
    authed = false; change = null;   // a step-up never outlives its socket (web.py keeps it on the ws)
    ws.onopen = function () {
      var t = token();
      if (t) send({ type: "resume", token: t });
    };
    ws.onmessage = function (ev) {
      var m = JSON.parse(ev.data);
      if (m.type === "hello") {
        window.webxdc.selfAddr = m.selfAddr || hello.selfAddr;
        window.webxdc.selfName = m.selfName || hello.selfName;
        hello.host = m.host || hello.host;
      } else if (m.type.indexOf("auth") === 0 || m.type.indexOf("passkey-") === 0) {
        onAuth(m);
      } else if (m.type.indexOf("step-up") === 0) {
        onStepUp(m);
      } else if (m.type.indexOf("enrol-") === 0) {
        onEnrol(m);
      } else if (m.type === "update") {
        if (m.update.serial <= last) return;   // a replay we already have
        last = m.update.serial;
        if (listener) listener(m.update);
      } else if (m.type === "caught-up" && readyResolve) {
        readyResolve(); readyResolve = null;
      }
    };
    ws.onclose = function () { setTimeout(connect, 1000); };   // the Mac restarted: come back, ask since `last`
  }

  window.webxdc = {
    selfAddr: hello.selfAddr,
    selfName: hello.selfName,
    sendUpdate: function (update, descr) {
      var s = JSON.stringify({ type: "update", update: update, descr: descr || "" });
      if (ws && ws.readyState === 1 && authed) ws.send(s); else outbox.push(s);
    },
    setUpdateListener: function (cb, serial) {
      listener = cb; last = serial || 0;
      var ready = new Promise(function (r) { readyResolve = r; });
      if (!ws) connect();
      return ready;
    },
    sendToChat: function () { return Promise.reject(new Error("no chat on the web")); },
    importFiles: function () { return Promise.reject(new Error("no file picker on the web yet")); },
    joinRealtimeChannel: function () { throw new Error("no realtime channel on the web"); }
  };
})();
