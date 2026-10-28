const statusNames = {
  all: "Tutti i giochi",
  playing: "In corso",
  wishlist: "Da giocare",
  completed: "Completati",
  on_hold: "In pausa",
  dropped: "Abbandonati",
};
const catalogDateLimits = [0, 365, 180, 90, 30, 14, 7, 3, 1];

function catalogDateLabel(days) {
  if (!days) return "SEMPRE";
  if (days === 1) return "OGGI";
  return `< ${days} GIORNI`;
}

const state = {
  status: "all",
  search: "",
  sort: "name",
  direction: "asc",
  filters: {
    include: { game_statuses: [], engines: [], tags: [], personal_statuses: [] },
    exclude: { game_statuses: [], engines: [], tags: [], personal_statuses: [] },
  },
  pageView: "library",
  catalog: {
    search: "",
    page: 1,
    totalPages: 1,
    totalGames: 0,
    sort: "date",
    filters: {
      creator: "",
      searchMode: "creator",
      titleSearch: "",
      ignored: false,
      date: 0,
      prefixes: [],
      excludePrefixes: [],
      engines: [],
      excludeEngines: [],
      statuses: [],
      excludeStatuses: [],
      tags: [],
      excludeTags: [],
      prefixType: "and",
      tagType: "or",
    },
    games: [],
  },
  games: [],
  activeGame: null,
};
const grid = document.querySelector("#game-grid");
const emptyState = document.querySelector("#empty-state");
const notice = document.querySelector("#notice");
const addModal = document.querySelector("#add-modal");
const settingsModal = document.querySelector("#settings-modal");
const detailModal = document.querySelector("#detail-modal");
const metadataModal = document.querySelector("#metadata-modal");
const filtersModal = document.querySelector("#filters-modal");
const catalogFiltersModal = document.querySelector("#catalog-filters-modal");
let searchTimer;
let catalogSearchTimer;
let syncStatusWasActive = false;
let syncStatusPollFailed = false;
let syncStatusPollInFlight = false;
let catalogSyncPending = false;
let syncNoticeTimeout;
let draftFilters = null;
let availableFilterOptions = null;
let catalogFilterOptions = null;
let catalogDraftFilters = null;
const filterCategories = {
  game_statuses: {
    label: "Stato F95Zone",
    searchId: "#library-game-status-search",
    listId: "#library-game-status-list",
    countId: "#library-game-status-count",
  },
  engines: {
    label: "Engine",
    searchId: "#library-engine-search",
    listId: "#library-engine-list",
    countId: "#library-engine-count",
  },
  tags: {
    label: "Tag",
    searchId: "#filter-tag-search",
    listId: "#library-tag-list",
    countId: "#library-tag-count",
  },
  personal_statuses: {
    label: "Stato della libreria",
    listId: "#library-personal-status-list",
    countId: "#library-personal-status-count",
  },
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Errore HTTP ${response.status}`);
  return payload;
}

function showNotice(message, error = false, type = "message") {
  clearTimeout(syncNoticeTimeout);
  syncNoticeTimeout = undefined;
  notice.textContent = message;
  notice.classList.toggle("error", error);
  notice.dataset.noticeType = type;
  notice.classList.remove("hidden");
  if (!error && type === "message") {
    syncNoticeTimeout = setTimeout(() => {
      if (notice.dataset.noticeType === type && notice.textContent === message) hideNotice();
    }, 5000);
  }
}

function hideNotice() {
  clearTimeout(syncNoticeTimeout);
  syncNoticeTimeout = undefined;
  notice.classList.add("hidden");
  delete notice.dataset.noticeType;
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function gameStatusLabel(status) {
  return statusNames[status] || "Da giocare";
}

function statusTone(value) {
  const status = (value || "").toLowerCase();
  if (/complete|finished|released/.test(status)) return "complete";
  if (/ongoing|development|progress|early access/.test(status)) return "ongoing";
  if (/abandon|cancel|discontinued/.test(status)) return "abandoned";
  if (/hold|paused/.test(status)) return "paused";
  return "neutral";
}

function ratingText(rating) {
  const score = Number(rating);
  return Number.isFinite(score) && score > 0 ? `★ ${score.toFixed(1)}` : "★ -";
}

function imageSource(value) {
  try {
    const url = new URL(value, window.location.href);
    if (url.protocol === "https:" && (
      url.hostname === "f95zone.to"
      || url.hostname.endsWith(".f95zone.to")
      || url.hostname.endsWith(".f95zone.com")
      || url.hostname.endsWith(".f95zone.ninja")
    )) {
      return `/api/image?url=${encodeURIComponent(url.href)}`;
    }
  } catch {
    return "";
  }
  return value;
}

function updateDate(value) {
  if (!value) return "Data non disponibile";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Data non disponibile";
  return date.toLocaleDateString("it-IT", { day: "numeric", month: "short", year: "numeric" });
}

function compactCount(value) {
  const count = Number(value) || 0;
  if (count <= 0) return "-";
  return new Intl.NumberFormat("it-IT", { notation: "compact", maximumFractionDigits: 1 }).format(count);
}

function sortOrderLabel() {
  if (state.sort === "name") return state.direction === "asc" ? "A–Z" : "Z–A";
  return state.direction === "asc" ? "Crescente" : "Decrescente";
}

function renderSortDirection() {
  const button = document.querySelector("#sort-direction");
  button.textContent = state.direction === "asc" ? "↑" : "↓";
  button.title = `Inverti ordine (${sortOrderLabel()})`;
  button.setAttribute("aria-label", `Inverti ordine (${sortOrderLabel()})`);
  document.querySelector("#sort-order-label").textContent = sortOrderLabel();
}

async function loadGames() {
  const params = new URLSearchParams({
    q: state.search,
    status: state.status,
    sort: state.sort,
    direction: state.direction,
  });
  for (const [key, values] of Object.entries({
    game_status: state.filters.include.game_statuses,
    engine: state.filters.include.engines,
    tag: state.filters.include.tags,
    personal_status: state.filters.include.personal_statuses,
    exclude_game_status: state.filters.exclude.game_statuses,
    exclude_engine: state.filters.exclude.engines,
    exclude_tag: state.filters.exclude.tags,
    exclude_personal_status: state.filters.exclude.personal_statuses,
  })) {
    values.forEach((value) => params.append(key, value));
  }
  state.games = await api(`/api/games?${params}`);
  renderGames();
}

function switchPage(page) {
  state.pageView = page;
  document.querySelector("#library-view").classList.toggle("hidden", page !== "library");
  document.querySelector("#catalog-view").classList.toggle("hidden", page !== "catalog");
  document.querySelector("#catalog-nav-button").classList.toggle("active", page === "catalog");
  document.querySelectorAll(".status-nav .nav-item").forEach((button) => {
    button.classList.toggle("active", page === "library" && button.dataset.status === state.status);
  });
  document.querySelector("#current-filter").textContent = page === "catalog" ? "Catalogo F95Zone" : statusNames[state.status];
  document.querySelector("#add-button").classList.toggle("hidden", page === "catalog");
  if (page === "catalog") loadCatalog().catch(showCatalogError);
}

function showCatalogError(error) {
  const element = document.querySelector("#catalog-notice");
  element.textContent = error.message || String(error);
  element.classList.remove("hidden");
}

async function loadCatalog() {
  const params = new URLSearchParams({
    search: state.catalog.search,
    creator: state.catalog.filters.creator,
    ignored: state.catalog.filters.ignored ? "show" : "hide",
    date: String(state.catalog.filters.date),
    page: String(state.catalog.page),
    sort: state.catalog.sort,
    prefixtype: state.catalog.filters.prefixType,
    tagtype: state.catalog.filters.tagType,
  });
  for (const [param, values] of [
    ["prefixes[]", [...new Set([...state.catalog.filters.prefixes, ...state.catalog.filters.engines, ...state.catalog.filters.statuses])]],
    ["noprefixes[]", [...new Set([...state.catalog.filters.excludePrefixes, ...state.catalog.filters.excludeEngines, ...state.catalog.filters.excludeStatuses])]],
    ["tags[]", state.catalog.filters.tags],
    ["notags[]", state.catalog.filters.excludeTags],
  ]) {
    values.forEach((value) => params.append(param, value));
  }
  const result = await api(`/api/catalog?${params}`);
  state.catalog.games = result.games;
  state.catalog.page = result.page;
  state.catalog.totalPages = result.total_pages;
  state.catalog.totalGames = result.total_games;
  renderCatalog();
}

function renderCatalog() {
  const catalog = state.catalog;
  document.querySelector("#catalog-total").textContent = Number(catalog.totalGames).toLocaleString("it-IT");
  document.querySelector("#catalog-page-label").textContent = `Pagina ${catalog.page} di ${catalog.totalPages}`;
  document.querySelector("#catalog-prev").disabled = catalog.page <= 1;
  document.querySelector("#catalog-next").disabled = catalog.page >= catalog.totalPages;
  const activeFilters = [
    ...catalog.filters.prefixes.map((id) => `Prefisso: ${catalogFilterName("prefixes", id)}`),
    ...catalog.filters.engines.map((id) => `Engine: ${catalogFilterName("engines", id)}`),
    ...catalog.filters.statuses.map((id) => `Stato: ${catalogFilterName("statuses", id)}`),
    ...catalog.filters.tags.map((id) => catalogFilterName("tags", id)),
  ];
  const excludedFilters = [
    ...catalog.filters.excludePrefixes.map((id) => `Prefisso: ${catalogFilterName("prefixes", id)}`),
    ...catalog.filters.excludeEngines.map((id) => `Engine: ${catalogFilterName("engines", id)}`),
    ...catalog.filters.excludeStatuses.map((id) => `Stato: ${catalogFilterName("statuses", id)}`),
    ...catalog.filters.excludeTags.map((id) => catalogFilterName("tags", id)),
  ];
  if (catalog.filters.creator) activeFilters.push(`Creatore: ${catalog.filters.creator}`);
  if (catalog.search) activeFilters.push(`Titolo: ${catalog.search}`);
  if (catalog.filters.date) activeFilters.push(
    catalog.filters.date === 1 ? "Aggiornati oggi" : `Aggiornati < ${catalog.filters.date} giorni`
  );
  if (catalog.filters.ignored) activeFilters.push("Thread ignorati inclusi");
  document.querySelector("#catalog-active-filters").textContent = [
    ...activeFilters.map((label) => `+ ${label}`),
    ...excludedFilters.map((label) => `− ${label}`),
  ].join(" · ");
  const gridElement = document.querySelector("#catalog-grid");
  gridElement.innerHTML = catalog.games.map((game) => {
    const cover = game.cover_url
      ? `<img src="${escapeHtml(imageSource(game.cover_url))}" alt="" loading="lazy" onerror="this.remove()">`
      : "";
    const rating = Number(game.rating);
    return `
      <article class="catalog-card" data-thread-id="${game.thread_id}" tabindex="0" aria-label="Dettagli di ${escapeHtml(game.title)}">
        <div class="catalog-cover">
          ${cover}<div class="cover-fallback">${escapeHtml((game.title || "?").slice(0, 1).toUpperCase())}</div>
        </div>
        <div class="catalog-card-body">
          <h2 title="${escapeHtml(game.title)}">${escapeHtml(game.title)}</h2>
          <p>${escapeHtml(game.creator || "Sviluppatore non indicato")}</p>
          <div class="catalog-card-stats">
            <span class="catalog-rating">★ ${rating > 0 ? rating.toFixed(1) : "-"}</span>
            <span class="catalog-likes">♥ ${compactCount(game.likes)}</span>
            <span>◉ ${compactCount(game.views)}</span>
          </div>
          <small>${game.version ? `v${escapeHtml(game.version.replace(/^v(?=\d)/i, ""))} · ` : ""}${escapeHtml(updateDate(game.updated_at))}</small>
          ${game.in_library
            ? '<p class="catalog-in-library" role="status">✓ Gioco già presente nella libreria</p>'
            : `<button type="button" class="button button-primary catalog-add-button" data-thread-url="${escapeHtml(game.thread_url)}">＋ Aggiungi alla libreria</button>`}
        </div>
      </article>`;
  }).join("");
  gridElement.querySelectorAll(".catalog-card").forEach((card) => {
    card.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      openCatalogDetails(Number(card.dataset.threadId));
    });
    card.addEventListener("keydown", (event) => {
      if ((event.key === "Enter" || event.key === " ") && !event.target.closest("button")) {
        event.preventDefault();
        openCatalogDetails(Number(card.dataset.threadId));
      }
    });
  });
  gridElement.querySelectorAll(".catalog-add-button").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      addCatalogGame(button);
    });
  });
  gridElement.classList.toggle("hidden", catalog.games.length === 0);
  if (!catalog.games.length) {
    document.querySelector("#catalog-notice").textContent = "Nessun gioco trovato con questi criteri.";
    document.querySelector("#catalog-notice").classList.remove("hidden");
  } else {
    document.querySelector("#catalog-notice").classList.add("hidden");
  }
}

async function openCatalogDetails(threadId) {
  const game = state.catalog.games.find((item) => item.thread_id === threadId);
  if (!game) return;
  const content = document.querySelector("#detail-content");
  content.innerHTML = `
    <header class="detail-page-header">
      <button class="button button-quiet" id="catalog-detail-back">← Catalogo</button>
    </header>
    <main class="detail-page-content">
      <div class="detail-inner"><p class="eyebrow">CATALOGO F95ZONE</p><h1 class="detail-title">${escapeHtml(game.title)}</h1>
      <p class="catalog-detail-loading" role="status">Caricamento dei dettagli del thread...</p></div>
    </main>`;
  detailModal.showModal();
  document.querySelector("#catalog-detail-back").addEventListener("click", () => detailModal.close());
  try {
    const details = await api(`/api/catalog/details?thread_id=${encodeURIComponent(threadId)}`);
    const coverUrl = details.image_url || game.cover_url;
    const cover = coverUrl
      ? `<img class="detail-cover" src="${escapeHtml(imageSource(coverUrl))}" alt="" onerror="this.remove()">`
      : "";
    const galleryItems = [...new Set([...(details.gallery || []), game.cover_url].filter((image) => image && image !== coverUrl))];
    const gallery = galleryItems.map((image) =>
      `<img src="${escapeHtml(imageSource(image))}" alt="Immagine di ${escapeHtml(game.title)}" loading="lazy" onerror="this.remove()">`
    ).join("");
    const metadataBadges = [
      details.game_status ? `<span class="metadata-badge status-${statusTone(details.game_status)}">${escapeHtml(details.game_status)}</span>` : "",
      details.engine ? `<span class="metadata-badge engine-badge">${escapeHtml(details.engine)}</span>` : "",
      ...(details.tags || []).map((tag) => `<span class="metadata-badge">${escapeHtml(tag)}</span>`),
    ].filter(Boolean).join("");
    const rating = Number(game.rating);
    content.innerHTML = `
      <header class="detail-page-header">
        <button class="button button-quiet" id="catalog-detail-back">← Catalogo</button>
        <a class="button button-quiet" href="${escapeHtml(details.thread_url)}" target="_blank" rel="noopener noreferrer">Apri thread su F95Zone ↗</a>
      </header>
      <main class="detail-page-content">
        ${cover}
        <div class="detail-inner">
          <div class="detail-top"><div><p class="eyebrow">DETTAGLI DEL GIOCO</p><h1 class="detail-title">${escapeHtml(details.title || game.title)}</h1>
          <p class="detail-developer">${escapeHtml(details.developer || game.creator || "Sviluppatore non indicato")}</p></div></div>
          ${metadataBadges ? `<div class="detail-meta">${metadataBadges}</div>` : ""}
          <div class="thread-stats">
            <span class="thread-stat rating-stat">★ ${rating > 0 ? rating.toFixed(1) : "-"}</span>
            <span class="thread-stat like-stat">♥ ${compactCount(game.likes)} like</span>
            <span class="thread-stat">◉ ${compactCount(game.views)} visualizzazioni</span>
          </div>
          <section class="detail-section"><h3>OVERVIEW</h3>
            <p class="detail-description">${escapeHtml(details.description || "Overview non disponibile nel primo post.")}</p>
          </section>
          ${gallery ? `<section class="detail-section"><h3>IMMAGINI</h3><div class="image-gallery">${gallery}</div></section>` : ""}
          <section class="detail-section catalog-detail-info"><h3>INFORMAZIONI</h3>
            <dl>
              <div><dt>Versione</dt><dd>${escapeHtml(details.latest_version || game.version || "Non indicata")}</dd></div>
              <div><dt>Ultimo aggiornamento</dt><dd>${escapeHtml(updateDate(details.thread_updated_at || game.updated_at))}</dd></div>
              <div><dt>Stato</dt><dd>${escapeHtml(details.game_status || "Non indicato")}</dd></div>
              <div><dt>Engine</dt><dd>${escapeHtml(details.engine || "Non indicato")}</dd></div>
            </dl>
          </section>
          ${game.in_library
            ? '<p class="catalog-in-library" role="status">✓ Gioco già presente nella libreria</p>'
            : `<button type="button" class="button button-primary catalog-detail-add" data-thread-url="${escapeHtml(game.thread_url)}">＋ Aggiungi alla libreria</button>`}
        </div>
      </main>`;
    document.querySelector("#catalog-detail-back").addEventListener("click", () => detailModal.close());
    document.querySelector(".catalog-detail-add")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      button.textContent = "Aggiunta...";
      try {
        await api("/api/games", {
          method: "POST",
          body: JSON.stringify({ thread_url: button.dataset.threadUrl }),
        });
        game.in_library = true;
        renderCatalog();
        button.outerHTML = '<p class="catalog-in-library" role="status">✓ Gioco già presente nella libreria</p>';
      } catch (error) {
        button.disabled = false;
        button.textContent = "＋ Aggiungi alla libreria";
        showNotice(error.message, true);
      }
    });
  } catch (error) {
    content.innerHTML = `
      <header class="detail-page-header"><button class="button button-quiet" id="catalog-detail-back">← Catalogo</button></header>
      <main class="detail-page-content"><div class="detail-inner"><h1 class="detail-title">${escapeHtml(game.title)}</h1>
      <p class="detail-error" role="alert">${escapeHtml(error.message || String(error))}</p></div></main>`;
    document.querySelector("#catalog-detail-back").addEventListener("click", () => detailModal.close());
  }
}

function cloneCatalogFilters(filters) {
  return {
    creator: filters.creator,
    searchMode: filters.searchMode,
    titleSearch: state.catalog.search,
    ignored: filters.ignored,
    date: filters.date,
    prefixes: [...filters.prefixes],
    excludePrefixes: [...filters.excludePrefixes],
    engines: [...filters.engines],
    excludeEngines: [...filters.excludeEngines],
    statuses: [...filters.statuses],
    excludeStatuses: [...filters.excludeStatuses],
    tags: [...filters.tags],
    excludeTags: [...filters.excludeTags],
    prefixType: filters.prefixType,
    tagType: filters.tagType,
  };
}

function renderCatalogFilterOptions(kind) {
  if (!catalogFilterOptions || !catalogDraftFilters) return;
  const config = {
    engines: {
      items: catalogFilterOptions.engines,
      searchId: "#catalog-engine-search",
      listId: "#catalog-engine-list",
      includeKey: "engines",
      excludeKey: "excludeEngines",
      countId: "#catalog-engine-count",
    },
    statuses: {
      items: catalogFilterOptions.statuses,
      searchId: "#catalog-status-search",
      listId: "#catalog-status-list",
      includeKey: "statuses",
      excludeKey: "excludeStatuses",
      countId: "#catalog-status-count",
    },
    others: {
      items: catalogFilterOptions.others,
      searchId: "#catalog-other-search",
      listId: "#catalog-other-list",
      includeKey: "prefixes",
      excludeKey: "excludePrefixes",
      countId: "#catalog-other-count",
    },
    tags: {
      items: catalogFilterOptions.tags,
      searchId: "#catalog-tag-search",
      selectId: "#catalog-tag-select",
      includeKey: "tags",
      excludeKey: "excludeTags",
      label: "un tag",
    },
    excludeTags: {
      items: catalogFilterOptions.tags,
      searchId: "#catalog-exclude-tag-search",
      selectId: "#catalog-exclude-tag-select",
      includeKey: "excludeTags",
      excludeKey: "excludeTags",
      label: "un tag",
    },
  }[kind];
  if (!config) return;
  config.items ||= [];
  const { items, searchId, includeKey, excludeKey } = config;
  const search = document.querySelector(searchId).value.trim().toLocaleLowerCase("it");
  const matches = search ? items.filter((item) => item.name.toLocaleLowerCase("it").includes(search)) : items;
  if (config.listId) {
    const list = document.querySelector(config.listId);
    list.innerHTML = matches.map((item) => {
      const included = catalogDraftFilters[includeKey].includes(item.id);
      const excluded = catalogDraftFilters[excludeKey].includes(item.id);
      return `<div class="catalog-prefix-row">
        <span>${escapeHtml(item.name)}</span>
        <button type="button" class="catalog-prefix-include ${included ? "selected" : ""}" data-catalog-pick-kind="${kind}" data-catalog-pick-id="${item.id}" data-catalog-pick-mode="include" aria-label="Includi ${escapeHtml(item.name)}" aria-pressed="${included}">✓</button>
        <button type="button" class="catalog-prefix-exclude ${excluded ? "selected" : ""}" data-catalog-pick-kind="${kind}" data-catalog-pick-id="${item.id}" data-catalog-pick-mode="exclude" aria-label="Escludi ${escapeHtml(item.name)}" aria-pressed="${excluded}">×</button>
      </div>`;
    }).join("") || '<p class="catalog-filter-empty">Nessun valore trovato in SAM.</p>';
    document.querySelector(config.countId).textContent = `(${matches.length})`;
    return;
  }
  const selectId = kind === "tags" ? "#catalog-tag-select" : "#catalog-exclude-tag-select";
  const select = document.querySelector(selectId);
  const currentValue = select.value;
  const label = "un tag";
  const emptyLabel = matches.length ? `Seleziona ${label}` : "Nessun risultato";
  select.innerHTML = `<option value="">${emptyLabel}</option>${matches.map((item) => {
    const included = catalogDraftFilters[kind === "excludeTags" ? "tags" : includeKey].includes(item.id);
    const excluded = catalogDraftFilters[excludeKey].includes(item.id);
    const mode = kind === "excludeTags"
      ? excluded ? " · escluso" : included ? " · incluso" : ""
      : included ? " · incluso" : excluded ? " · escluso" : "";
    return `<option value="${item.id}">${escapeHtml(item.name + mode)}</option>`;
  }).join("")}`;
  if (matches.some((item) => String(item.id) === currentValue)) select.value = currentValue;
}

function renderCatalogFilterSummary() {
  if (!catalogDraftFilters || !catalogFilterOptions) return;
  const summary = document.querySelector("#catalog-filter-summary");
  const chips = [];
  for (const [mode, keys, sign] of [
    ["include", ["prefixes", "engines", "statuses", "tags"], "+"],
    ["exclude", ["excludePrefixes", "excludeEngines", "excludeStatuses", "excludeTags"], "−"],
  ]) {
    for (const key of keys) {
      const kind = key.toLowerCase().includes("engine") ? "engines"
        : key.toLowerCase().includes("status") ? "statuses"
          : key.toLowerCase().includes("tag") ? "tags" : "prefixes";
      for (const id of catalogDraftFilters[key]) {
        const label = catalogFilterName(kind, id);
        chips.push(
          `<span class="filter-chip ${mode === "exclude" ? "excluded" : ""}">` +
          `${sign} ${kind === "prefixes" ? "Prefisso" : kind === "engines" ? "Engine" : kind === "statuses" ? "Stato" : "Tag"}: ${escapeHtml(label)}` +
          `<button type="button" data-catalog-remove="${key}" data-catalog-remove-id="${id}" aria-label="Rimuovi ${escapeHtml(label)}">×</button></span>`
        );
      }
    }
  }
  if (catalogDraftFilters.date) {
    const dateText = catalogDraftFilters.date === 1
      ? "Aggiornati oggi"
      : `Aggiornati < ${catalogDraftFilters.date} giorni`;
    chips.push(`<span class="filter-chip">${dateText}<button type="button" data-catalog-clear="date" aria-label="Rimuovi limite data">×</button></span>`);
  }
  if (catalogDraftFilters.creator) {
    chips.push(`<span class="filter-chip">Creatore: ${escapeHtml(catalogDraftFilters.creator)}<button type="button" data-catalog-clear="creator" aria-label="Rimuovi filtro creatore">×</button></span>`);
  }
  if (catalogDraftFilters.searchMode === "title" && catalogDraftFilters.titleSearch) {
    chips.push(`<span class="filter-chip">Titolo: ${escapeHtml(catalogDraftFilters.titleSearch)}<button type="button" data-catalog-clear="title" aria-label="Rimuovi ricerca titolo">×</button></span>`);
  }
  if (catalogDraftFilters.ignored) {
    chips.push('<span class="filter-chip">Thread ignorati inclusi<button type="button" data-catalog-clear="ignored" aria-label="Rimuovi filtro thread ignorati">×</button></span>');
  }
  summary.innerHTML = chips.join("") || '<span class="filter-summary-empty">Nessun filtro selezionato</span>';
  document.querySelector("#catalog-tag-limit").textContent = `${catalogDraftFilters.tags.length}/10 tag inclusi`;
  document.querySelector("#catalog-exclude-tag-limit").textContent = `${catalogDraftFilters.excludeTags.length}/10 tag esclusi`;
}

async function openCatalogFilters() {
  catalogDraftFilters = cloneCatalogFilters(state.catalog.filters);
  const searchMode = catalogDraftFilters.searchMode || "creator";
  document.querySelector("#catalog-search-mode").value = searchMode;
  document.querySelector("#catalog-filter-search").value =
    searchMode === "creator" ? catalogDraftFilters.creator : catalogDraftFilters.titleSearch;
  document.querySelector("#catalog-ignored").checked = catalogDraftFilters.ignored;
  document.querySelector("#catalog-date-limit").value = String(Math.max(0, catalogDateLimits.indexOf(catalogDraftFilters.date)));
  document.querySelector("#catalog-date-label").textContent = catalogDateLabel(catalogDraftFilters.date);
  document.querySelector("#catalog-sort-modal").value = state.catalog.sort;
  document.querySelector("#catalog-prefix-mode").value = catalogDraftFilters.prefixType;
  document.querySelector("#catalog-tag-mode").value = catalogDraftFilters.tagType;
  renderCatalogFilterSummary();
  catalogFiltersModal.showModal();
  document.querySelector("#catalog-filters-error").classList.add("hidden");
  document.querySelector("#catalog-filter-counts").textContent = "Caricamento dei filtri aggiornati da SAM...";
  try {
    catalogFilterOptions = await api("/api/catalog/filters?refresh=1");
    catalogFilterOptions.others ||= [];
    renderLoadedCatalogFilters();
  } catch (error) {
    const errorElement = document.querySelector("#catalog-filters-error");
    errorElement.textContent = error.message || String(error);
    errorElement.classList.remove("hidden");
    document.querySelector("#catalog-filter-counts").textContent = "Filtri SAM non disponibili";
  }
}

function renderLoadedCatalogFilters() {
  document.querySelector("#catalog-filter-counts").textContent =
    `${(catalogFilterOptions.tags || []).length.toLocaleString("it-IT")} tag · ${(catalogFilterOptions.engines || []).length} engine · ${(catalogFilterOptions.others || []).length} altri prefissi · ${(catalogFilterOptions.statuses || []).length} stati`;
  renderCatalogFilterOptions("engines");
  renderCatalogFilterOptions("others");
  renderCatalogFilterOptions("statuses");
  renderCatalogFilterOptions("tags");
  renderCatalogFilterOptions("excludeTags");
  renderCatalogFilterSummary();
}

function addCatalogDraftFilter(kind, mode) {
  const select = document.querySelector({
    tags: "#catalog-tag-select",
    excludeTags: "#catalog-exclude-tag-select",
  }[kind]);
  const values = [Number(select.value)].filter(Boolean);
  if (!values.length) return;
  const includeKey = kind === "excludeTags" ? "tags" : kind;
  const excludeKey = kind === "excludeTags" ? "excludeTags" : "excludeTags";
  const resolvedExcludeKey = excludeKey;
  const selectedKey = kind === "excludeTags" || mode === "exclude" ? resolvedExcludeKey : includeKey;
  const otherKey = kind === "excludeTags" ? "tags" : selectedKey === includeKey ? resolvedExcludeKey : includeKey;
  if ((selectedKey === "tags" || selectedKey === "excludeTags") && catalogDraftFilters[selectedKey].length + values.filter((id) => !catalogDraftFilters[selectedKey].includes(id)).length > 10) {
    showCatalogError(new Error("Puoi selezionare al massimo 10 tag per elenco."));
    return;
  }
  for (const value of values) {
    catalogDraftFilters[otherKey] = catalogDraftFilters[otherKey].filter((id) => id !== value);
    if (!catalogDraftFilters[selectedKey].includes(value)) catalogDraftFilters[selectedKey].push(value);
  }
  select.value = "";
  renderCatalogFilterOptions(kind);
  renderCatalogFilterSummary();
}

function removeCatalogDraftFilter(button) {
  const key = button.dataset.catalogRemove;
  const value = Number(button.dataset.catalogRemoveId);
  catalogDraftFilters[key] = catalogDraftFilters[key].filter((id) => id !== value);
  const kind = key.toLowerCase().includes("engine") ? "engines"
    : key.toLowerCase().includes("status") ? "statuses"
      : key.toLowerCase().includes("tag") ? key === "excludeTags" ? "excludeTags" : "tags"
        : "others";
  renderCatalogFilterOptions(kind);
  renderCatalogFilterSummary();
}

function applyCatalogDraftFilters() {
  const searchText = document.querySelector("#catalog-filter-search").value.trim();
  catalogDraftFilters.searchMode = document.querySelector("#catalog-search-mode").value;
  if (catalogDraftFilters.searchMode === "creator") {
    catalogDraftFilters.creator = searchText;
    state.catalog.search = "";
    document.querySelector("#catalog-search").value = "";
  } else {
    catalogDraftFilters.creator = "";
    catalogDraftFilters.titleSearch = searchText;
    state.catalog.search = searchText;
    document.querySelector("#catalog-search").value = searchText;
  }
  catalogDraftFilters.ignored = document.querySelector("#catalog-ignored").checked;
  catalogDraftFilters.date = catalogDateLimits[Number(document.querySelector("#catalog-date-limit").value)];
  catalogDraftFilters.prefixType = document.querySelector("#catalog-prefix-mode").value;
  catalogDraftFilters.tagType = document.querySelector("#catalog-tag-mode").value;
  state.catalog.sort = document.querySelector("#catalog-sort-modal").value;
  document.querySelector("#catalog-sort").value = state.catalog.sort;
  state.catalog.filters = cloneCatalogFilters(catalogDraftFilters);
  state.catalog.page = 1;
  catalogFiltersModal.close();
  loadCatalog().catch(showCatalogError);
}

function catalogFilterName(kind, id) {
  const items = catalogFilterOptions?.[kind];
  return items?.find((item) => item.id === id)?.name || String(id);
}

async function addCatalogGame(button) {
  button.disabled = true;
  button.textContent = "Aggiunta...";
  try {
    await api("/api/games", {
      method: "POST",
      body: JSON.stringify({ thread_url: button.dataset.threadUrl }),
    });
    const game = state.catalog.games.find((item) => item.thread_url === button.dataset.threadUrl);
    if (game) game.in_library = true;
    renderCatalog();
    const notice = document.querySelector("#catalog-notice");
    notice.textContent = "Gioco aggiunto. Importazione dei metadati avviata.";
    notice.dataset.noticeType = "catalog-sync";
    notice.classList.remove("hidden");
    catalogSyncPending = true;
  } catch (error) {
    button.disabled = false;
    button.textContent = "＋ Aggiungi alla libreria";
    showCatalogError(error);
  }
}

function populateQuickStatus(values) {
  const select = document.querySelector("#quick-f95-status");
  const selected = state.filters.include.game_statuses.length === 1 ? state.filters.include.game_statuses[0] : "";
  select.innerHTML = `<option value="">Tutti gli stati</option>${values.map((value) =>
    `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`
  ).join("")}`;
  select.value = selected;
}

function updateFilterCount() {
  const count = Object.values(state.filters.include).flat().length + Object.values(state.filters.exclude).flat().length;
  const badge = document.querySelector("#filter-count");
  badge.textContent = count;
  badge.classList.toggle("hidden", count === 0);
  const quickStatus = state.filters.include.game_statuses.length === 1 ? state.filters.include.game_statuses[0] : "";
  document.querySelector("#quick-f95-status").value = quickStatus;
}

function cloneFilters(filters) {
  return {
    include: Object.fromEntries(Object.entries(filters.include).map(([key, values]) => [key, [...values]])),
    exclude: Object.fromEntries(Object.entries(filters.exclude).map(([key, values]) => [key, [...values]])),
  };
}

function renderFilterSummary() {
  const summary = document.querySelector("#filter-summary-list");
  const chips = [];
  for (const mode of ["include", "exclude"]) {
    const sign = mode === "include" ? "+" : "−";
    for (const [category, values] of Object.entries(draftFilters[mode])) {
      for (const value of values) {
        const label = category === "personal_statuses" ? gameStatusLabel(value) : value;
        chips.push(
          `<span class="filter-chip ${mode === "exclude" ? "excluded" : ""}">` +
          `${sign} ${escapeHtml(filterCategories[category].label)}: ${escapeHtml(label)}` +
          `<button type="button" data-remove-filter-mode="${mode}" data-remove-filter-category="${category}" data-remove-filter-value="${escapeHtml(value)}" aria-label="Rimuovi filtro ${escapeHtml(label)}">×</button></span>`
        );
      }
    }
  }
  summary.innerHTML = chips.join("") || '<span class="filter-summary-empty">Nessun filtro selezionato</span>';
}

function renderLibraryFilterOptions(category) {
  const config = filterCategories[category];
  if (!config || !draftFilters) return;
  const options = category === "personal_statuses"
    ? ["playing", "wishlist", "completed", "on_hold", "dropped"]
    : availableFilterOptions?.[category] || [];
  const search = config.searchId
    ? document.querySelector(config.searchId).value.trim().toLocaleLowerCase("it")
    : "";
  const values = search
    ? options.filter((value) => {
      const label = category === "personal_statuses" ? gameStatusLabel(value) : value;
      return label.toLocaleLowerCase("it").includes(search);
    })
    : options;
  document.querySelector(config.countId).textContent = `(${values.length})`;
  document.querySelector(config.listId).innerHTML = values.map((value) => {
    const label = category === "personal_statuses" ? gameStatusLabel(value) : value;
    const included = draftFilters.include[category].includes(value);
    const excluded = draftFilters.exclude[category].includes(value);
    return `<div class="catalog-prefix-row">
      <span>${escapeHtml(label)}</span>
      <button type="button" class="catalog-prefix-include ${included ? "selected" : ""}" data-library-pick-category="${category}" data-library-pick-value="${escapeHtml(value)}" data-library-pick-mode="include" aria-label="Includi ${escapeHtml(label)}" aria-pressed="${included}">✓</button>
      <button type="button" class="catalog-prefix-exclude ${excluded ? "selected" : ""}" data-library-pick-category="${category}" data-library-pick-value="${escapeHtml(value)}" data-library-pick-mode="exclude" aria-label="Escludi ${escapeHtml(label)}" aria-pressed="${excluded}">×</button>
    </div>`;
  }).join("") || '<p class="catalog-filter-empty">Nessun valore trovato nella libreria.</p>';
}

async function openFilters() {
  const options = await api("/api/filters");
  availableFilterOptions = options;
  draftFilters = cloneFilters(state.filters);
  populateQuickStatus(options.game_statuses);
  for (const category of Object.keys(filterCategories)) renderLibraryFilterOptions(category);
  renderFilterSummary();
  filtersModal.showModal();
}

function removeDraftFilter(button) {
  const { removeFilterMode: mode, removeFilterCategory: category, removeFilterValue: value } = button.dataset;
  draftFilters[mode][category] = draftFilters[mode][category].filter((item) => item !== value);
  renderLibraryFilterOptions(category);
  renderFilterSummary();
}

function applyDraftFilters() {
  state.filters = cloneFilters(draftFilters);
  updateFilterCount();
  filtersModal.close();
  loadGames().catch((error) => showNotice(error.message, true));
}

function renderGames() {
  const games = state.games;
  document.querySelector("#game-count").textContent = games.length;
  document.querySelector("#count-all").textContent = state.status === "all" ? games.length : "";
  document.querySelector("#current-filter").textContent = statusNames[state.status];
  document.querySelector("#page-title").textContent = state.status === "all" ? "La tua libreria" : statusNames[state.status];
  document.querySelector("#empty-add-button").classList.toggle("hidden", state.status !== "all");
  grid.innerHTML = games.map((game) => {
    const cover = game.image_url
      ? `<img src="${escapeHtml(imageSource(game.image_url))}" alt="" loading="lazy" onerror="this.remove()">`
      : "";
    const displayedVersion = game.user_version || game.latest_version || game.version;
    const threadRating = Number(game.f95_rating);
    const ratingTitle = Number.isFinite(threadRating) && threadRating > 0
      ? `Valutazione F95Zone: ${game.f95_rating} su 5`
      : "Valutazione F95Zone non disponibile";
    const badges = [
      game.game_status ? `<span class="card-badge card-status status-${statusTone(game.game_status)}">${escapeHtml(game.game_status)}</span>` : "",
      game.engine ? `<span class="card-badge card-engine">${escapeHtml(game.engine)}</span>` : "",
    ].filter(Boolean).join("");
    return `
      <article class="game-card" data-id="${game.id}" tabindex="0" aria-label="Apri ${escapeHtml(game.title)}">
        <div class="cover-wrap">
          ${cover}<div class="cover-fallback">${escapeHtml((game.title || "?").slice(0, 1).toUpperCase())}</div>
          <span class="status-pill">${escapeHtml(gameStatusLabel(game.personal_status))}</span>
          ${game.last_error
            ? '<span class="update-pill error-pill">⚠ Verifica fallita</span>'
            : game.update_available ? '<span class="update-pill">✦ Update</span>' : ""}
        </div>
        <div class="card-body">
          <h2 class="card-title">${escapeHtml(game.title)}</h2>
          <p class="developer">${escapeHtml(game.developer || "Thread F95Zone")}</p>
          <div class="card-badges">${badges}</div>
          <div class="card-stats">
            <span class="card-rating" title="${escapeHtml(ratingTitle)}">${Number.isFinite(threadRating) && threadRating > 0 ? `★ ${threadRating.toFixed(1)}` : "★ -"}</span>
            <span title="Mi piace">♥ ${compactCount(game.likes)}</span>
            <span title="Visualizzazioni">◉ ${compactCount(game.views)}</span>
          </div>
          <div class="card-footer"><span title="${escapeHtml(game.thread_updated_at || "")}">${escapeHtml(updateDate(game.thread_updated_at))}</span><span class="card-version">${displayedVersion ? `v${escapeHtml(displayedVersion)}` : "↗ F95Zone"}</span></div>
        </div>
      </article>`;
  }).join("");
  grid.querySelectorAll(".game-card").forEach((card) => {
    const open = () => openDetails(Number(card.dataset.id));
    card.addEventListener("click", open);
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
    });
  });
  emptyState.classList.toggle("hidden", games.length > 0);
  grid.classList.toggle("hidden", games.length === 0);
}

function openAddModal() {
  document.querySelector("#form-error").classList.add("hidden");
  document.querySelector("#add-form").reset();
  addModal.showModal();
}

async function openSettings() {
  document.querySelector("#auth-error").classList.add("hidden");
  document.querySelector("#settings-login-form").reset();
  settingsModal.showModal();
  await refreshAuthStatus();
}

async function refreshAuthStatus() {
  const errorElement = document.querySelector("#auth-error");
  try {
    const status = await api("/api/settings/auth");
    const connected = document.querySelector("#auth-connected");
    connected.classList.toggle("hidden", !status.logged_in);
    document.querySelector("#auth-login-fields").classList.toggle("hidden", status.logged_in);
    document.querySelector("#login-button").classList.toggle("hidden", status.logged_in);
    document.querySelector("#auth-username").textContent = status.username || "";
    errorElement.classList.add("hidden");
  } catch (error) {
    errorElement.textContent = error.message;
    errorElement.classList.remove("hidden");
  }
}

function renderDetail(game) {
  const cover = game.image_url
    ? `<img class="detail-cover" src="${escapeHtml(imageSource(game.image_url))}" alt="" onerror="this.remove()">`
    : "";
  const tags = (game.tags || []).map((tag) => `<span>${escapeHtml(tag)}</span>`).join("");
  const gallery = (game.gallery || [])
    .filter((image) => image !== game.image_url)
    .map((image) => `<img src="${escapeHtml(imageSource(image))}" alt="" loading="lazy" onerror="this.remove()">`)
    .join("");
  document.querySelector("#detail-content").innerHTML = `
    <header class="detail-page-header">
      <button class="button button-quiet" id="detail-close">← Libreria</button>
      <div class="detail-tools">
        <button class="button button-quiet" id="edit-metadata">Modifica metadati</button>
        <div class="sync-control detail-sync-control">
          <button class="icon-button" id="detail-sync" title="Controlla aggiornamenti">⟳</button>
          <span class="sync-progress" id="detail-sync-progress" aria-live="polite">0/0</span>
        </div>
      </div>
    </header>
    <main class="detail-page-content">
      ${cover}
      <div class="detail-inner">
      <div class="detail-top">
        <div><p class="eyebrow">DETTAGLI DEL GIOCO</p><h1 class="detail-title">${escapeHtml(game.title)}</h1><p class="detail-developer">${escapeHtml(game.developer || "Sviluppatore non indicato")}</p></div>
      </div>
      <div class="detail-meta">
        ${game.game_status ? `<span class="metadata-badge status-${statusTone(game.game_status)}">${escapeHtml(game.game_status)}</span>` : ""}
        ${game.engine ? `<span class="metadata-badge engine-badge">${escapeHtml(game.engine)}</span>` : ""}
        <span class="metadata-badge library-badge">${escapeHtml(gameStatusLabel(game.personal_status))}</span>
        ${tags}
        ${game.update_available ? "<span>Nuovo aggiornamento rilevato</span>" : ""}
      </div>
      <div class="thread-stats">
        <span class="thread-stat rating-stat" title="Valutazione F95Zone">${escapeHtml(ratingText(game.f95_rating))}</span>
        <span class="thread-stat like-stat">♥ ${compactCount(game.likes)} like</span>
        <span class="thread-stat">◉ ${compactCount(game.views)} visualizzazioni</span>
      </div>
      ${game.last_error ? `<div class="detail-error">${escapeHtml(game.last_error)}</div>` : ""}
      <section class="detail-section">
        <h3>OVERVIEW</h3>
        <p class="detail-description">${escapeHtml(game.description || "Overview non disponibile nel primo post.")}</p>
      </section>
      ${gallery ? `<section class="detail-section"><h3>IMMAGINI</h3><div class="image-gallery">${gallery}</div></section>` : ""}
      <section class="detail-section">
        <h3>VERSIONI</h3>
        <div class="version-panel">
          <label>La mia versione<div class="version-entry">
            <input id="user-version" value="${escapeHtml(game.user_version || "")}" maxlength="300" placeholder="Inserisci la versione che stai usando">
            <button type="button" class="button button-quiet" id="copy-latest-version" ${game.latest_version ? "" : "disabled"}>Usa l'ultima</button>
          </div><small class="version-date">Data inserimento: ${escapeHtml(updateDate(game.user_version_updated_at))}</small></label>
          <div class="latest-version"><span>Ultima versione su F95Zone</span><strong>${escapeHtml(game.latest_version || "Non rilevata")}</strong><small class="version-date">Data versione: ${escapeHtml(updateDate(game.thread_updated_at))}</small></div>
        </div>
      </section>
      <section class="detail-section">
        <h3>COMMENTI PERSONALI</h3>
        <textarea class="detail-notes" id="detail-notes" placeholder="Aggiungi una nota o un commento personale...">${escapeHtml(game.notes || "")}</textarea>
        <div class="personal-rating">
          <span>Il mio voto</span>
          <div class="personal-stars" role="group" aria-label="Voto personale da una a cinque stelle">
            ${[1, 2, 3, 4, 5].map((rating) => `<button type="button" class="personal-star ${rating <= (game.rating || 0) ? "selected" : ""}" data-rating="${rating}" aria-label="${rating} stelle" aria-pressed="${rating === game.rating}">★</button>`).join("")}
          </div>
        </div>
      </section>
      <div class="detail-actions">
        <select id="detail-status" aria-label="Stato del gioco">
          ${Object.entries(statusNames).filter(([key]) => key !== "all").map(([key, label]) => `<option value="${key}" ${game.personal_status === key ? "selected" : ""}>${label}</option>`).join("")}
        </select>
        <a href="${escapeHtml(game.thread_url)}" target="_blank" rel="noopener noreferrer">Apri thread su F95Zone ↗</a>
        ${game.update_available ? '<button class="button button-quiet" id="mark-update-seen">Segna update come visto</button>' : ""}
        <button class="button button-primary" id="save-detail">Salva commenti e versione</button>
        <button class="button button-quiet" id="delete-game">Rimuovi</button>
      </div>
      </div>
    </main>`;
  document.querySelector("#detail-close").addEventListener("click", () => detailModal.close());
  document.querySelector("#detail-sync").addEventListener("click", () => queueSync(game.id));
  document.querySelector("#edit-metadata").addEventListener("click", () => openMetadataEditor(game));
  document.querySelector("#copy-latest-version").addEventListener("click", () => {
    document.querySelector("#user-version").value = game.latest_version || "";
  });
  document.querySelectorAll(".personal-star").forEach((button) => {
    button.addEventListener("click", () => {
      state.activeGame.rating = Number(button.dataset.rating);
      document.querySelectorAll(".personal-star").forEach((star) => {
        const selected = Number(star.dataset.rating) <= state.activeGame.rating;
        star.classList.toggle("selected", selected);
        star.setAttribute("aria-pressed", String(Number(star.dataset.rating) === state.activeGame.rating));
      });
    });
  });
  document.querySelector("#mark-update-seen")?.addEventListener("click", () => markUpdateSeen(game.id));
  document.querySelector("#save-detail").addEventListener("click", () => saveDetail(game.id));
  document.querySelector("#delete-game").addEventListener("click", () => deleteGame(game.id));
}

function openMetadataEditor(game) {
  const form = document.querySelector("#metadata-form");
  form.elements.title.value = game.title || "";
  form.elements.developer.value = game.developer || "";
  form.elements.game_status.value = game.game_status || "";
  form.elements.engine.value = game.engine || "";
  form.elements.tags.value = (game.tags || []).join(", ");
  document.querySelector("#metadata-error").classList.add("hidden");
  metadataModal.showModal();
}

async function openDetails(id) {
  try {
    state.activeGame = await api(`/api/games/${id}`);
    renderDetail(state.activeGame);
    detailModal.showModal();
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function saveDetail(id) {
  try {
    state.activeGame = await api(`/api/games/${id}`, {
      method: "PATCH",
      body: JSON.stringify({
        user_version: document.querySelector("#user-version").value,
        notes: document.querySelector("#detail-notes").value,
        personal_status: document.querySelector("#detail-status").value,
        rating: state.activeGame.rating || null,
      }),
    });
    renderDetail(state.activeGame);
    await loadGames();
    showNotice("Commenti, voto e versione personale salvati.");
  } catch (error) { showNotice(error.message, true); }
}

document.querySelector("#metadata-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const errorElement = document.querySelector("#metadata-error");
  errorElement.classList.add("hidden");
  try {
    state.activeGame = await api(`/api/games/${state.activeGame.id}`, {
      method: "PATCH",
      body: JSON.stringify({
        title: form.elements.title.value,
        developer: form.elements.developer.value,
        game_status: form.elements.game_status.value,
        engine: form.elements.engine.value,
        tags: form.elements.tags.value.split(",").map((tag) => tag.trim()).filter(Boolean),
      }),
    });
    metadataModal.close();
    renderDetail(state.activeGame);
    await loadGames();
    showNotice("Metadati aggiornati.");
  } catch (error) {
    errorElement.textContent = error.message;
    errorElement.classList.remove("hidden");
  }
});

async function markUpdateSeen(id) {
  try {
    await api(`/api/games/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ update_available: false }),
    });
    state.activeGame = await api(`/api/games/${id}`);
    renderDetail(state.activeGame);
    await loadGames();
    showNotice("Aggiornamento segnato come visto.");
  } catch (error) { showNotice(error.message, true); }
}

