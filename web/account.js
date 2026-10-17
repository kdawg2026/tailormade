const $ = (id) => document.getElementById(id);

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

function say(el, text = "") { el.textContent = text; }

async function run(button, fn) {
  button.disabled = true;
  try { await fn(); } finally { button.disabled = false; }
}

function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }

function show(me) {
  $("signed-out").hidden = Boolean(me);
  $("settings").hidden = !me;
  if (!me) return;
  $("who").textContent = me.email;
  const sub = me.subscription;
  const parts = [];
  if (me.free_left) parts.push(plural(me.free_left, "free tailor"));
  if (sub.active) parts.push(`${sub.left} of this period's monthly tailors`);
  if (me.pack_credits) parts.push(plural(me.pack_credits, "pack tailor"));
  say($("balance"), parts.length ? `You have ${parts.join(" + ")} left.` : "You're out of tailors.");

  const subscribed = sub.active || ["past_due", "unpaid"].includes(sub.status);
  const until = sub.renews ? new Date(sub.renews).toLocaleDateString() : "";
  say($("sub-line"), !subscribed ? "No monthly plan."
    : sub.cancel_at_period_end ? `Monthly plan ends ${until}. You keep its tailors until then.`
    : sub.status === "past_due" ? "Your last monthly payment failed. Update your payment method."
    : `Monthly plan renews ${until} at $9.`);
  $("cancel-sub").hidden = !subscribed || sub.cancel_at_period_end;
  $("resume-sub").hidden = !subscribed || !sub.cancel_at_period_end;
  $("billing").hidden = !me.has_billing_account;
}

async function refresh() {
  const response = await fetch("/api/me");
  show(response.ok ? await response.json() : null);
}
refresh();

$("cancel-sub").addEventListener("click", () => run($("cancel-sub"), async () => {
  say($("plan-error"));
  if (!confirm("Cancel the monthly plan? It stays active until the end of the period you've paid for.")) return;
  try { show(await postJson("/api/subscription/cancel")); } catch (err) { say($("plan-error"), err.message); }
}));

$("resume-sub").addEventListener("click", () => run($("resume-sub"), async () => {
  say($("plan-error"));
  try { show(await postJson("/api/subscription/resume")); } catch (err) { say($("plan-error"), err.message); }
}));

$("billing").addEventListener("click", () => run($("billing"), async () => {
  say($("plan-error"));
  try { location.href = (await postJson("/api/portal")).url; } catch (err) { say($("plan-error"), err.message); }
}));

$("password-form").addEventListener("submit", (e) => {
  e.preventDefault();
  run($("save-password"), async () => {
    say($("password-error"));
    say($("password-ok"));
    try {
      await postJson("/api/password", { current: $("current").value, new: $("new").value });
      $("password-form").reset();
      say($("password-ok"), "Password changed. Other devices have been signed out.");
    } catch (err) {
      say($("password-error"), err.message);
    }
  });
});

$("delete-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if (!confirm("Delete your account permanently? This cannot be undone.")) return;
  run($("delete"), async () => {
    say($("delete-error"));
    try {
      await postJson("/api/account/delete", { password: $("delete-password").value });
      try { localStorage.removeItem("tailormade.master"); } catch {}
      location.href = "/";
    } catch (err) {
      say($("delete-error"), err.message);
    }
  });
});
