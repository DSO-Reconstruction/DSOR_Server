import { $, esc, api, section, confirmAction, notice, isLegacyInterface, saveInterfacePreference } from "./admin_ui.js";

export function settingsPage() {
  const classic = isLegacyInterface();
  const option = (mode, title, description, selected) => `<label class="interface-option">
    <input type="radio" name="cms-interface" value="${mode}" ${selected ? "checked" : ""}>
    <strong>${title}</strong>
    <span class="interface-preview interface-preview-${mode}" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
    <small>${description}</small>
  </label>`;
  return {
    title: "CMS settings",
    description: "Choose how the administration console is presented on this browser.",
    breadcrumbs: [["Administration", "status"], ["CMS settings"]],
    html: section("Interface", `<fieldset class="interface-choices">
      <legend>Layout for this browser</legend>
      ${option("modern", "Realm console", "Operations overview and dedicated workspaces.", !classic)}
      ${option("legacy", "Classic CMS", "Compact navigation and table-based directories.", classic)}
    </fieldset><div class="form-actions"><button id="save-interface">Save interface preference</button></div>
    <p id="settings-result" class="help" role="status"></p>`),
    mount: () => bindServerControls()
  };
}

function countdownLabel(seconds) {
  if (seconds == null) return "Not scheduled";
  const whole = Math.max(0, Math.ceil(seconds));
  if (!whole) return "Countdown ended";
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")} remaining`;
}

export async function serverControlPage() {
  const data = await api("/api/server-control");
  const serviceRows = data.services.map(service => `<tr><td>${esc(service.name)}</td><td>${esc(service.port ?? "—")}</td><td><span class="status ${service.running ? "good" : "bad"}">${service.running ? "Running" : "Stopped"}</span></td><td>${esc(service.connections ?? "—")}</td></tr>`).join("");
  const unavailable = !data.available && !data.can_start;
  const disabled = unavailable ? "disabled" : "";
  const maintenance = data.maintenance?.remaining_seconds;
  const deadline = maintenance == null ? null : performance.now() + maintenance * 1000;
  const maintenanceLabel = `<span data-maintenance-countdown>${esc(countdownLabel(maintenance))}</span>`;
  const operations = data.can_start
    ? section("Game server", `<article class="control-card start-card"><h3>Start game server</h3><p>Start game services in this CMS process. The CMS remains available and returns to manager mode after the server stops.</p>
        <div class="form-actions"><button id="start-server">Start game server</button></div></article>`)
    : section("Operations", `<div class="control-grid">
        <article class="control-card"><h3>Maintenance countdown</h3><p>Notify active players. At zero, game services stop and this CMS returns to manager mode.</p>
          <label>Countdown in minutes<input id="maintenance-minutes" type="number" min="1" max="1440" value="5" ${disabled}></label>
          <div class="form-actions"><button id="send-maintenance" ${disabled}>Send maintenance signal</button></div></article>
        <article class="control-card"><h3>Restart server</h3><p>Send the countdown, stop cleanly, then relaunch the game services.</p>
          <label>Countdown in minutes<input id="restart-minutes" type="number" min="0" max="1440" value="5" ${disabled}></label>
          <div class="form-actions"><button id="restart-server" ${disabled}>Schedule restart</button></div></article>
        <article class="control-card"><h3>Stop server</h3><p>Stop after a zero-minute signal. The CMS returns in manager mode.</p>
          <div class="form-actions"><button id="stop-server" class="secondary" ${disabled}>Stop server</button></div></article>
      </div>`);
  const serverState = data.available ? "Game server is running" : data.can_start ? "Game server is stopped" : "Game server unavailable";
  const stateTone = data.available ? "good" : unavailable ? "bad" : "";
  const live = `<span class="status ${stateTone}">${serverState}</span>`;
  const warning = unavailable ? '<p class="control-warning">This CMS process is not running as the game server manager. Launch it with the CMS launcher to control the game server here.</p>' : "";
  const shutdown = data.available ? section("Immediate shutdown", `<div class="control-warning">This stops the game server without a maintenance notice. The CMS returns to manager mode; active players may lose unsaved progress.</div>
    <div class="form-actions"><button id="shutdown-server" class="danger" ${disabled}>Shut down now</button></div>`) : "";
  const services = section("Running services", `<div class="table-wrap"><table><thead><tr><th>Service</th><th>Port</th><th>State</th><th>Connections</th></tr></thead><tbody>${serviceRows}</tbody></table></div>`);
  const classic = `<div class="control-live">${live}<span class="muted">${maintenanceLabel}</span></div>${warning}${operations}${shutdown}${services}`;
  const modern = `<div class="server-workspace">
    <div class="server-workspace-main">${warning}${operations}${services}</div>
    <aside aria-label="Server status and emergency controls">
      ${section("Current state", `<div class="server-state">${live}<dl><dt>Maintenance countdown</dt><dd>${maintenanceLabel}</dd><dt>Services running</dt><dd>${data.services.filter(service => service.running).length} / ${data.services.length}</dd></dl></div>`)}
      ${shutdown}
    </aside>
  </div>`;
  return {
    title: "Server control",
    description: "Manage the running game server and send a maintenance countdown to players.",
    breadcrumbs: [["Administration", "status"], ["Server control"]],
    html: isLegacyInterface() ? classic : modern,
    mount() {
      bindServerControls();
      if (deadline == null) return;
      const countdown = $("[data-maintenance-countdown]");
      const tick = () => {
        if (!countdown.isConnected) return;
        const remaining = (deadline - performance.now()) / 1000;
        countdown.textContent = countdownLabel(remaining);
        if (remaining > 0) setTimeout(tick, 250);
      };
      tick();
    }
  };
}

