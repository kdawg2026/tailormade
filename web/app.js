const $ = (id) => document.getElementById(id);
const STORAGE_KEY = "tailormade.master";

function remember(text) {
  try { localStorage.setItem(STORAGE_KEY, text); } catch {}
}
try { $("master").value = localStorage.getItem(STORAGE_KEY) || ""; } catch {}
$("master").addEventListener("input", (e) => remember(e.target.value));

async function postJson(url, body = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

// Import and tailor answer with JSON lines: {"log"} while working, then one
// {"result"}, {"files"}, or {"error"}. Blank lines are heartbeats.
async function postStream(url, body, logEl, onLog = () => {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401) showAccount(null);
    throw new Error(payload.error || response.statusText);
  }
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  const log = [];
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) throw new Error("The connection closed before the job finished. Try again.");
    buffer += value;
    const lines = buffer.split("\n");
    buffer = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      const item = JSON.parse(line);
      if ("log" in item) {
        onLog(item.log);
        if (logEl) {
          log.push(item.log);
          logEl.textContent = log.join("\n");
          logEl.scrollTop = logEl.scrollHeight;
        }
      } else {
        return item;
      }
    }
  }
}

// Model output is untrusted text, so every notice is built with textContent.
function showNotice(el, header, items = [], advice = "") {
  el.replaceChildren();
  if (!header) return;
  const strong = document.createElement("strong");
  strong.textContent = header;
  el.append(strong);
  if (items.length) {
    const ul = document.createElement("ul");
    for (const item of items) {
      const li = document.createElement("li");
      li.textContent = item;
      ul.append(li);
    }
    el.append(ul);
  }
  if (advice) {
    const p = document.createElement("p");
    p.className = "advice";
    p.textContent = advice;
    el.append(p);
  }
}

function readBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function run(button, fn) {
  button.disabled = true;
  try { await fn(); } finally { button.disabled = false; }
}

// ---- account ----

function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }

function showAccount(me) {
  $("auth-form").hidden = Boolean(me);
  $("signed-in").hidden = !me;
  if (!me) return;
  $("who").textContent = me.email;
  const parts = [];
  if (me.free_left) parts.push(plural(me.free_left, "free tailor"));
  if (me.subscription.active) {
    const renews = new Date(me.subscription.renews).toLocaleDateString();
    parts.push(`${me.subscription.left} of this month's tailors (renews ${renews})`);
  }
  if (me.pack_credits) parts.push(plural(me.pack_credits, "pack tailor"));
  $("balance").textContent = parts.length ? `You have ${parts.join(" + ")} left.` : "You're out of tailors.";
  const subscribed = me.subscription.active || ["past_due", "unpaid"].includes(me.subscription.status);
  $("subscribe").hidden = subscribed;
  // Packs are one-time top-ups: nothing to manage, so the portal is for subscribers only.
  $("manage").hidden = !subscribed;
  for (const button of document.querySelectorAll('[data-plan^="pack_"]')) {
    button.textContent = me.pack_credits ? "Buy another pack" : "Buy pack";
  }
  showNotice($("billing-note"), me.billing_hold
    ? "Paid tailors are paused because of a payment dispute. Contact support to resolve it."
    : me.subscription.status === "past_due"
      ? "Your last monthly payment failed. Update your card under Manage subscription."
      : "");
}

async function refreshAccount() {
  const response = await fetch("/api/me");
  const me = response.ok ? await response.json() : null;
  showAccount(me);
  return me;
}

async function authenticate(url) {
  showNotice($("auth-error"));
  try {
    showAccount(await postJson(url, { email: $("email").value, password: $("password").value }));
    $("password").value = "";
  } catch (err) {
    showNotice($("auth-error"), err.message);
  }
}

$("auth-form").addEventListener("submit", (e) => {
  e.preventDefault();
  run($("login"), () => authenticate("/api/login"));
});
$("signup").addEventListener("click", () => {
  if ($("auth-form").reportValidity()) run($("signup"), () => authenticate("/api/signup"));
});
$("logout").addEventListener("click", () => run($("logout"), async () => {
  await postJson("/api/logout");
  showAccount(null);
}));