async function deleteGame(id) {
  if (!window.confirm("Rimuovere questo gioco dalla libreria? Le note salvate verranno eliminate.")) return;
  try {
    await api(`/api/games/${id}`, { method: "DELETE" });
    detailModal.close();
    await loadGames();
    showNotice("Gioco rimosso dalla libreria.");
  } catch (error) { showNotice(error.message, true); }
}

async function queueSync(id = null) {
  try {
    const result = await api(id ? `/api/games/${id}/sync` : "/api/sync", { method: "POST", body: "{}" });
    if (result.progress) {
      renderSyncProgress(result.progress);
      if (result.progress.active) {
        showNotice("Controllo F95Zone avviato. La libreria si aggiornerà tra poco.", false, "sync");
      } else {
        hideNotice();
      }
    }
  } catch (error) { showNotice(error.message, true); }
}

function renderSyncProgress(progress) {
  const text = progress.total ? `${progress.completed}/${progress.total}` : "0/0";
  const currentLabel = progress.active && progress.current_title
    ? `Aggiornamento ${text}: ${progress.current_title}`
    : progress.active ? `Aggiornamento ${text}` : `Ultimo aggiornamento: ${text}`;
  for (const id of ["sync-progress", "detail-sync-progress"]) {
    const element = document.getElementById(id);
    if (!element) continue;
    element.textContent = text;
    element.title = currentLabel;
    element.setAttribute("aria-label", currentLabel);
  }
  document.querySelectorAll(".sync-control").forEach((element) => {
    element.classList.toggle("is-active", Boolean(progress.active));
  });
  syncStatusWasActive = Boolean(progress.active);
}

