// The sign-in and the keyring step-up: app/webxdc.js end to end in a real browser. Headless Chrome over
// CDP with a virtual authenticator (WebAuthn.addVirtualAuthenticator), so the
// passkey ceremony is real and web.py's real webauth checks it. Run by run.sh.
// usage: node drive.mjs <cdpPort> <base> <mode: https|http> <other: a page on a second port, https only, or -> <code>
// <code> is a one-time code from `settled.web enrol`: the first passkey needs it too (no TOFU).
const [cdpPort, base, mode, other, code] = process.argv.slice(2);
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
async function cookie() { const { cookies } = await cdp("Network.getAllCookies"); return cookies.find(c => c.name === "vm_session"); }
async function check() { // GET auth/check from the page, so the browser sends its own cookie
  return await ev(`fetch("auth/check",{cache:"no-store"}).then(r=>r.status)`); }

await cdp("Page.enable"); await cdp("Runtime.enable"); await cdp("Network.enable");
await cdp("WebAuthn.enable");
const AUTH = { options: { protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true } };
const authA = (await cdp("WebAuthn.addVirtualAuthenticator", AUTH)).authenticatorId;
// record every lock ever drawn on each document, from the first byte
// and every alert, and answer a name prompt, so a dialog never blocks headless Chrome
await cdp("Page.addScriptToEvaluateOnNewDocument", { source: `window.__alerts=[];window.alert=m=>window.__alerts.push(String(m));window.__name="Drive key";window.prompt=()=>window.__name;` });
await cdp("Page.addScriptToEvaluateOnNewDocument", { source: `window.__locks=[];new MutationObserver(()=>{document.querySelectorAll('div[style*="z-index:99"]').forEach(d=>{const t=d.innerText.split("\\n").filter(Boolean)[1];if(t&&window.__locks[window.__locks.length-1]!==t)window.__locks.push(t)})}).observe(document,{subtree:true,childList:true,characterData:true});` });
const go = async u => { await cdp("Page.navigate", { url: u }); await sleep(400); await until("document.readyState==='complete'", "load " + u); };
const authed = () => until("!!(window.webxdc&&window.webxdc.auth)", "auth-ok");

