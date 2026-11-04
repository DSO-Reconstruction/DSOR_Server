import { $, esc, api, section, table, debounce, confirmAction, notice, formError, mapWorkspace } from "./admin_ui.js";

const MAX_TARGET = 200;

function numberInput(name, value, label) {
  const shown = value == null ? "" : value;
  return `<input name="${esc(name)}" type="number" min="0" max="${MAX_TARGET}" step="1" value="${esc(shown)}" aria-label="${esc(label)}" required>`;
}

function difficultyTable(data) {
  const rows = data.difficulties.map(d => [
    `${esc(d.name)}<span class="sub">Tier ${d.tier}</span>`,
    numberInput(`diff-${d.tier}-leaders`, d.leaders, `${d.name} leaders`),
    numberInput(`diff-${d.tier}-champions`, d.champions, `${d.name} champions`),
    numberInput(`diff-${d.tier}-guardians`, d.guardians, `${d.name} guardians`),
  ]);
  return table(["Difficulty", "Leaders", "Champions", "Guardians"], rows);
}

function mapResults(data) {
  if (!data.query) return '<p class="muted">Search by map ID or type to edit one map.</p>';
  const rows = data.maps.map(map => [
    `<code class="resource">${esc(map.map_id)}</code>`,
    esc(map.type || "—"),
    `<button type="button" class="secondary" data-map-id="${esc(map.map_id)}" data-map-type="${esc(map.type || "")}">Edit</button>`,
  ]);
  return table(["Map", "Type", ""], rows, "No maps matched this search.") +
    `<p class="muted">${data.total > data.maps.length
      ? `Showing ${data.maps.length} of ${data.total}. Add more characters to narrow the search.`
      : `${data.total} map(s).`}</p>`;
}

function mapEditor(data, fallback) {
  if (!data.selected) {
    return '<p class="help">No map selected. The difficulty table applies to every map.</p>';
  }
  if (data.remove_targets) {
    return `<div class="selection"><div><strong>${esc(data.selected)}</strong><span class="muted">${esc(data.selected_type || "")}</span></div>
        <button type="button" class="secondary" id="elite-keep-map">Keep override</button></div>
      <p class="help">This map's override will be removed when you save, so it inherits the difficulty table again.</p>`;
  }
  const stored = data.selected_targets || {};
  const value = key => stored[key] ?? fallback[key];
  return `<div class="selection"><div><strong>${esc(data.selected)}</strong><span class="muted">${esc(data.selected_type || "")}</span></div>
      <button type="button" class="secondary" id="elite-clear-map">Remove override</button></div>
    <p class="help">These counts replace the difficulty table for this map at every difficulty. Remove the override to inherit the table again.</p>
    <div class="form-grid">
      <label>Leaders${numberInput("map-leaders", value("leaders"), "Map leaders")}</label>
      <label>Champions${numberInput("map-champions", value("champions"), "Map champions")}</label>
      <label>Guardians${numberInput("map-guardians", value("guardians"), "Map guardians")}</label>
    </div>`;
}

