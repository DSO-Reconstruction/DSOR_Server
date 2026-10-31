import { $, esc, api, section, table, confirmAction, notice, formError } from "./admin_ui.js";

const categories = [
  ["map_reward", "Listed map items"],
  ["unique_set", "Map and region Unique/Set items"],
  ["gem", "Gem"],
  ["rune", "Rune (elite mobs)"],
  ["ingredient", "Basic ingredients"],
  ["elite_ingredient", "Elite ingredients"],
  ["jewel", "Normal Jewel (Parallel World chest)"],
];

function percentInput(name, value, placeholder = "") {
  const shown = value == null ? "" : value;
  return `<input name="${esc(name)}" type="number" min="0" max="100" step="0.1" value="${esc(shown)}" placeholder="${esc(placeholder)}" ${placeholder ? "" : "required"}>`;
}

function mapResults(data) {
  if (!data.query) return '<p class="muted">Search by map name or access ID to edit a map/difficulty override.</p>';
  const rows = data.maps.map(map => [
    `<code class="resource">${esc(map.map_id || map.access_group)}</code>`,
    `Difficulty ${esc(map.difficulty)}`,
    `<code class="resource">${esc(map.access_id)}</code>`,
    `<button type="button" class="secondary" data-map-access-id="${esc(map.access_id)}">Edit</button>`,
  ]);
  return table(["Map", "Difficulty", "Access row", ""], rows,
    "No map reward rows matched this search.") +
    `<p class="muted">${data.total > data.maps.length ? `Showing ${data.maps.length} of ${data.total}. Add more characters to narrow the search.` : `${data.total} matching difficulty row(s).`}</p>`;
}

function renderContent(data) {
  const defaults = categories.map(([key, label]) => [
    `<label>${esc(label)}<small>Global default (%)</small>${percentInput(`default-${key}`, data.defaults[key])}</label>`,
  ]);
  const selected = data.selected;
  const overrides = categories.map(([key, label]) => [
    `<label>${esc(label)}<small>Blank uses the global default (${Number(data.defaults[key]).toFixed(1)}%)</small>${percentInput(`override-${key}`, data.selected_overrides[key] ?? "", "Inherit")}</label>`,
  ]);
  const selectedLabel = selected
    ? `${selected.map_id || selected.access_group} · Difficulty ${selected.difficulty} · ${selected.access_id}`
    : "No map difficulty selected";
  return section("Search map and difficulty", `<form id="loot-map-search" class="toolbar">
    <input name="q" aria-label="Search maps" placeholder="Map name or access ID (e.g. pw002)" maxlength="100" value="${esc(data.query || "")}">
    <button type="submit" class="secondary">Search</button>
  </form><div id="loot-map-results">${mapResults(data)}</div>`) +
    `<form id="loot-rates-form" class="form-page">
      <fieldset class="form-section"><legend>Global default rates</legend>
        <p class="help">These rates apply to all maps unless that map/difficulty has an override. The map's difficulty drop modifiers still scale the selected rate. Set 0% to disable a reward category.</p>
        <div class="form-grid">${defaults.map(row => row[0]).join("")}</div>
      </fieldset>
      <fieldset class="form-section"><legend>Map and difficulty override</legend>
        <p class="muted">${esc(selectedLabel)}</p>
        ${selected ? `<p class="help">Enter a percentage to override only this access row. Leave a field blank to inherit the global default.</p><div class="form-grid">${overrides.map(row => row[0]).join("")}</div>` : '<p class="help">Choose a result above to edit this map difficulty. Global defaults can be saved without selecting a map.</p>'}
      </fieldset>
      <p id="form-error" class="error-text" role="alert"></p>
      <div class="form-actions"><button type="submit">Save loot rates</button></div>
    </form>`;
}

export async function lootRatesPage() {
  const data = await api("/api/loot-rates");
  data.query = "";
  const result = {
    title: "Loot rates",
    description: "Tune Possible Rewards chances globally or per map difficulty.",
    breadcrumbs: [["Administration", "status"], ["Loot rates"]],
    html: "",
  };

  function draw() {
    $("#content").innerHTML = renderContent(data);
    bind();
  }

  async function searchMaps(query) {
    const params = new URLSearchParams({q: query});
    if (data.selected?.access_id) params.set("access_id", data.selected.access_id);
    const fresh = await api(`/api/loot-rates?${params}`);
    data.revision = fresh.revision;
    data.defaults = fresh.defaults;
    data.maps = fresh.maps;
    data.total = fresh.total;
    data.selected = fresh.selected;
    data.selected_overrides = fresh.selected_overrides;
    data.query = query;
    draw();
  }

  function bind() {
    $("#loot-map-search").addEventListener("submit", async event => {
      event.preventDefault();
      const button = event.target.querySelector("button"); button.disabled = true;
      try { await searchMaps(String(new FormData(event.target).get("q") || "").trim()); }
      catch (error) { notice(error.message, true); }
      finally { button.disabled = false; }
    });
    document.querySelectorAll("[data-map-access-id]").forEach(button => button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const params = new URLSearchParams({access_id: button.dataset.mapAccessId});
        if (data.query) params.set("q", data.query);
        const fresh = await api(`/api/loot-rates?${params}`);
        data.revision = fresh.revision;
        data.defaults = fresh.defaults;
        data.selected = fresh.selected;
        data.selected_overrides = fresh.selected_overrides;
        draw();
      } catch (error) { notice(error.message, true); }
    }));
    $("#loot-rates-form").addEventListener("submit", async event => {
      event.preventDefault();
      const form = event.target;
      if (!form.reportValidity()) return;
      const values = new FormData(form);
      const defaults = Object.fromEntries(categories.map(([key]) => [
        key, Number(values.get(`default-${key}`)),
      ]));
      const accessRates = data.selected ? Object.fromEntries(categories.map(([key]) => {
        const value = String(values.get(`override-${key}`) ?? "").trim();
        return [key, value === "" ? null : Number(value)];
      })) : {};
      const buttons = [...form.querySelectorAll("button")]; buttons.forEach(button => button.disabled = true);
      try {
        const reason = await confirmAction("Save loot rates?", "The new rates apply to subsequent monster and chest loot rolls. Difficulty modifiers continue to scale them.", "Save rates");
        if (reason === null) return;
        const saved = await api("/api/loot-rates", {
          revision: data.revision,
          defaults,
          access_id: data.selected?.access_id || "",
          access_rates: accessRates,
          reason,
        });
        data.revision = saved.revision;
        notice("Loot rates saved. New rates apply immediately.");
        window.dispatchEvent(new Event("reload-page"));
      } catch (error) { if (form.isConnected) formError(error); }
      finally { buttons.forEach(button => button.disabled = false); }
    });
  }

  result.html = renderContent(data);
  result.mount = bind;
  return result;
}