// 1. fresh: the first passkey is enrolled with the code (no TOFU), and unlocks
await go(base + "/");
await until("window.__locks.length>0", "lock drawn");
ok((await ev("window.__locks[0]")) === "Enrol this device", "fresh visit draws the enrol sheet, never a bare setup", await ev("JSON.stringify(window.__locks)"));
await ev(`document.getElementById('enrol-code').value=${JSON.stringify(code || "")}`);
await ev("document.getElementById('unlock').click()", true);
await until(`(document.getElementById('unlock')||{}).textContent==="Make the passkey"`, "code accepted, passkey button");
await ev("document.getElementById('unlock').click()", true);
await authed();
await sleep(600);
let c = await cookie();
if (mode === "https") {
  ok(!!c && c.httpOnly && c.secure && c.sameSite === "Strict" && c.path === "/" && c.domain === "localhost" && c.value.length >= 43,
     "https: after the touch the browser holds vm_session HttpOnly Secure SameSite=Strict Path=/ host-only", c && JSON.stringify({ h: c.httpOnly, s: c.secure, ss: c.sameSite, p: c.path, d: c.domain, len: c.value.length }));
  ok((await ev("sessionStorage.length")) === 0, "https: no sessionStorage token written");
  ok((await check()) === 200, "https: /auth/check 200 with the cookie");
  // 2. reload: the cookie resumes, the lock is never drawn
  await go(base + "/");
  await authed(); await sleep(300);
  ok((await ev("window.__locks.length")) === 0, "https: cookie-resumed connect never draws the lock", await ev("JSON.stringify(window.__locks)"));
  ok((await ev("sessionStorage.length")) === 0, "https: still no sessionStorage after a cookie resume");
  // 3. already signed in, ?return=/talk/: bounce straight back, no touch
  await go(base + "/?return=" + encodeURIComponent("/talk/"));
  await until("location.pathname==='/talk/'", "landed on /talk/");
  ok(true, "https: signed in + ?return=/talk/ lands on /talk/ without a touch");
  // 4. refused return: stays in Settings, unlocked, says so
  for (const bad of ["//evil.com/", "https://x/", "/\\evil.com"]) {
    await go(base + "/?return=" + encodeURIComponent(bad));
    await authed();
    await until("window.__locks.length>0", "refusal shown");
    ok((await ev("location.pathname + '|' + window.__locks[0] + '|' + !!window.webxdc.auth")) === "/|That return link was refused|true",
       "https: ?return=" + bad + " refused: stays in Settings, unlocked, says so", await ev("location.href"));
  }
  // 4b. The other-port probe. A page on a SECOND port is same site, so SameSite=Strict
  // sends vm_session on its no-cors fetch of auth/sign-out; the server refuses it (403, nothing revoked)
  const held = (await cookie()).value;
  await go(other + "/");
  ok((await ev("location.origin")) === other && other !== base, "https: probe page loaded on a second port", other);
  const fired = await ev(`fetch(${JSON.stringify(base + "/auth/sign-out")},{credentials:"include",mode:"no-cors",cache:"no-store"}).then(r=>"sent "+r.type,e=>"threw "+e.message)`);
  ok(fired === "sent opaque", "https: the second-port page fired auth/sign-out no-cors with the cookie", fired);
  await sleep(300);
  const kept = await cookie();
  ok(!!kept && kept.value === held, "https: the refusal set no cookie: the browser still holds the same vm_session");
  await go(base + "/");
  await authed(); await sleep(300);
  ok((await ev("window.__locks.length")) === 0, "https: after the cross-port sign-out, the same cookie still unlocks the socket with no touch", await ev("JSON.stringify(window.__locks)"));
  ok((await check()) === 200, "https: /auth/check still 200 with the same cookie after the probe");
  // 5. sign out: auth/sign-out, reload, the lock again; the cookie and the session are gone
  const old = (await cookie()).value;
  await ev("window.webxdc.auth.signOut()");
  await sleep(1500); await until("document.readyState==='complete'", "reloaded");
  await until("window.__locks.length>0", "lock after sign-out");
  ok((await ev("window.__locks[0]")) === "Settings is locked", "https: after signOut the page reloads to the lock");
  ok(!(await cookie()), "https: signOut cleared vm_session in the browser");
  results.push("INFO old-cookie=" + old);
  // 6. signed out, sign in from Talk: ?return=/talk/ -> touch -> /talk/
  await go(base + "/?return=" + encodeURIComponent("/talk/"));
  await until("window.__locks.length>0 && document.getElementById('unlock')", "login lock");
  await ev("document.getElementById('unlock').click()", true);
  await until("location.pathname==='/talk/'", "landed on /talk/ after login", 10000);
  c = await cookie();
  ok(!!c && c.value !== old, "https: sign-in from ?return=/talk/ mints a fresh cookie and lands on /talk/");
} else {
  ok(!c, "http: no vm_session cookie set on localhost");
  ok(!!(await ev("sessionStorage.getItem('settings-web-token')")), "http: the tab token is kept in sessionStorage");
  await go(base + "/");
  await authed(); await sleep(300);
  ok((await ev("window.__locks.length")) === 0, "http: reload resumes with the token, no lock");
  await ev("window.webxdc.auth.signOut()");
  await sleep(1500); await until("document.readyState==='complete'", "reloaded");
  await until("window.__locks.length>0", "lock after sign-out");
  ok((await ev("window.__locks[0]")) === "Settings is locked" && (await ev("sessionStorage.length")) === 0, "http: signOut drops the token and reloads to the lock");
}