export async function dungeonElitesPage() {
  const data = await api("/api/dungeon-elites");
  data.query = "";
  data.selected = "";
  data.selected_type = "";
  data.selected_targets = {};
  data.remove_targets = false;
  data.maps = [];
  data.total = 0;

  const result = {
    title: "Dungeon elites",
    description: "Set how many Guardians, Champions, and Leaders a dungeon instance contains.",
    breadcrumbs: [["Administration", "status"], ["Dungeon elites"]],
    html: "",
  };

  // The map selection fallback: the tier 0 counts the administrator can see.
  function tierZeroFromForm() {
    const value = name => {
      const input = document.querySelector(`[name="${name}"]`);
      return input ? Math.max(0, Math.floor(Number(input.value) || 0)) : 0;
    };
    return {
      leaders: value("diff-0-leaders"),
      champions: value("diff-0-champions"),
      guardians: value("diff-0-guardians"),
    };
  }

  function drawEditor() {
    $("#elite-map-editor").innerHTML = mapEditor(data, tierZeroFromForm());
    $("#elite-clear-map")?.addEventListener("click", () => {
      data.remove_targets = true;
      drawEditor();
    });
    $("#elite-keep-map")?.addEventListener("click", () => {
      data.remove_targets = false;
      drawEditor();
    });
  }

  function renderContent() {
    return mapWorkspace(section("Search map", `<form id="elite-map-search" class="toolbar" role="search">
        <input name="q" type="search" aria-label="Search maps" placeholder="Map ID or type (e.g. c2301, Standard)" maxlength="100" autocomplete="off">
        <button type="submit" class="secondary">Search</button>
      </form><div id="elite-map-results">${mapResults(data)}</div>`),
      `<form id="dungeon-elites-form" class="form-page">
        <fieldset class="form-section"><legend>Counts by difficulty</legend>
          <p class="help">Each dungeon instance holds exactly these counts per elite rank. Extra authored elites are demoted to ordinary monsters and missing ones are promoted from the normal pool. Set 0 to remove a rank from dungeons.</p>
          ${difficultyTable(data)}
          <label class="check"><input type="checkbox" name="include_event_maps" ${data.include_event_maps ? "checked" : ""}> Apply these counts to event maps</label>
          <p class="help">Event maps author their encounters as ranked waves, so they keep their own composition unless this is enabled.</p>
        </fieldset>
        <fieldset class="form-section"><legend>Map override</legend>
          <div id="elite-map-editor"></div>
        </fieldset>
        <p id="form-error" class="error-text" role="alert"></p>
        <div class="form-actions"><button type="submit">Save elite counts</button></div>
      </form>`);
  }

  async function searchMaps(query) {
    const params = new URLSearchParams({ q: query });
    if (data.selected) params.set("map_id", data.selected);
    const fresh = await api(`/api/dungeon-elites?${params}`);
    data.revision = fresh.revision;
    data.maps = fresh.maps;
    data.total = fresh.total;
    data.query = query;
    $("#elite-map-results").innerHTML = mapResults(data);
    bindMapResults();
  }

  function bindMapResults() {
    document.querySelectorAll("[data-map-id]").forEach(button => button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const params = new URLSearchParams({ map_id: button.dataset.mapId });
        if (data.query) params.set("q", data.query);
        const fresh = await api(`/api/dungeon-elites?${params}`);
        data.revision = fresh.revision;
        data.selected = fresh.selected;
        data.selected_type = button.dataset.mapType || "";
        data.selected_targets = fresh.selected_targets || {};
        data.remove_targets = false;
        drawEditor();
      } catch (error) { notice(error.message, true); }
      finally { button.disabled = false; }
    }));
  }

  function bind() {
    const searchForm = $("#elite-map-search");
    const search = async () => {
      const button = searchForm.querySelector("button");
      button.disabled = true;
      try { await searchMaps(String(new FormData(searchForm).get("q") || "").trim()); }
      catch (error) { notice(error.message, true); }
      finally { button.disabled = false; }
    };
    searchForm.addEventListener("submit", event => { event.preventDefault(); search(); });
    searchForm.elements.q.addEventListener("input", debounce(search));
    bindMapResults();

    $("#dungeon-elites-form").addEventListener("submit", async event => {
      event.preventDefault();
      const form = event.target;
      if (!form.reportValidity()) return;
      const values = new FormData(form);
      const difficulties = {};
      for (const entry of data.difficulties) {
        difficulties[String(entry.tier)] = {
          leaders: Number(values.get(`diff-${entry.tier}-leaders`)),
          champions: Number(values.get(`diff-${entry.tier}-champions`)),
          guardians: Number(values.get(`diff-${entry.tier}-guardians`)),
        };
      }
      const mapTargets = data.selected && !data.remove_targets ? {
        leaders: Number(values.get("map-leaders")),
        champions: Number(values.get("map-champions")),
        guardians: Number(values.get("map-guardians")),
      } : null;
      const buttons = [...form.querySelectorAll("button")];
      buttons.forEach(button => button.disabled = true);
      try {
        const reason = await confirmAction(
          "Save dungeon elite counts?",
          "New counts apply the next time a dungeon instance is generated. Already generated maps keep their population until they are rebuilt.",
          "Save counts",
        );
        if (reason === null) return;
        const saved = await api("/api/dungeon-elites", {
          revision: data.revision,
          difficulties,
          include_event_maps: form.elements.include_event_maps.checked,
          map_id: data.selected || "",
          map_targets: mapTargets,
          reason,
        });
        data.revision = saved.revision;
        notice("Dungeon elite counts saved.");
        window.dispatchEvent(new Event("reload-page"));
      } catch (error) { if (form.isConnected) formError(error); }
      finally { buttons.forEach(button => button.disabled = false); }
    });
  }

  result.html = renderContent();
  result.mount = () => { bind(); drawEditor(); };
  return result;
}