async function pollSyncProgress() {
  if (syncStatusPollInFlight) return;
  syncStatusPollInFlight = true;
  try {
    const progress = await api("/api/sync/status");
    const wasActive = syncStatusWasActive;
    renderSyncProgress(progress);
    syncStatusPollFailed = false;
    if (catalogSyncPending && !progress.active) {
      catalogSyncPending = false;
      const catalogNotice = document.querySelector("#catalog-notice");
      if (catalogNotice.dataset.noticeType === "catalog-sync") {
        catalogNotice.classList.add("hidden");
        catalogNotice.textContent = "";
        delete catalogNotice.dataset.noticeType;
      }
      if (state.pageView === "catalog") await loadCatalog().catch(showCatalogError);
    }
    if (wasActive && !progress.active) {
      await loadGames();
      if (detailModal.open && state.activeGame) {
        state.activeGame = await api(`/api/games/${state.activeGame.id}`);
        renderDetail(state.activeGame);
      }
      if (notice.dataset.noticeType === "sync") {
        clearTimeout(syncNoticeTimeout);
        syncNoticeTimeout = setTimeout(() => {
          if (!syncStatusWasActive && notice.dataset.noticeType === "sync") hideNotice();
        }, 5000);
      }
    }
  } catch (error) {
    if (!syncStatusPollFailed) showNotice(`Impossibile leggere l'avanzamento: ${error.message}`, true);
    syncStatusPollFailed = true;
  } finally {
    syncStatusPollInFlight = false;
  }
}

