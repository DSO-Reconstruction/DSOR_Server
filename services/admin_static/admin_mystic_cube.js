import { $, esc, api, table, confirmAction, notice, formError } from "./admin_ui.js";

function percentInput(name, value, label) {
  return `<input name="${esc(name)}" type="number" min="0" max="100" step="0.01" value="${esc(value)}" aria-label="${esc(label)}" required>`;
}

function weightInput(name, value, label, disabled = false) {
  return `<input name="${esc(name)}" type="number" min="0" max="100" step="1" value="${esc(value)}" aria-label="${esc(label)}" ${disabled ? "disabled" : ""} required>`;
}

function difficultyTable(data) {
  return table(["Difficulty", "Drop chance (%)"], data.difficulties.map(entry => [
    `${esc(entry.name)}<span class="sub">Tier ${entry.tier}</span>`,
    percentInput(`diff-${entry.tier}`, entry.drop_chance, `${entry.name} drop chance`),
  ]));
}

function weightGrid(data, prefix, weights) {
  return data.contents.keys.map(({ key, label }) =>
    `<label>${esc(label)}<small>% of opens</small>${weightInput(`${prefix}-${key}`, weights[key], `${label} weight`)}</label>`
  ).join("");
}

function tierTable(data) {
  const rows = data.contents.tiers.map(tier => [
    `${esc(tier.label)}<span class="sub">${esc(tier.template_id)}</span>`,
    `<label class="check"><input type="checkbox" name="tier-${esc(tier.template_id)}-override" ${tier.overridden ? "checked" : ""}> Override</label>`,
    ...data.contents.keys.map(({ key, label }) =>
      weightInput(
        `tier-${tier.template_id}-${key}`,
        tier.weights[key],
        `${tier.label} ${label} weight`,
        !tier.overridden,
      )),
  ]);
  return table(
    ["Cube", "Override", ...data.contents.keys.map(entry => entry.label)],
    rows,
  );
}

function renderContent(data) {
  return `<form id="mystic-cube-form" class="form-page">
    <fieldset class="form-section"><legend>Cube drop chance</legend>
      <p class="help">Each kill by a Premium or Deluxe member rolls this chance once. The cube tier follows the map difficulty. Values are percentages and apply immediately, with no restart.</p>
      <div class="form-grid"><label>Default<small>Fallback for a difficulty without its own value</small>${percentInput("drop-chance-default", data.default, "Default drop chance")}</label></div>
      ${difficultyTable(data)}
    </fieldset>
    <fieldset class="form-section"><legend>Cube contents</legend>
      <p class="help">Every cube contains exactly one reward. The default weights must total 100%.</p>
      <div class="form-grid">${weightGrid(data, "weights", data.contents.default)}</div>
      <p class="muted" id="mystic-cube-default-total">Total: 100%</p>
    </fieldset>
    <fieldset class="form-section"><legend>Per-tier content overrides</legend>
      <p class="help">Optional. An enabled tier replaces the default weights for that cube type; every enabled row must total 100%.</p>
      ${tierTable(data)}
    </fieldset>
    <p id="form-error" class="error-text" role="alert"></p>
    <div class="form-actions"><button type="submit">Save Mystic Cube settings</button></div>
  </form>`;
}

export async function mysticCubePage() {
  const data = await api("/api/mystic-cube");
  const result = {
    title: "Mystic cubes",
    description: "Tune the member drop chance and the contents of every Mystic Cube.",
    breadcrumbs: [["Administration", "status"], ["Mystic cubes"]],
    html: renderContent(data),
  };

  function bind() {
    const form = $("#mystic-cube-form");

    function weightsFrom(prefix) {
      return Object.fromEntries(data.contents.keys.map(({ key }) => [
        key, Number(form.elements[`${prefix}-${key}`].value),
      ]));
    }

    function updateTotal() {
      const total = data.contents.keys.reduce(
        (sum, { key }) => sum + (Number(form.elements[`weights-${key}`].value) || 0),
        0,
      );
      const label = $("#mystic-cube-default-total");
      label.textContent = `Total: ${total}%`;
      label.classList.toggle("error-text", total !== 100);
    }

    form.addEventListener("input", updateTotal);
    form.addEventListener("change", event => {
      const checkbox = event.target;
      if (!checkbox.matches('input[type="checkbox"][name$="-override"]')) return;
      const prefix = checkbox.name.replace(/-override$/, "");
      form.querySelectorAll(`[name^="${CSS.escape(prefix)}-"]`).forEach(input => {
        if (input !== checkbox) input.disabled = !checkbox.checked;
      });
    });
    updateTotal();

    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      const values = new FormData(form);
      const difficulties = {};
      for (const entry of data.difficulties) {
        difficulties[String(entry.tier)] = Number(values.get(`diff-${entry.tier}`));
      }
      const defaultWeights = weightsFrom("weights");
      const tiers = {};
      for (const tier of data.contents.tiers) {
        if (!form.elements[`tier-${tier.template_id}-override`].checked) continue;
        tiers[tier.template_id] = weightsFrom(`tier-${tier.template_id}`);
      }
      const total = weights => Object.values(weights).reduce((sum, value) => sum + value, 0);
      if (total(defaultWeights) !== 100) {
        return formError(new Error("The default content weights must total 100%."));
      }
      for (const [templateId, weights] of Object.entries(tiers)) {
        if (total(weights) !== 100) {
          return formError(new Error(`${templateId} weights must total 100%.`));
        }
      }
      const buttons = [...form.querySelectorAll("button")];
      buttons.forEach(button => { button.disabled = true; });
      try {
        const reason = await confirmAction(
          "Save Mystic Cube settings?",
          "New drop chances and content weights apply to subsequent drops and cube opens.",
          "Save settings",
        );
        if (reason === null) return;
        const saved = await api("/api/mystic-cube", {
          revision: data.revision,
          drop_chance: {
            default: Number(values.get("drop-chance-default")),
            difficulties,
          },
          contents: { default: defaultWeights, tiers },
          reason,
        });
        data.revision = saved.revision;
        notice("Mystic Cube settings saved. New rolls use them immediately.");
        window.dispatchEvent(new Event("reload-page"));
      } catch (error) {
        if (form.isConnected) formError(error);
      } finally {
        buttons.forEach(button => { button.disabled = false; });
      }
    });
  }

  result.mount = bind;
  return result;
}
