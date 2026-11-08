// The enrol sheet in a real browser. A host whose store holds only ANOTHER
// host's passkey is offered "enrol": the page asks for the one-time code `web.py enrol`
// printed, and a virtual authenticator makes this host's own passkey. Run by run.sh enrol.
// The code is pre-checked (enrol-check) BEFORE the ceremony, so a wrong code never
// calls navigator.credentials.create() and leaves no passkey on the device.
// usage: node enrol.mjs <cdpPort> <base> <code>
const [cdpPort, base, code] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const list = await (await fetch(`http://127.0.0.1:${cdpPort}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => ws.onopen = r);
let id = 0; const wait = {};
ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && wait[m.id]) { wait[m.id](m); delete wait[m.id]; } };
const cdp = (method, params = {}) => new Promise((res, rej) => { const i = ++id; wait[i] = m => m.error ? rej(new Error(method + ": " + JSON.stringify(m.error))) : res(m.result); ws.send(JSON.stringify({ id: i, method, params })); });
const ev = async (expr, gesture) => (await cdp("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true, userGesture: !!gesture })).result.value;
async function until(expr, what, ms = 8000) { for (let t = 0; t < ms; t += 100) { try { const v = await ev(expr); if (v) return v; } catch (e) {} await sleep(100); } throw new Error("timeout: " + what); }
const results = []; let failed = 0;
function ok(cond, name, extra) { results.push((cond ? "PASS " : "FAIL ") + name + (extra ? "  " + extra : "")); if (!cond) failed++; }

await cdp("Page.enable"); await cdp("Runtime.enable"); await cdp("WebAuthn.enable");
const { authenticatorId } = await cdp("WebAuthn.addVirtualAuthenticator", { options: { protocol: "ctap2", transport: "internal", hasResidentKey: true,
  hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true } });
await cdp("Page.addScriptToEvaluateOnNewDocument", { source: `window.__alerts=[];window.alert=m=>window.__alerts.push(String(m));` });
await cdp("Page.addScriptToEvaluateOnNewDocument", { source: `window.__locks=[];new MutationObserver(()=>{document.querySelectorAll('div[style*="z-index:99"]').forEach(d=>{const t=d.innerText.split("\\n").filter(Boolean)[1];if(t&&window.__locks[window.__locks.length-1]!==t)window.__locks.push(t)})}).observe(document,{subtree:true,childList:true,characterData:true});` });
// every navigator.credentials.create() the page makes, and what the device holds
await cdp("Page.addScriptToEvaluateOnNewDocument", { source: `window.__creates=0;(()=>{const c=navigator.credentials.create.bind(navigator.credentials);navigator.credentials.create=o=>{window.__creates++;return c(o)}})();` });
const onDevice = async () => (await cdp("WebAuthn.getCredentials", { authenticatorId })).credentials.length;
const sheet = () => ev(`(document.querySelector('div[style*="z-index:99"]')||{}).innerText||""`);
const type = v => ev(`document.getElementById('enrol-code').value=${JSON.stringify(v)}`);
const tap = () => ev("document.getElementById('unlock').click()", true);

try {   // a step that times out still prints the results so far
// 1. the second host: the enrol sheet, a code input and its button
await cdp("Page.navigate", { url: base + "/" });
await until("window.__locks.length>0", "lock drawn");
ok((await ev("window.__locks[0]")) === "Enrol this device", "a host with only another host's passkey draws the enrol sheet", await ev("JSON.stringify(window.__locks)"));
ok((await ev("!!document.getElementById('enrol-code') && document.getElementById('unlock').textContent")) === "Enrol this device",
   "the sheet has a code input and an Enrol this device button");
// 2. no code: nothing is sent, the sheet stays
await tap(); await sleep(600);
ok((await ev("!window.webxdc.auth && !!document.getElementById('enrol-code') && window.__locks.length===1")) === true,
   "Enrol with an empty code starts no ceremony and stays locked", await ev("JSON.stringify(window.__locks)"));
// 3. a wrong code: refused, the enrol sheet comes back saying why
await type("WRONGCODE9"); await tap();
await until(`(document.querySelector('div[style*="z-index:99"]')||{}).innerText.indexOf("wrong enrol code")>=0`, "wrong-code why");
ok((await ev("!window.webxdc.auth && !!document.getElementById('enrol-code')")) === true,
   "a wrong code is refused: still locked, the enrol sheet is back with why", JSON.stringify(await sheet()));
const made = await ev("window.__creates"), held = await onDevice();
ok(made === 0 && held === 0, "a wrong code never starts the passkey ceremony: no credentials.create(), no passkey on the device",
   `creates=${made} device=${held}`);
// 4. the code from `web.py enrol`, typed lower case with a space: enrols and unlocks
await type(" " + code.toLowerCase() + " "); await tap();
// the right code: enrol-ok, then a second tap makes the passkey (Safari wants a gesture for it)
await until(`(document.getElementById('unlock')||{}).textContent==="Make the passkey"`, "code accepted, passkey button");
ok((await ev("window.__creates")) === 0, "a right code alone starts no ceremony: it waits for the tap");
await tap();
await until("!!(window.webxdc&&window.webxdc.auth)", "auth-ok");
ok((await ev("window.__creates")) === 1 && (await onDevice()) === 1, "the tap after enrol-ok makes exactly one passkey",
   `creates=${await ev("window.__creates")} device=${await onDevice()}`);
const keys = await ev("JSON.stringify(window.webxdc.auth.passkeys.map(k=>k.rp_id))");
ok(JSON.stringify(["studio.enrol-rig.ts.net", "localhost"]) === keys, "the code enrols THIS host's passkey beside the other host's, and unlocks", keys);
ok((await ev("window.__alerts.length")) === 0, "no alerts along the way", await ev("JSON.stringify(window.__alerts)"));
} catch (e) { ok(false, String(e && e.message || e)); }
console.log(results.join("\n")); ws.close(); process.exit(failed ? 1 : 0);