// 7. Every keyring change needs a step-up touch, however the page was unlocked
if (mode === "https") { await go(base + "/"); await authed(); }   // the cookie unlocks: no touch
else { await until("document.getElementById('unlock')", "login lock"); await ev("document.getElementById('unlock').click()", true); await authed(); }
await sleep(300);
const keys = async () => JSON.parse(await ev("JSON.stringify(window.webxdc.auth.passkeys)"));
const sheet = t => until(`window.__locks.includes(${JSON.stringify(t)}) && document.getElementById('unlock')`, "sheet " + t);
const tap = () => ev("document.getElementById('unlock').click()", true);
const reloaded = what => until("!window.__old && !!(window.webxdc&&window.webxdc.auth)", "reload after " + what);
const aId = (await keys())[0].id;
ok((await ev("'addOffer' in window.webxdc.auth")) === false, mode + ": the page holds no addOffer after unlock");
// add: step-up touch, then a second tap makes the new passkey; the name rides on the add
await ev("window.__old=1; window.webxdc.auth.addKey()");
await sheet("Confirm with Touch ID"); await tap();
await sheet("Now add the new passkey");
// a second device: authenticator A holds the passkey the add excludes, so B makes the new one
await cdp("WebAuthn.removeVirtualAuthenticator", { authenticatorId: authA });
await cdp("WebAuthn.addVirtualAuthenticator", AUTH);
await tap(); await reloaded("add"); await sleep(300);
let k = await keys();
ok(k.length === 2 && k[1].name === "Drive key" && k[0].id === aId, mode + ": add = step-up touch, then a second tap; the name rides on passkey-add", JSON.stringify(k.map(x => x.name)));
// a same-origin script on its own socket (the planted-passkey attack): unlocked, but every change is refused
const probe = await ev(`new Promise(res => {
  const u = new URL("ws", location.href); u.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const w = new WebSocket(u.href), out = { replies: [] }, s = o => w.send(JSON.stringify(o));
  w.onopen = () => { if (location.protocol !== "https:") s({ type: "resume", token: sessionStorage.getItem("settings-web-token") }); };
  w.onmessage = e => { const m = JSON.parse(e.data);
    if (m.type === "auth-ok") { out.authOk = true; out.addOffer = "addOffer" in m;
      s({ type: "passkey-add", mode: "register", id: "forged", clientDataJSON: "", authenticatorData: "", publicKey: "", alg: -7, name: "pwned" });
      s({ type: "passkey-rename", id: ${JSON.stringify(aId)}, name: "pwned" });
      s({ type: "passkey-remove", id: ${JSON.stringify(aId)} });
      s({ type: "step-up-begin", op: "remove", id: ${JSON.stringify(aId)} });   // a challenge is not a touch
      s({ type: "passkey-remove", id: ${JSON.stringify(aId)} });
    } else if (m.type === "step-up") { out.stepUp = true;
    } else if (m.type.indexOf("passkey-") === 0) {
      out.replies.push({ type: m.type, why: m.why, n: m.passkeys.length, names: m.passkeys.map(x => x.name).join(",") });
      if (out.replies.length === 4) { w.close(); res(out); }
    } };
  setTimeout(() => res(out), 4000); })`);
ok(probe.authOk && probe.addOffer === false, mode + ": a same-origin socket unlocks (" + (mode === "https" ? "cookie" : "token") + ") but its auth-ok carries no addOffer", JSON.stringify({ authOk: probe.authOk, addOffer: probe.addOffer }));
ok(probe.replies.length === 4 && probe.replies.every(r => r.why === "touch to confirm first" && r.n === 2 && r.names === "Mac,Drive key"),
   mode + ": without the step-up, passkey-add, -rename and -remove (even after step-up-begin) are each refused; the keyring is unchanged", JSON.stringify(probe.replies));
ok(probe.stepUp === true, mode + ": step-up-begin answers with a login challenge only");
k = await keys();
ok(k.length === 2 && k[0].name !== "pwned", mode + ": the page's keyring is unchanged by the probe");
// cancel: the sheet goes, nothing is sent, nothing changes
await ev("window.__old=1; window.webxdc.auth.rename(" + JSON.stringify(aId) + ", 'Never')");
await sheet("Confirm with Touch ID");
await ev("document.getElementById('cancel').click()", true); await sleep(800);
ok((await ev("window.__old === 1 && !document.getElementById('unlock')")) && (await keys())[0].name === "Mac", mode + ": cancel on the step-up sheet sends nothing");
// rename: index.html already asked the name; the touch confirms it
await ev("window.webxdc.auth.rename(" + JSON.stringify(aId) + ", 'Old Mac')");
await sheet("Confirm with Touch ID"); await tap(); await reloaded("rename"); await sleep(300);
k = await keys();
ok(k.length === 2 && k[0].id === aId && k[0].name === "Old Mac", mode + ": rename = step-up touch, then passkey-rename", JSON.stringify(k.map(x => x.name)));
// remove: index.html already confirmed; the touch (with B now) removes A's passkey
await ev("window.__old=1; window.webxdc.auth.remove(" + JSON.stringify(aId) + ")");
await sheet("Confirm with Touch ID"); await tap(); await reloaded("remove"); await sleep(300);
k = await keys();
ok(k.length === 1 && k[0].name === "Drive key", mode + ": remove = step-up touch, then passkey-remove", JSON.stringify(k.map(x => x.name)));
ok((await ev("window.__alerts.length")) === 0, mode + ": no alerts along the way", await ev("JSON.stringify(window.__alerts)"));
console.log(results.join("\n")); ws.close(); process.exit(failed ? 1 : 0);
