import { $, esc, api, link, navigate, table, section, facts, stats, toolbar,
  bindSearch, pager, formError, confirmAction, notice } from "./admin_ui.js";

const query = params => new URLSearchParams({limit: 50, ...params});
const priceFields = [
  ["price_rc", "Andermant (RC)"], ["price_vc", "Gold (VC)"],
  ["price_gc", "Glyphs (GC)"], ["price_ec", "Draken (EC)"],
  ["price_uc", "Other currency (UC)"], ["price_arena", "Honor (Arena)"],
  ["price_ic", "Shop currency (IC)"]
];
const pricesText = prices => priceFields.filter(([key]) => Number(prices[key]) > 0)
  .map(([key, label]) => `${label}: ${prices[key]}`).join(" · ") || "Free";
const priceInputs = prices => `<div class="form-grid">${priceFields.map(([key, label]) =>
  `<label>${esc(label)}<input name="${key}" type="number" min="0" max="4294967295" step="1" value="${esc(prices?.[key] ?? 0)}" required></label>`).join("")}</div>`;
const readPrices = form => Object.fromEntries(priceFields.map(([key]) => [key, Number(new FormData(form).get(key))]));

export async function npcShopsPage(route) {
  const data = await api(`/api/npc-shops?${query(route.params)}`);
  return {
    title: "NPC shops",
    description: "Inspect shop inventories, create client-supported tabs, add items, edit prices, and remove CMS offers.",
    breadcrumbs: [["Administration", "status"], ["NPC shops"]],
    html: toolbar(route, "Search by shop ID, NPC, or profession") +
      table(["Shop", "NPCs", "Static + CMS offers", "Current categories", "Selling"], data.rows.map(shop => [
        link(shop.shop_id, `npc-shops/${encodeURIComponent(shop.shop_id)}`, {}, "resource mono"),
        shop.npcs.map(npc => `<span>${esc(npc.npc_id)}${npc.profession ? ` <small>· ${esc(npc.profession)}</small>` : ""}</span>`).join("<br>"),
        esc(shop.offer_count), esc(shop.offer_categories || "—"),
        shop.player_can_sell ? "Yes" : "No"
      ]), "No NPC shops matched this search.") + pager(data, route),
    mount: () => bindSearch(route)
  };
}