function bindServerControls() {
  const saveButton = $("#save-interface");
  saveButton?.addEventListener("click", () => {
    const mode = $('input[name="cms-interface"]:checked').value;
    try { saveInterfacePreference(mode); } catch {
      $("#settings-result").textContent = "This browser could not save the preference.";
      return;
    }
    location.reload();
  });

  const bind = (selector, action, title, message, label, payload = () => ({})) => {
    $(selector)?.addEventListener("click", async () => {
      const reason = await confirmAction(title, message, label);
      if (!reason) return;
      try {
        await api(`/api/server-control/${action}`, {...payload(), reason});
        notice(action === "maintenance" ? "Maintenance countdown scheduled." : "Server request accepted. The CMS may reconnect in manager mode.");
        if (action === "maintenance" || action === "restart") {
          window.dispatchEvent(new Event("reload-page"));
        } else if (["start", "stop", "shutdown"].includes(action)) {
          waitForManagerTransition(action, location.hash);
        }
      } catch (error) { notice(error.message, true); }
    });
  };
  bind("#start-server", "start", "Start the game server",
    "The CMS will switch to the full game server process. After the server stops, the CMS returns to manager mode.", "Start server");
  bind("#send-maintenance", "maintenance", "Send maintenance signal",
    "Players will see the countdown. Game services stop at zero and the CMS returns to manager mode.", "Send signal",
    () => ({minutes: Number($("#maintenance-minutes").value)}));
  bind("#restart-server", "restart", "Schedule server restart",
    "The server will broadcast a maintenance countdown, stop cleanly, and relaunch the game services when it ends.", "Schedule restart",
    () => ({minutes: Number($("#restart-minutes").value)}));
  bind("#stop-server", "stop", "Stop the game server",
    "Players will receive a zero-minute maintenance signal. Game services stop and the CMS returns to manager mode.", "Stop server");
  bind("#shutdown-server", "shutdown", "Shut down the server now",
    "Game services stop immediately without notifying players. The CMS returns in manager mode. Active players may lose unsaved progress.", "Shut down", () => ({}));
}

async function waitForManagerTransition(action, routeHash) {
  for (let attempt = 0; attempt < 120; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 1000));
    if (location.hash !== routeHash) return;
    try {
      const response = await fetch("/api/server-control", {
        credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(2500)
      });
      if (location.hash !== routeHash) return;
      if (response.status === 401) { location.reload(); return; }
      if (!response.ok) continue;
      const state = await response.json();
      if (action === "start" ? state.available : state.can_start) {
        window.dispatchEvent(new Event("reload-page"));
        return;
      }
    } catch { /* The process is switching; retry when the CMS is back. */ }
  }
}