for (const button of document.querySelectorAll("[data-plan]")) {
  button.addEventListener("click", () => run(button, async () => {
    try {
      location.href = (await postJson("/api/checkout", { plan: button.dataset.plan })).url;
    } catch (err) {
      showNotice($("billing-note"), err.message);
    }
  }));
}
$("manage").addEventListener("click", () => { location.href = "/account"; });

// Stripe returns here after checkout. Credits arrive by webhook a moment
// later, so poll briefly instead of trusting the redirect.
(async () => {
  const checkout = new URLSearchParams(location.search).get("checkout");
  history.replaceState(null, "", location.pathname);
  const before = await refreshAccount();
  if (checkout !== "success" || !before) return;
  showNotice($("billing-note"), "Payment received. Adding your tailors…");
  const start = JSON.stringify(before);
  for (let i = 0; i < 10; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    const me = await refreshAccount();
    if (me && JSON.stringify(me) !== start) return;
  }
  showNotice($("billing-note"), "Payment received. Your tailors will appear shortly; refresh the page in a minute.");
})();

// ---- resume import ----

// Clear the selection on open so re-choosing the same (edited) file still fires change.
$("file").addEventListener("click", (e) => { e.target.value = ""; });

$("file").addEventListener("change", () => run($("file"), async () => {
  const file = $("file").files[0];
  if (!file) return;
  $("file-name").textContent = file.name;
  showNotice($("import-warn"));
  showNotice($("import-error"));
  $("import-log").textContent = "";
  try {
    const outcome = await postStream(
      "/api/import", { filename: file.name, data: await readBase64(file) }, $("import-log"));
    if (outcome.error) {
      return showNotice($("import-error"), outcome.error.header, outcome.error.items, outcome.error.advice);
    }
    const text = JSON.stringify(outcome.result.master, null, 2);
    $("master").value = text;
    remember(text);
    if (outcome.result.warnings.length) {
      showNotice($("import-warn"), "Review before tailoring:", outcome.result.warnings);
    }
  } catch (err) {
    showNotice($("import-error"), err.message);
  }
}));

// ---- tailor ----

function download(file) {
  const bytes = Uint8Array.from(atob(file.data), (c) => c.charCodeAt(0));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([bytes], { type: file.type }));
  a.download = file.name;
  a.textContent = `Download ${file.name}`;
  return a;
}

const progress = $("tailor-progress");
const progressFill = progress.firstElementChild;

// Null shows an indeterminate sweep; a number fills the bar to that percent.
function showProgress(percent) {
  progress.hidden = false;
  progress.classList.toggle("indeterminate", percent === null);
  progressFill.style.width = percent === null ? "" : `${percent}%`;
}

// Each attempt is one slice of the bar; halfway through a slice while it runs.
function progressFromLog(line) {
  const match = /Attempt (\d+)\/(\d+)/.exec(line);
  if (!match) return null;
  return ((Number(match[1]) - 1) + 0.5) / Number(match[2]) * 100;
}

$("tailor").addEventListener("click", () => run($("tailor"), async () => {
  showNotice($("tailor-error"));
  $("downloads").replaceChildren();
  let master;
  if (!$("master").value.trim()) {
    return showNotice($("tailor-error"), "No resume data yet. In step 1, choose your resume file (it converts automatically), or paste master-resume JSON.");
  }
  try {
    master = JSON.parse($("master").value);
  } catch (err) {
    return showNotice($("tailor-error"), "Resume data is not valid JSON.", [err.message]);
  }
  showProgress(null);
  try {
    const outcome = await postStream("/api/tailor", {
      master,
      jd: $("jd").value,
      company: $("company").value.trim(),
      rewrite: $("rewrite").checked,
      approve: $("approve").value,
    }, null, (line) => {
      const percent = progressFromLog(line);
      if (percent !== null) showProgress(percent);
    });
    if (outcome.error) {
      return showNotice($("tailor-error"), outcome.error.header, outcome.error.items,
        [outcome.error.advice, "This tailor was not charged."].filter(Boolean).join("\n\n"));
    }
    const heading = document.createElement("h4");
    heading.textContent = "On the green: your documents are ready";
    $("downloads").append(heading, ...outcome.files.map(download));
  } catch (err) {
    showNotice($("tailor-error"), err.message);
  } finally {
    progress.hidden = true;
    refreshAccount();
  }
}));