export async function npcShopPage(route) {
  const shopId = decodeURIComponent(route.parts[1]);
  const data = await api(`/api/npc-shops/${encodeURIComponent(shopId)}?${query(route.params)}`);
  const shop = data.shop;
  const editOffer = route.params.edit ? data.offers.find(row => row.offer_id === route.params.edit) : null;
  const editForm = editOffer ? `<form id="shop-price-form" class="form-page">
    <h2>Edit offer price</h2><p><code>${esc(editOffer.offer_id)}</code> · ${esc(editOffer.item_name)} (${esc(editOffer.item_id)})</p>
    ${priceInputs(editOffer.prices)}<p id="form-error" class="error-text" role="alert"></p>
    <div class="form-actions"><button type="submit">Save price</button>${link("Cancel", `npc-shops/${encodeURIComponent(shopId)}`, Object.fromEntries(Object.entries(route.params).filter(([key]) => key !== "edit")), "button secondary")}</div></form>` : "";
  const categoryForm = data.available_categories.length ? `<form id="shop-category-form" class="form-page">
    <h2>Create category tab</h2><p class="muted">The game client has a fixed set of category tabs. Choose an unused supported tab for this NPC.</p>
    <label>Client category<select name="category_name" required>${data.available_categories.map(c => `<option value="${esc(c.category_name)}">${esc(c.category_name)} (${c.category_id})</option>`).join("")}</select></label>
    <p id="category-error" class="error-text" role="alert"></p><div class="form-actions"><button type="submit">Create category</button></div></form>` :
    `<p class="muted">All client-supported category tabs are already in use by this shop.</p>`;
  const categoryOptions = data.categories.map(c => `<option value="${esc(c.category_name)}">${esc(c.category_name)}</option>`).join("");
  const addForm = data.categories.length ? `<form id="shop-offer-form" class="form-page">
    <h2>Add item for sale</h2>
    <div class="form-grid"><label>Category<select name="category_name" required>${categoryOptions}</select></label>
      <label>Item template ID<input id="npc-item-id" name="item_id" required maxlength="200" autocomplete="off"></label>
      <label>Quantity per purchase<input name="amount" type="number" min="1" max="1000" step="1" value="1" required></label></div>
    <div class="toolbar"><input id="npc-item-search" aria-label="Find item" placeholder="Find item by name or template ID" maxlength="100"><button id="npc-item-find" class="secondary" type="button">Find item</button><a class="button secondary" href="#items">Open item catalog</a></div>
    <div id="npc-item-results"></div><h3>Price</h3>${priceInputs({})}
    <p id="offer-error" class="error-text" role="alert"></p><div class="form-actions"><button type="submit">Add offer</button></div></form>` : "";
  const offerRows = data.offers.map(row => [
    `<span>${esc(row.item_name)}${row.custom ? " <small>CMS</small>" : row.price_overridden ? " <small>Custom price</small>" : ""}</span>`,
    `<code class="resource mono">${esc(row.item_id)}</code><span class="sub">${esc(row.offer_id)}</span>`,
    esc(row.category_name), esc(row.amount), esc(pricesText(row.prices)),
    `${link("Edit price", `npc-shops/${encodeURIComponent(shopId)}`, {...route.params, edit: row.offer_id})}${row.custom ? ` <button type="button" class="danger" data-delete-offer="${esc(row.offer_id)}" data-item-name="${esc(row.item_name)}">Delete</button>` : ""}`
  ]);
  const categoryTable = table(["Game tab", "Category ID", "Origin"], data.categories.map(c => [
    esc(c.category_name), esc(c.category_id), c.created ? "CMS created" : "Existing"
  ]), "This shop has no categories.");
  const result = {
    title: shopId,
    description: "NPC shop inventory and pricing. Saved changes refresh players who currently have this shop open.",
    breadcrumbs: [["NPC shops", "npc-shops"], [shopId]],
    actions: link("Back to NPC shops", "npc-shops", {}, "button secondary"),
    html: stats([["NPCs", shop.npcs.length], ["Offers", data.total], ["Can sell items", shop.player_can_sell ? "Yes" : "No"]]) +
      section("NPCs using this shop", facts(shop.npcs.map(npc => [npc.npc_id, `${npc.profession || "NPC"} · ${npc.shop_loca_id || "No shop label"}`]))) +
      section("Shop categories", categoryTable) + categoryForm + addForm + editForm +
      toolbar(route, "Search offers by item, offer ID, or category") +
      table(["Item", "Template / offer", "Category", "Amount", "Price", "Action"], offerRows, "This NPC shop has no matching offers.") + pager(data, route)
  };
  result.mount = () => {
    bindSearch(route);
    $("#shop-category-form")?.addEventListener("submit", async event => {
      event.preventDefault(); const form = event.target, button = form.querySelector("button[type=submit]"); button.disabled = true;
      try {
        const values = new FormData(form);
        const reason = await confirmAction("Create this shop category?", `${values.get("category_name")} will appear as a tab in ${shopId}.`, "Create category");
        if (reason === null) return;
        const result = await api(`/api/npc-shops/${encodeURIComponent(shopId)}/categories`, {category_name: values.get("category_name"), reason});
        notice(!Object.hasOwn(result, "live_refreshed")
          ? "Category saved, but the game server needs a restart to load live shop updates."
          : result.live_refreshed
            ? `Category created; refreshed ${result.live_refreshed} open shop(s).`
            : "Category saved; it will appear next time the shop is opened.");
        window.dispatchEvent(new Event("reload-page"));
      } catch (error) { const target = $("#category-error"); if (target) target.textContent = error.message; else notice(error.message, true); }
      finally { button.disabled = false; }
    });
    $("#npc-item-find")?.addEventListener("click", async () => {
      const button = $("#npc-item-find"), queryText = $("#npc-item-search").value.trim(); button.disabled = true;
      try {
        const found = await api(`/api/items?${new URLSearchParams({q: queryText, limit: 10})}`);
        $("#npc-item-results").innerHTML = table(["Item", "Template", "Select"], found.rows.map(item => [
          esc(item.name), `<code class="resource mono">${esc(item.template_id)}</code>`,
          `<button type="button" class="secondary" data-item-id="${esc(item.template_id)}">Select</button>`
        ]), "No item templates matched.");
        for (const select of $("#npc-item-results").querySelectorAll("[data-item-id]")) select.addEventListener("click", () => {
          $("#npc-item-id").value = select.dataset.itemId;
        });
      } catch (error) { notice(error.message, true); } finally { button.disabled = false; }
    });
    $("#shop-offer-form")?.addEventListener("submit", async event => {
      event.preventDefault(); const form = event.target, button = form.querySelector("button[type=submit]"); button.disabled = true;
      try {
        const values = new FormData(form), payload = {category_name: values.get("category_name"), item_id: values.get("item_id").trim(), amount: Number(values.get("amount")), prices: readPrices(form)};
        const reason = await confirmAction("Add this NPC shop offer?", `${payload.item_id} will be added to ${payload.category_name} in ${shopId}.`, "Add offer");
        if (reason === null) return;
        const result = await api(`/api/npc-shops/${encodeURIComponent(shopId)}/offers`, {...payload, reason});
        notice(!Object.hasOwn(result, "live_refreshed")
          ? "Item saved, but the game server needs a restart to load live shop updates."
          : result.live_refreshed
            ? `Item added; refreshed ${result.live_refreshed} open shop(s).`
            : "Item saved; it will appear next time the shop is opened.");
        navigate(`npc-shops/${encodeURIComponent(shopId)}`);
      } catch (error) { const target = $("#offer-error"); if (target) target.textContent = error.message; else notice(error.message, true); }
      finally { button.disabled = false; }
    });
    $("#shop-price-form")?.addEventListener("submit", async event => {
      event.preventDefault(); const form = event.target, button = form.querySelector("button[type=submit]"); button.disabled = true;
      try {
        const prices = readPrices(form);
        const reason = await confirmAction("Update this offer price?", `${editOffer.offer_id} will use the new price immediately for players with the shop open.`, "Save price");
        if (reason === null) return;
        const result = await api(`/api/npc-shops/${encodeURIComponent(shopId)}/offers/${encodeURIComponent(editOffer.offer_id)}/price`, {prices, reason});
        notice(!Object.hasOwn(result, "live_refreshed")
          ? "Price saved, but the game server needs a restart to load live shop updates."
          : result.live_refreshed
            ? `Price updated; refreshed ${result.live_refreshed} open shop(s).`
            : "Price saved; it will apply next time the shop is opened.");
        navigate(`npc-shops/${encodeURIComponent(shopId)}`);
      } catch (error) { formError(error); } finally { button.disabled = false; }
    });
    for (const button of document.querySelectorAll("[data-delete-offer]")) {
      button.addEventListener("click", async () => {
        const offerId = button.dataset.deleteOffer;
        const itemName = button.dataset.itemName;
        const reason = await confirmAction(
          "Delete this CMS offer?",
          `${itemName} will be removed from ${shopId}. This cannot be undone.`,
          "Delete offer"
        );
        if (reason === null) return;
        button.disabled = true;
        try {
          const result = await api(
            `/api/npc-shops/${encodeURIComponent(shopId)}/offers/${encodeURIComponent(offerId)}/delete`,
            {reason}
          );
          notice(!Object.hasOwn(result, "live_refreshed")
            ? "Offer deleted, but the game server needs a restart to load the change."
            : result.live_refreshed
              ? `Offer deleted; refreshed ${result.live_refreshed} open shop(s).`
              : "Offer deleted; it will be gone next time the shop is opened.");
          navigate(`npc-shops/${encodeURIComponent(shopId)}`, route.params);
        } catch (error) {
          button.disabled = false;
          notice(error.message, true);
        }
      });
    }
  };
  return result;
}