document.querySelector("#add-button").addEventListener("click", openAddModal);
document.querySelector("#empty-add-button").addEventListener("click", openAddModal);
document.querySelector("#settings-button").addEventListener("click", () => {
  openSettings().catch((error) => showNotice(error.message, true));
});
document.querySelector("#sync-button").addEventListener("click", () => queueSync());
document.querySelector("#filters-button").addEventListener("click", () => {
  openFilters().catch((error) => showNotice(error.message, true));
});
document.querySelector("#filter-tag-search").addEventListener("input", () => renderLibraryFilterOptions("tags"));
document.querySelector("#library-game-status-search").addEventListener("input", () => renderLibraryFilterOptions("game_statuses"));
document.querySelector("#library-engine-search").addEventListener("input", () => renderLibraryFilterOptions("engines"));
document.querySelector("#close-filters").addEventListener("click", () => filtersModal.close());
document.querySelector("#quick-f95-status").addEventListener("change", (event) => {
  const value = event.currentTarget.value;
  state.filters.include.game_statuses = value ? [value] : [];
  if (value) state.filters.exclude.game_statuses = state.filters.exclude.game_statuses.filter((item) => item !== value);
  updateFilterCount();
  loadGames().catch((error) => showNotice(error.message, true));
});
document.querySelector("#filters-form").addEventListener("click", (event) => {
  const pickButton = event.target.closest("[data-library-pick-category]");
  if (!pickButton) return;
  const { libraryPickCategory: category, libraryPickValue: value, libraryPickMode: mode } = pickButton.dataset;
  const otherMode = mode === "include" ? "exclude" : "include";
  draftFilters[otherMode][category] = draftFilters[otherMode][category].filter((item) => item !== value);
  draftFilters[mode][category] = draftFilters[mode][category].includes(value)
    ? draftFilters[mode][category].filter((item) => item !== value)
    : [...draftFilters[mode][category], value];
  renderLibraryFilterOptions(category);
  renderFilterSummary();
});
document.querySelector("#filter-summary-list").addEventListener("click", (event) => {
  const removeButton = event.target.closest("[data-remove-filter-mode]");
  if (removeButton) removeDraftFilter(removeButton);
});
document.querySelector("#filters-form").addEventListener("submit", (event) => {
  event.preventDefault();
  applyDraftFilters();
});
document.querySelector("#clear-filters").addEventListener("click", () => {
  draftFilters = { include: { game_statuses: [], engines: [], tags: [], personal_statuses: [] }, exclude: { game_statuses: [], engines: [], tags: [], personal_statuses: [] } };
  for (const [category, config] of Object.entries(filterCategories)) {
    if (config.searchId) document.querySelector(config.searchId).value = "";
    renderLibraryFilterOptions(category);
  }
  renderFilterSummary();
});
document.querySelector("#cancel-filters").addEventListener("click", () => filtersModal.close());
document.querySelector("#filters-modal").addEventListener("click", (event) => {
  if (event.target === filtersModal) filtersModal.close();
});
document.querySelector("#sort-select").addEventListener("change", (event) => {
  state.sort = event.currentTarget.value;
  state.direction = state.sort === "name" ? "asc" : "desc";
  renderSortDirection();
  loadGames().catch((error) => showNotice(error.message, true));
});
document.querySelector("#sort-direction").addEventListener("click", () => {
  state.direction = state.direction === "asc" ? "desc" : "asc";
  renderSortDirection();
  loadGames().catch((error) => showNotice(error.message, true));
});
document.querySelectorAll(".close-modal").forEach((button) => button.addEventListener("click", () => addModal.close()));
document.querySelectorAll(".close-settings").forEach((button) => button.addEventListener("click", () => settingsModal.close()));
document.querySelectorAll(".close-metadata").forEach((button) => button.addEventListener("click", () => metadataModal.close()));
document.querySelector("#settings-login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const username = document.querySelector("#f95-username").value;
  const passwordInput = document.querySelector("#f95-password");
  const errorElement = document.querySelector("#auth-error");
  const button = document.querySelector("#login-button");
  button.disabled = true;
  button.textContent = "Accesso...";
  errorElement.classList.add("hidden");
  try {
    await api("/api/settings/login", {
      method: "POST",
      body: JSON.stringify({ username, password: passwordInput.value }),
    });
    passwordInput.value = "";
    await refreshAuthStatus();
    showNotice("Account F95Zone collegato. Ora puoi sincronizzare i thread.");
  } catch (error) {
    errorElement.textContent = error.message;
    errorElement.classList.remove("hidden");
  } finally {
    passwordInput.value = "";
    button.disabled = false;
    button.textContent = "Accedi a F95Zone";
  }
});
document.querySelector("#logout-button").addEventListener("click", async () => {
  const button = document.querySelector("#logout-button");
  button.disabled = true;
  try {
    await api("/api/settings/auth", { method: "DELETE" });
    await refreshAuthStatus();
    showNotice("Sessione F95Zone rimossa da questa app.");
  } catch (error) {
    const errorElement = document.querySelector("#auth-error");
    errorElement.textContent = error.message;
    errorElement.classList.remove("hidden");
  } finally {
    button.disabled = false;
  }
});
document.querySelectorAll(".status-nav .nav-item").forEach((button) => button.addEventListener("click", () => {
  document.querySelector(".nav-item.active")?.classList.remove("active");
  button.classList.add("active");
  state.status = button.dataset.status;
  switchPage("library");
  hideNotice();
  loadGames().catch((error) => showNotice(error.message, true));
  renderSortDirection();
}));
document.querySelector("#catalog-nav-button").addEventListener("click", () => switchPage("catalog"));
document.querySelector("#catalog-back-button").addEventListener("click", () => switchPage("library"));
document.querySelector("#catalog-search").addEventListener("input", (event) => {
  state.catalog.search = event.target.value.trim();
  state.catalog.page = 1;
  clearTimeout(catalogSearchTimer);
  catalogSearchTimer = setTimeout(() => loadCatalog().catch(showCatalogError), 250);
});
document.querySelector("#catalog-sort").addEventListener("change", (event) => {
  state.catalog.sort = event.currentTarget.value;
  state.catalog.page = 1;
  loadCatalog().catch(showCatalogError);
});
document.querySelector("#catalog-prev").addEventListener("click", () => {
  if (state.catalog.page > 1) {
    state.catalog.page -= 1;
    loadCatalog().catch(showCatalogError);
  }
});
document.querySelector("#catalog-next").addEventListener("click", () => {
  if (state.catalog.page < state.catalog.totalPages) {
    state.catalog.page += 1;
    loadCatalog().catch(showCatalogError);
  }
});
document.querySelector("#catalog-filters-button").addEventListener("click", () => {
  openCatalogFilters().catch(showCatalogError);
});
document.querySelector("#catalog-engine-search").addEventListener("input", () => renderCatalogFilterOptions("engines"));
document.querySelector("#catalog-other-search").addEventListener("input", () => renderCatalogFilterOptions("others"));
document.querySelector("#catalog-status-search").addEventListener("input", () => renderCatalogFilterOptions("statuses"));
document.querySelector("#catalog-tag-search").addEventListener("input", () => renderCatalogFilterOptions("tags"));
document.querySelector("#catalog-exclude-tag-search").addEventListener("input", () => renderCatalogFilterOptions("excludeTags"));
document.querySelector("#catalog-filter-search").addEventListener("input", () => {
  if (!catalogDraftFilters) return;
  const value = document.querySelector("#catalog-filter-search").value.trim();
  if (document.querySelector("#catalog-search-mode").value === "creator") catalogDraftFilters.creator = value;
  else catalogDraftFilters.titleSearch = value;
  renderCatalogFilterSummary();
});
document.querySelector("#catalog-search-mode").addEventListener("change", (event) => {
  const input = document.querySelector("#catalog-filter-search");
  input.value = event.currentTarget.value === "creator" ? catalogDraftFilters.creator : catalogDraftFilters.titleSearch;
  renderCatalogFilterSummary();
});
document.querySelector("#catalog-date-limit").addEventListener("input", (event) => {
  const days = catalogDateLimits[Number(event.currentTarget.value)];
  document.querySelector("#catalog-date-label").textContent = catalogDateLabel(days);
  if (catalogDraftFilters) {
    catalogDraftFilters.date = days;
    renderCatalogFilterSummary();
  }
});
document.querySelector("#catalog-ignored").addEventListener("change", (event) => {
  if (!catalogDraftFilters) return;
  catalogDraftFilters.ignored = event.currentTarget.checked;
  renderCatalogFilterSummary();
});
document.querySelector("#catalog-filters-form").addEventListener("click", (event) => {
  const clear = event.target.closest("[data-catalog-clear]");
  if (clear) {
    if (clear.dataset.catalogClear === "creator") {
      catalogDraftFilters.creator = "";
      if (document.querySelector("#catalog-search-mode").value === "creator") {
        document.querySelector("#catalog-filter-search").value = "";
      }
    } else if (clear.dataset.catalogClear === "title") {
      catalogDraftFilters.titleSearch = "";
      if (document.querySelector("#catalog-search-mode").value === "title") document.querySelector("#catalog-filter-search").value = "";
    } else if (clear.dataset.catalogClear === "date") {
      catalogDraftFilters.date = 0;
      document.querySelector("#catalog-date-limit").value = "0";
      document.querySelector("#catalog-date-label").textContent = catalogDateLabel(0);
    } else {
      catalogDraftFilters.ignored = false;
      document.querySelector("#catalog-ignored").checked = false;
    }
    renderCatalogFilterSummary();
    return;
  }
  const remove = event.target.closest("[data-catalog-remove]");
  if (remove) {
    removeCatalogDraftFilter(remove);
    return;
  }
  const prefixPick = event.target.closest("[data-catalog-pick-kind]");
  if (prefixPick) {
    const kind = prefixPick.dataset.catalogPickKind;
    const includeKey = kind === "engines" ? "engines" : kind === "statuses" ? "statuses" : "prefixes";
    const excludeKey = kind === "engines" ? "excludeEngines" : kind === "statuses" ? "excludeStatuses" : "excludePrefixes";
    const selectedKey = prefixPick.dataset.catalogPickMode === "include" ? includeKey : excludeKey;
    const otherKey = selectedKey === includeKey ? excludeKey : includeKey;
    const id = Number(prefixPick.dataset.catalogPickId);
    catalogDraftFilters[otherKey] = catalogDraftFilters[otherKey].filter((value) => value !== id);
    catalogDraftFilters[selectedKey] = catalogDraftFilters[selectedKey].includes(id)
      ? catalogDraftFilters[selectedKey].filter((value) => value !== id)
      : [...catalogDraftFilters[selectedKey], id];
    renderCatalogFilterOptions(kind);
    renderCatalogFilterSummary();
    return;
  }
  const action = event.target.closest("[data-catalog-action]");
  if (!action) return;
  const kind = action.dataset.catalogAction === "exclude-tag" ? "excludeTags" : "tags";
  addCatalogDraftFilter(kind, action.dataset.catalogAction.startsWith("include") ? "include" : "exclude");
});
document.querySelector("#catalog-filters-form").addEventListener("submit", (event) => {
  event.preventDefault();
  applyCatalogDraftFilters();
});
document.querySelector("#catalog-filters-close").addEventListener("click", () => catalogFiltersModal.close());
document.querySelector("#catalog-filters-cancel").addEventListener("click", () => catalogFiltersModal.close());
document.querySelector("#catalog-filters-clear").addEventListener("click", () => {
  catalogDraftFilters = {
    creator: "", searchMode: "creator", titleSearch: "", ignored: false, date: 0,
    prefixes: [], excludePrefixes: [], engines: [], excludeEngines: [], statuses: [], excludeStatuses: [],
    tags: [], excludeTags: [], prefixType: "and", tagType: "or",
  };
  document.querySelector("#catalog-filter-search").value = "";
  document.querySelector("#catalog-search-mode").value = "creator";
  document.querySelector("#catalog-ignored").checked = false;
  document.querySelector("#catalog-date-limit").value = "0";
  document.querySelector("#catalog-date-label").textContent = catalogDateLabel(0);
  document.querySelector("#catalog-prefix-mode").value = "and";
  document.querySelector("#catalog-tag-mode").value = "or";
  renderCatalogFilterOptions("engines");
  renderCatalogFilterOptions("others");
  renderCatalogFilterOptions("statuses");
  renderCatalogFilterOptions("tags");
  renderCatalogFilterOptions("excludeTags");
  renderCatalogFilterSummary();
});
document.querySelector("#catalog-refresh-filters").addEventListener("click", async () => {
  const button = document.querySelector("#catalog-refresh-filters");
  button.disabled = true;
  document.querySelector("#catalog-filter-counts").textContent = "Aggiornamento dei filtri da SAM...";
  document.querySelector("#catalog-filters-error").classList.add("hidden");
  try {
    catalogFilterOptions = await api("/api/catalog/filters?refresh=1");
    renderLoadedCatalogFilters();
  } catch (error) {
    const errorElement = document.querySelector("#catalog-filters-error");
    errorElement.textContent = error.message || String(error);
    errorElement.classList.remove("hidden");
    document.querySelector("#catalog-filter-counts").textContent = "Filtri SAM non disponibili";
  } finally {
    button.disabled = false;
  }
});
document.querySelector("#search-input").addEventListener("input", (event) => {
  state.search = event.target.value.trim();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadGames().catch((error) => showNotice(error.message, true)), 180);
});
document.querySelector("#add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.querySelector("#submit-add");
  const errorElement = document.querySelector("#form-error");
  button.disabled = true;
  button.textContent = "Aggiunta...";
  errorElement.classList.add("hidden");
  try {
    const form = new FormData(event.currentTarget);
    await api("/api/games", {
      method: "POST",
      body: JSON.stringify({ thread_url: form.get("thread_url"), notes: form.get("notes") }),
    });
    addModal.close();
    state.status = "all";
    document.querySelector('[data-status="all"]').click();
    showNotice("Gioco aggiunto. Sto importando i metadati del thread.");
  } catch (error) {
    errorElement.textContent = error.message;
    errorElement.classList.remove("hidden");
  } finally {
    button.disabled = false;
    button.textContent = "Aggiungi alla libreria";
  }
});
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    document.querySelector("#search-input").focus();
  }
  if (event.key === "Escape" && detailModal.open) detailModal.close();
});
detailModal.addEventListener("click", (event) => {
  if (event.target === detailModal) detailModal.close();
});

loadGames().catch((error) => showNotice(error.message, true));
api("/api/filters")
  .then((options) => populateQuickStatus(options.game_statuses))
  .catch((error) => showNotice(`Impossibile caricare i filtri: ${error.message}`, true));
setInterval(() => loadGames().catch((error) => showNotice(error.message, true)), 15000);
pollSyncProgress();
setInterval(pollSyncProgress, 1000);

let devVersion;
async function checkForDevelopmentChanges() {
  try {
    const response = await fetch("/api/dev/version", { cache: "no-store" });
    if (!response.ok) return;
    const status = await response.json();
    if (!status.dev) return;
    if (devVersion !== undefined && status.version !== devVersion) {
      window.location.reload();
      return;
    }
    devVersion = status.version;
  } catch {
    // The development server may restart while Python changes are being applied.
  }
}
checkForDevelopmentChanges();
setInterval(checkForDevelopmentChanges, 1500);
