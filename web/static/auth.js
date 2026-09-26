"use strict";

// Shared handling for authentication failures returned by protected JSON APIs.
function safeAccountsUrl(candidate) {
  try {
    const url = new URL(candidate, location.origin);
    return url.protocol === "https:" &&
      url.hostname === "accounts.arboretuminvestments.net" ? url.href : null;
  } catch (_) {
    return null;
  }
}

function hydrateAccountHeader(session) {
  const identity = document.getElementById("customer-identity");
  const email = document.getElementById("customer-email");
  const plan = document.getElementById("customer-plan");
  if (identity && email && plan) {
    email.textContent = session.email;
    plan.textContent = session.plan;
    identity.hidden = false;
  }

  const account = document.getElementById("account-link");
  const accountUrl = safeAccountsUrl(session.account_url);
  if (account && accountUrl) account.href = accountUrl;

}

function renderAuthProblem(target, status, data) {
  if (!target) return;
  target.replaceChildren();
  const box = document.createElement("div");
  box.className = "auth-problem";
  const title = document.createElement("strong");
  const detail = document.createElement("p");
  if (status === 403) {
    title.textContent = "Valence requires Max";
    detail.textContent = "Your Arboretum account does not currently include Valence.";
    const href = safeAccountsUrl(data.upgrade_url);
    if (href) {
      const link = document.createElement("a");
      link.href = href;
      link.textContent = "Manage account";
      box.append(title, detail, link);
    } else {
      box.append(title, detail);
    }
  } else {
    title.textContent = "Authentication temporarily unavailable";
    detail.textContent = "Valence could not verify your session. Please try again shortly.";
    box.append(title, detail);
  }
  target.append(box);
}

async function authFetch(input, init, target) {
  const response = await fetch(input, init);
  if (![401, 403, 503].includes(response.status)) return response;

  let data = {};
  try { data = await response.clone().json(); } catch (_) { /* fixed copy below */ }

  if (response.status === 401) {
    const destination = safeAccountsUrl(data.login_url || response.headers.get("Location"));
    if (destination) location.assign(destination);
    else renderAuthProblem(target, 503, {});
    return null;
  }

  renderAuthProblem(target, response.status, data);
  return null;
}
