import { $, esc, api, table, facts, section, formError, notice, confirmAction } from "./admin_ui.js";

export async function livePage(result, id) {
  const live = await api(`/api/characters/${id}/live`);
  result.html += `<p class="help">${live.ready ? `Current map: <code>${esc(live.map_id)}</code>. Status effects and map transfers apply immediately.` : "The character must be online and fully loaded into a map to use these actions."}</p>`;
  result.html += `<form id="live-form" class="form-page">
    ${section("Admin status effects", '<p class="muted">These effects persist across map changes and logins until disabled. Existing copies are not stacked.</p>' +
      table(["Effect", "Current state", ""], live.effects.map(effect => [
        `<strong>${esc(effect.label)}</strong><span class="sub">${esc(effect.description)}</span>`,
        effect.enabled ? "Enabled" : "Disabled",
        `<button type="button" class="secondary" data-effect="${esc(effect.id)}" data-enabled="${!effect.enabled}" ${!live.ready || !effect.available ? "disabled" : ""}>${effect.enabled ? "Disable" : "Enable"}</button>`])))}
    ${section("Map access", `<p class="muted">Unlock all travel destinations and quest-gated map access for this character. The grant persists; change maps or relog to refresh the travel menu. Event availability still applies.</p>
      <button id="unlock-maps" type="button" class="secondary" ${!live.ready || live.all_maps_unlocked ? "disabled" : ""}>${live.all_maps_unlocked ? "All maps unlocked" : "Unlock all maps"}</button>`)}
    ${section("Change map", `<p class="muted">Force a transfer to the selected map's normal entry point. This bypasses travel costs and event access restrictions.</p>
      <label for="map-query">Find destination</label><div class="toolbar"><input id="map-query" placeholder="Map ID or type" maxlength="100" ${!live.ready ? "disabled" : ""}><button id="find-map" type="button" class="secondary" ${!live.ready ? "disabled" : ""}>Search maps</button></div>
      <div id="map-results"></div><div id="map-selection" class="selection" hidden></div>
      <div class="form-grid"><label>Scaling difficulty<input name="scaling_difficulty" type="number" min="0" max="100" step="1" value="0" ${!live.ready ? "disabled" : ""}><small>0 uses normal scaling. Map access variants also carry their own difficulty.</small></label></div>`)}
    <p class="help">Every action asks for a reason, recorded with the administrator and character in the audit log.</p>
    <p id="form-error" class="error-text" role="alert"></p>
    <div class="form-actions"><button id="refresh-live" type="button" class="secondary">Refresh status</button><button id="teleport" type="submit" disabled>Change map</button></div>
  </form>`;
  result.mount = () => {
    const form = $("#live-form"); let destination = null, generation = 0, busy = false;
    $("#refresh-live").addEventListener("click", () => window.dispatchEvent(new Event("reload-page")));
    async function findMaps() {
      const current = ++generation, button = $("#find-map"); button.disabled = true;
      try {
        const q = encodeURIComponent($("#map-query").value), maps = {rows: [], total: 1};
        while (maps.rows.length < maps.total) {
          const part = await api(`/api/maps?limit=100&offset=${maps.rows.length}&q=${q}`);
          if (!form.isConnected || generation !== current) return;
          maps.rows.push(...part.rows); maps.total = part.rows.length ? part.total : maps.rows.length;
        }
        $("#map-results").innerHTML = table(["Destination", "Type", "Map difficulty", ""], maps.rows.map(map => [
          `<code>${esc(map.map_id)}</code><span class="sub">${esc(map.physical_map)}</span>`, esc(map.type), map.difficulty,
          `<button type="button" class="secondary" data-map="${esc(map.map_id)}">Select</button>`
        ]), "No installed maps match this search.");
        for (const button of $("#map-results").querySelectorAll("[data-map]")) button.addEventListener("click", () => {
          destination = maps.rows.find(map => map.map_id === button.dataset.map);
          $("#map-selection").hidden = false;
          $("#map-selection").innerHTML = facts([["Destination", `<code>${esc(destination.map_id)}</code>`], ["Physical map", esc(destination.physical_map)], ["Map difficulty", destination.difficulty]]);
          $("#teleport").disabled = busy;
        });
      } catch (error) { if (form.isConnected) formError(error); }
      finally { if (form.isConnected) button.disabled = false; }
    }
    $("#find-map").addEventListener("click", findMaps);
    if (live.ready) findMaps();
    $("#map-query").addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); findMaps(); } });
    async function act(action, payload, title, message, success) {
      if (busy || !form.reportValidity()) return;
      busy = true;
      const buttons = [...form.querySelectorAll("button")], disabled = buttons.map(button => button.disabled);
      buttons.forEach(button => { button.disabled = true; });
      try {
        const reason = await confirmAction(title, message, "Apply");
        if (reason === null) return;
        await api(`/api/characters/${id}/${action}`, {...payload, reason});
        notice(success); window.dispatchEvent(new Event("reload-page"));
      } catch (error) { if (form.isConnected) formError(error); }
      finally { busy = false; buttons.forEach((button, index) => { button.disabled = disabled[index]; }); }
    }
    for (const button of form.querySelectorAll("[data-effect]")) button.addEventListener("click", () => {
      const effect = live.effects.find(effect => effect.id === button.dataset.effect), enabled = button.dataset.enabled === "true";
      act("effect", {effect: effect.id, enabled}, `${enabled ? "Enable" : "Disable"} effect?`, effect.label, `${effect.label} ${enabled ? "enabled" : "disabled"}.`);
    });
    $("#unlock-maps").addEventListener("click", () => {
      act("unlock_maps", {}, "Unlock all maps?", "Grant permanent map access to this character.",
        "All maps unlocked. Change maps or relog to refresh the travel menu.");
    });
    form.addEventListener("submit", event => {
      event.preventDefault(); if (!destination) return;
      act("teleport", {map_id: destination.map_id, scaling_difficulty: Number(form.elements.scaling_difficulty.value)},
        "Change this character's map?", `Transfer to ${destination.map_id} now.`, "Map transfer started.");
    });
  };
  return result;
}
