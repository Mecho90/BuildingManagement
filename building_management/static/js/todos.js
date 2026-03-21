(function () {
  const configEl = document.getElementById("todo-config");
  if (!configEl) return;
  const config = JSON.parse(configEl.textContent);

  const defaultPageSize =
    Number(config.defaultPageSize) ||
    (Array.isArray(config.pageSizeOptions) && config.pageSizeOptions.length ? Number(config.pageSizeOptions[0]) : 25) ||
    25;

  const state = {
    currentTab: "this-week",
    cache: {},
    undo: null,
    calendar: {
      current: config.today ? new Date(config.today) : new Date(),
      events: [],
    },
    pagination: {
      "this-week": { page: 1, per: defaultPageSize },
    },
    meta: {},
    pageSize: defaultPageSize,
    search: "",
    owner: config.ownerFilterDefault || "",
    completedSelection: new Set(),
    lastResults: [],
  };

  const els = {
    list: document.getElementById("todo-list"),
    empty: document.getElementById("todo-empty"),
    emptyText: document.getElementById("todo-empty-text"),
    filterReset: document.getElementById("todo-filter-reset"),
    liveRegion: document.getElementById("todo-live-region"),
    statsDueToday: document.getElementById("todo-stat-due-today"),
    statsOverdue: document.getElementById("todo-stat-overdue"),
    statsDueNextWeek: document.getElementById("todo-stat-due-next-week"),
    statsCompleted: document.getElementById("todo-stat-completed"),
    filterChips: document.getElementById("todo-filter-chips"),
    filterForm: document.getElementById("todo-filter-form"),
    bulkBar: document.getElementById("todo-bulk-bar"),
    bulkCount: document.getElementById("todo-bulk-count"),
    bulkMarkDone: document.getElementById("todo-bulk-mark-done"),
    bulkDelete: document.getElementById("todo-bulk-delete"),
    completedDeleteBtn: document.getElementById("todo-completed-delete-selected"),
    completedSelectAll: document.getElementById("todo-completed-select-all"),
    completedSelectAllWrap: document.getElementById("todo-completed-select-all-wrap"),
    calendarGrid: document.getElementById("todo-calendar-grid") || document.getElementById("todo-calendar"),
    calendarMonth: document.getElementById("todo-calendar-month"),
    calendarPrev: document.getElementById("todo-calendar-prev"),
    calendarNext: document.getElementById("todo-calendar-next"),
    calendarToday: document.getElementById("todo-calendar-today"),
    calendarStatus: document.getElementById("todo-calendar-status"),
    undoBox: document.getElementById("todo-undo"),
    undoLabel: document.getElementById("todo-undo-label"),
    undoBtn: document.getElementById("todo-undo-btn"),
    refreshBtn: document.getElementById("todo-refresh"),
    pageSizeSelect: document.getElementById("todo-page-size"),
    filterApply: document.getElementById("todo-filter-apply"),
    searchInput: document.getElementById("todo-search"),
    ownerFilter: document.getElementById("todo-owner-filter"),
    pagination: document.getElementById("todo-pagination"),
    paginationTop: document.getElementById("todo-pagination-top"),
    paginationLabel: document.getElementById("todo-pagination-label"),
    paginationPage: document.getElementById("todo-pagination-page"),
    paginationPrev: document.getElementById("todo-pagination-prev"),
    paginationNext: document.getElementById("todo-pagination-next"),
    paginationBlocks: document.querySelectorAll("[data-todo-pagination]"),
  };

  const tabs = document.querySelectorAll(".todo-tab");

  const t = (msg) => (window.gettext ? gettext(msg) : msg);
  const isoToday = config.today || new Date().toISOString().slice(0, 10);

  function announce(message) {
    if (!els.liveRegion) return;
    els.liveRegion.textContent = message || "";
  }

  function escapeHtml(value) {
    if (value === null || value === undefined) return "";
    return String(value).replace(/[&<>"']/g, (ch) => {
      switch (ch) {
        case "&":
          return "&amp;";
        case "<":
          return "&lt;";
        case ">":
          return "&gt;";
        case '"':
          return "&quot;";
        case "'":
          return "&#39;";
        default:
          return ch;
      }
    });
  }

  const STATUS_BADGES = {
    pending: "bg-blue-100 text-blue-900 border border-blue-200",
    in_progress: "bg-amber-100 text-amber-800 border border-amber-200",
    done: "bg-emerald-100 text-emerald-800 border border-emerald-200",
  };
  const CALENDAR_BADGE_OVERRIDES = {
    in_progress: "background:#fef3c7;border-color:#fcd34d;color:#92400e;",
    done: "background:#d1fae5;border-color:#34d399;color:#065f46;text-decoration:line-through;text-decoration-thickness:2px;text-decoration-color:#059669;",
  };

  function pendingBadgeStyle() {
    const isDark = document.documentElement.classList.contains("dark");
    if (isDark) {
      return "background:rgba(59,130,246,0.35);border-color:rgba(96,165,250,0.9);color:#dbeafe;box-shadow:0 0 12px rgba(59,130,246,0.5);";
    }
    return "background:#e0f2fe;border-color:#93c5fd;color:#1d4ed8;";
  }

  function badgeStatusClass(status) {
    if (status === "done") return "todo-calendar-badge--done";
    if (status === "in_progress") return "todo-calendar-badge--progress";
    return "todo-calendar-badge--pending";
  }

  function cardStatusClass(status) {
    if (status === "done") return "todo-card todo-card--done";
    if (status === "in_progress") return "todo-card todo-card--in-progress";
    return "todo-card todo-card--pending";
  }

  const TAB_QUERIES = {
    "this-week": () => ({ week_start: config.currentWeek }),
    upcoming: () => ({ include_history: 1, upcoming: 1, status: "pending,in_progress" }),
    all: () => ({ include_history: 1, created_only: 1, status: "pending,in_progress" }),
    created: () => ({ include_history: 1, created_only: 1, status: "pending,in_progress" }),
    completed: () => ({ week_start: config.currentWeek, status: "done", history: 1 }),
    history: () => ({ status: "done", history: 1 }),
  };

  function getPaginationState(tab) {
    if (!state.pagination[tab]) {
      state.pagination[tab] = { page: 1, per: state.pageSize };
    } else {
      state.pagination[tab].per = state.pageSize;
    }
    return state.pagination[tab];
  }

  function resetPagination() {
    state.pagination = {};
    state.meta = {};
  }

  const completedDeleteBtn = els.completedDeleteBtn;

  function visibleTaskCheckboxes() {
    if (!els.list) return [];
    return Array.from(els.list.querySelectorAll("input[data-task-select]"));
  }

  function updateBulkControls() {
    const count = state.completedSelection.size;
    if (els.bulkBar) {
      els.bulkBar.classList.toggle("hidden", count === 0);
    }
    if (els.bulkCount) {
      const noun = count === 1 ? t("task selected") : t("tasks selected");
      els.bulkCount.textContent = `${count} ${noun}`;
    }
    if (els.bulkMarkDone) {
      els.bulkMarkDone.disabled = count === 0;
    }
    if (els.bulkDelete) {
      els.bulkDelete.disabled = count === 0;
    }
    const onCompletedTab = state.currentTab === "completed";
    if (completedDeleteBtn) {
      completedDeleteBtn.classList.toggle("hidden", !onCompletedTab);
      completedDeleteBtn.disabled = !onCompletedTab || count === 0;
      completedDeleteBtn.textContent = count > 0 ? `${t("Delete selected")} (${count})` : t("Delete selected");
    }
    if (els.completedSelectAllWrap) {
      els.completedSelectAllWrap.classList.toggle("hidden", !onCompletedTab);
      els.completedSelectAllWrap.classList.toggle("inline-flex", onCompletedTab);
    }
    if (els.completedSelectAll) {
      const visible = visibleTaskCheckboxes();
      const totalVisible = visible.length;
      const checkedVisible = visible.filter((el) => el.checked).length;
      const isAll = totalVisible > 0 && checkedVisible === totalVisible;
      els.completedSelectAll.checked = isAll;
      els.completedSelectAll.indeterminate = checkedVisible > 0 && checkedVisible < totalVisible;
      els.completedSelectAll.disabled = !onCompletedTab || totalVisible === 0;
    }
  }

  function clearCompletedSelection() {
    state.completedSelection.clear();
    updateBulkControls();
  }

  function detailUrl(id) {
    return config.detailUrl.replace("{id}", id);
  }

  function listUrlWithCurrentTab() {
    const base = config.listUrl || window.location.pathname;
    const url = new URL(base, window.location.origin);
    if (state.currentTab) {
      url.searchParams.set("tab", state.currentTab);
    }
    return `${url.pathname}${url.search}`;
  }

  function csrftoken() {
    const input = document.querySelector("input[name='csrfmiddlewaretoken']");
    return input ? input.value : "";
  }

  function relativeTime(value) {
    if (!value) return "";
    const locale = config.locale || "en";
    const formatter = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
    const now = Date.now();
    const then = new Date(value).getTime();
    const diffMs = then - now;
    const diffMinutes = Math.round(diffMs / 60000);
    const absMinutes = Math.abs(diffMinutes);
    if (absMinutes < 60) return formatter.format(diffMinutes, "minute");
    const diffHours = Math.round(diffMinutes / 60);
    if (Math.abs(diffHours) < 24) return formatter.format(diffHours, "hour");
    const diffDays = Math.round(diffHours / 24);
    return formatter.format(diffDays, "day");
  }

  function formatDate(value, opts) {
    if (!value) return "";
    const date = new Date(value);
    return new Intl.DateTimeFormat(config.locale || "en", opts || { weekday: "short", month: "short", day: "numeric" }).format(date);
  }

  function calendarDateString(date) {
    const d = typeof date === "string" ? new Date(date) : date;
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function calendarIcsDate(date) {
    const d = typeof date === "string" ? new Date(date) : date;
    return `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
  }

  function startOfWeek(date) {
    const d = new Date(date);
    const day = (d.getDay() + 6) % 7; // Monday start
    d.setDate(d.getDate() - day);
    d.setHours(0, 0, 0, 0);
    return d;
  }

  const ACTIVE_TAB_CLASSES = ["nav-link--active"];
  const INACTIVE_TAB_CLASSES = [];

  function setTabActive(targetTab) {
    tabs.forEach((btn) => {
      const isActive = btn.dataset.tab === targetTab;
      btn.classList.remove("btn", "btn-secondary", "active", "nav-link--active");
      btn.classList.add("nav-link");
      ACTIVE_TAB_CLASSES.forEach((cls) => btn.classList.toggle(cls, isActive));
      btn.setAttribute("aria-pressed", isActive ? "true" : "false");
    });
    clearCompletedSelection();
    updateBulkControls();
  }

  function showEmpty(show) {
    if (!els.empty) return;
    els.empty.classList.toggle("hidden", !show);
  }

  function hasActiveFilters() {
    const ownerDefault = config.ownerFilterDefault || "";
    return Boolean(state.search) || state.pageSize !== defaultPageSize || (state.owner || "") !== ownerDefault;
  }

  function renderFilterChips() {
    if (!els.filterChips) return;
    const chips = [];
    if (state.search) {
      chips.push({ key: "search", label: `${t("Search")}: ${state.search}` });
    }
    if (state.owner && state.owner !== "all") {
      const owner = (config.ownerOptions || []).find((opt) => String(opt.value) === String(state.owner));
      const ownerLabel = owner ? owner.label : state.owner;
      chips.push({ key: "owner", label: `${t("Owner")}: ${ownerLabel}` });
    }
    if (state.currentTab === "completed") {
      chips.push({ key: "status", label: `${t("Status")}: ${t("Completed")}` });
    } else if (state.currentTab === "this-week") {
      chips.push({ key: "status", label: `${t("Status")}: ${t("Pending")}` });
    }
    if (state.pageSize !== defaultPageSize) {
      chips.push({ key: "per", label: `${t("Page size")}: ${state.pageSize}` });
    }
    if (!chips.length) {
      els.filterChips.innerHTML = "";
      els.filterChips.classList.add("hidden");
      return;
    }
    els.filterChips.classList.remove("hidden");
    els.filterChips.innerHTML = chips
      .map(
        (chip) =>
          `<span class="todo-chip">${escapeHtml(chip.label)} <button type="button" data-chip-remove="${chip.key}" aria-label="${t("Remove filter")}">×</button></span>`
      )
      .join("");
  }

  function renderStats(summary) {
    const dueToday = Number(summary && summary.due_today) || 0;
    const overdue = Number(summary && summary.overdue) || 0;
    const dueNextWeek = Number(summary && summary.due_next_7_days) || 0;
    const completed = Number(summary && summary.completed) || 0;
    if (els.statsDueToday) els.statsDueToday.textContent = String(dueToday);
    if (els.statsOverdue) els.statsOverdue.textContent = String(overdue);
    if (els.statsDueNextWeek) els.statsDueNextWeek.textContent = String(dueNextWeek);
    if (els.statsCompleted) els.statsCompleted.textContent = String(completed);
  }

  function fetchTaskSummary() {
    if (!config.summaryUrl) return Promise.resolve();
    return fetch(config.summaryUrl, {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then((response) => {
        if (!response.ok) throw new Error(t("Failed to load task summary"));
        return response.json();
      })
      .then((summary) => {
        renderStats(summary);
      })
      .catch(() => {
        renderStats(null);
      });
  }

  function renderEmptyState(totalCount) {
    if (!els.emptyText) return;
    if (hasActiveFilters()) {
      els.emptyText.textContent = t("No tasks match your filters.");
      return;
    }
    if (totalCount === 0) {
      els.emptyText.textContent = t("No tasks yet. Create your first task to get started.");
      return;
    }
    els.emptyText.textContent = t("No tasks in this view.");
  }

  function resetAllFiltersAndReload() {
    state.search = "";
    state.owner = config.ownerFilterDefault || "";
    state.pageSize = defaultPageSize;
    if (els.searchInput) els.searchInput.value = "";
    if (els.ownerFilter) els.ownerFilter.value = state.owner || "all";
    if (els.pageSizeSelect) els.pageSizeSelect.value = String(defaultPageSize);
    resetPagination();
    fetchTasks(state.currentTab);
  }

  function renderTasks(tab, tasks) {
    const activeStatuses = ["pending", "in_progress"];
    const activeTasks = tab === "this-week" ? tasks.filter((t) => activeStatuses.includes(t.status)) : tasks;
    state.lastResults = activeTasks;
    const cards = activeTasks.map(renderCard).join("");
    els.list.innerHTML = cards;
    showEmpty(activeTasks.length === 0);
    updateBulkControls();
  }

  function renderPagination(tab, pagination, visibleCount, totalCount) {
    if (!els.paginationBlocks || !els.paginationBlocks.length) return;
    if (!pagination) {
      els.paginationBlocks.forEach((block) => block.classList.add("hidden"));
      return;
    }
    state.pagination[tab] = { page: pagination.page, per: pagination.per };
    if (pagination.pages <= 1 && totalCount <= pagination.per) {
      els.paginationBlocks.forEach((block) => block.classList.add("hidden"));
      return;
    }
    const start = visibleCount ? (pagination.page - 1) * pagination.per + 1 : 0;
    const end = visibleCount ? (pagination.page - 1) * pagination.per + visibleCount : 0;
    const summary = visibleCount ? `${t("Showing")} ${start}–${end} ${t("of")} ${totalCount}` : t("No tasks to show.");
    els.paginationBlocks.forEach((block) => {
      const label = block.querySelector("[data-pagination-label]");
      const page = block.querySelector("[data-pagination-page]");
      const prev = block.querySelector("[data-pagination-prev]");
      const next = block.querySelector("[data-pagination-next]");
      const jumpWrap = block.querySelector("[data-pagination-jump-wrap]");
      const jumpInput = block.querySelector("[data-pagination-jump-input]");
      if (label) label.textContent = summary;
      if (page) page.textContent = `${t("Page")} ${pagination.page} / ${pagination.pages}`;
      if (prev) prev.disabled = !pagination.has_previous;
      if (next) next.disabled = !pagination.has_next;
      if (jumpWrap) {
        const showJump = pagination.pages > 7;
        jumpWrap.classList.toggle("hidden", !showJump);
        jumpWrap.classList.toggle("inline-flex", showJump);
      }
      if (jumpInput) {
        jumpInput.value = String(pagination.page);
        jumpInput.max = String(pagination.pages);
      }
      block.classList.remove("hidden");
    });
    announce(summary);
  }

  function deleteCompletedTasks() {
    if (!completedDeleteBtn) return;
    if (state.currentTab !== "completed") return;
    const selectedIds = Array.from(state.completedSelection);
    if (!selectedIds.length) return;
    const confirmMessage = completedDeleteBtn.dataset.confirm || t("Delete completed tasks?");
    if (!window.confirm(confirmMessage)) return;
    const params = new URLSearchParams();
    params.set("ids", selectedIds.join(","));
    if (state.owner) {
      params.set("owner", state.owner);
    }
    const url = params.toString() ? `${config.completedClearUrl}?${params.toString()}` : config.completedClearUrl;
    fetch(url, {
      method: "DELETE",
      headers: {
        "X-CSRFToken": csrftoken(),
      },
      credentials: "same-origin",
    })
      .then((response) => {
        if (!response.ok) {
          throw new Error(t("Failed to delete tasks"));
        }
        return response.json();
      })
      .then(() => {
        clearCompletedSelection();
        fetchTasks(state.currentTab === "completed" ? "completed" : "this-week");
        fetchCalendar();
      })
      .catch(showError);
  }

  function selectedIds() {
    return Array.from(state.completedSelection).filter(Boolean);
  }

  function runBulkPatch(payload) {
    const ids = selectedIds();
    if (!ids.length) return Promise.resolve();
    return Promise.all(ids.map((id) => updateTask(id, payload)));
  }

  function runBulkDelete() {
    const ids = selectedIds();
    if (!ids.length) return Promise.resolve();
    return Promise.all(
      ids.map((id) =>
        fetch(detailUrl(id), {
          method: "DELETE",
          headers: {
            "X-CSRFToken": csrftoken(),
          },
          credentials: "same-origin",
        }).then((response) => {
          if (response.status === 204 || response.ok) return null;
          return response.json().then((data) => {
            throw new Error(data.error || t("Request failed"));
          });
        })
      )
    );
  }

  function calendarLinks(item) {
    return "";
  }

  function todayDate() {
    return new Date(`${isoToday}T00:00:00`);
  }

  function relativeDaysLabel(value) {
    if (!value) return "";
    const due = new Date(`${value}T00:00:00`);
    const today = todayDate();
    const diff = Math.round((due.getTime() - today.getTime()) / 86400000);
    if (diff < 0) {
      const days = Math.abs(diff);
      return days === 1 ? t("1 day late") : `${days} ${t("days late")}`;
    }
    if (diff === 0) {
      return t("Due today");
    }
    if (diff === 1) {
      return t("Due tomorrow");
    }
    return `${diff} ${t("days left")}`;
  }

  function priorityFor(item) {
    const due = item.due_date;
    if (!due) return { level: "low", icon: "●", label: t("Low") };
    const dueDate = new Date(`${due}T00:00:00`);
    const today = todayDate();
    const diff = Math.round((dueDate.getTime() - today.getTime()) / 86400000);
    if (diff < 0) return { level: "high", icon: "!", label: t("High") };
    if (diff <= 1) return { level: "medium", icon: "•", label: t("Medium") };
    return { level: "low", icon: "●", label: t("Low") };
  }

  function statusOptions(selected) {
    const allowed = Object.keys(config.statusLabels || {}).filter((code) => code !== "archived");
    return allowed
      .map((code) => {
        const selectedAttr = code === selected ? " selected" : "";
        return `<option value="${escapeHtml(code)}"${selectedAttr}>${escapeHtml(config.statusLabels[code] || code)}</option>`;
      })
      .join("");
  }

  function renderCard(item) {
    const status = item.status || "pending";
    const safeTitle = escapeHtml(item.title || "");
    const statusLabel = config.statusLabels[status] || status;
    const badgeClass = STATUS_BADGES[status] || STATUS_BADGES.pending;
    const badgeStyle =
      status === "pending"
        ? "background:#e0f2fe;border:1px solid #93c5fd;color:#1d4ed8;"
        : status === "in_progress"
        ? "background:#fef3c7;border:1px solid #fcd34d;color:#92400e;"
        : status === "done"
        ? "background:#d1fae5;border:1px solid #34d399;color:#065f46;"
        : "";
    const dueLabel = item.due_date ? formatDate(item.due_date) : formatDate(item.week_start);
    const action = status === "done" ? { label: t("Reopen"), next: "pending" } : { label: t("Mark done"), next: "done" };
    const editUrl = config.editUrl ? config.editUrl.replace("{id}", item.id) : null;
    const descriptionBlock = item.description
      ? `<p class="todo-card__description">${escapeHtml(item.description)}</p>`
      : "";
    const deleteUrl = config.deleteUrl ? config.deleteUrl.replace("{id}", item.id) : null;
    const deleteTarget =
      deleteUrl && (config.listUrl || window.location.pathname)
        ? `${deleteUrl}?next=${encodeURIComponent(listUrlWithCurrentTab())}`
        : deleteUrl;
    const ownerName = item.owner && item.owner.name ? escapeHtml(item.owner.name) : "";
    const showOwner = ownerName && item.owner.id !== config.currentUserId;
    const ownerBadge = showOwner ? `<span class="todo-card__owner">${ownerName}</span>` : "";
    const isSelected = state.completedSelection.has(String(item.id));
    const selectionControl = `<label class="inline-flex items-center gap-2 text-xs font-semibold text-slate-600 dark:text-slate-300"><input type="checkbox" data-task-select value="${item.id}" ${isSelected ? "checked" : ""} class="h-4 w-4 rounded border-slate-300 text-rose-600 focus:ring-rose-500 dark:border-slate-600 dark:bg-slate-900"/>${t("Select")}</label>`;
    const completedMeta = item.completed_at
      ? `<span class="inline-flex items-center gap-1"><span class="font-semibold">${t("Completed")}:</span> ${relativeTime(item.completed_at)}</span>`
      : "";
    const dueRelative = item.due_date ? relativeDaysLabel(item.due_date) : "";
    const isOverdue = dueRelative.includes(t("late"));
    const dueMetaClass = isOverdue ? "todo-card__due--overdue" : "";
    const priority = priorityFor(item);
    const priorityBadge = `<span class="todo-card__priority todo-card__priority--${priority.level}" title="${t("Priority")}">${priority.icon} ${priority.label}</span>`;
    const cardClass = cardStatusClass(status);
    const quickStatus = `
      <label>${t("Status")}
        <select class="input" data-inline-status>
          ${statusOptions(status)}
        </select>
      </label>`;
    const quickDue = `
      <label>${t("Due date")}
        <input type="date" class="input" data-inline-due min="${isoToday}" value="${item.due_date || ""}" />
      </label>`;

    return `
      <article class="${cardClass}" data-todo-id="${item.id}" data-title="${safeTitle}" data-status="${status}" data-date="${item.due_date || item.week_start || ""}">
        <div class="todo-card__body">
          <div class="todo-card__header">
            <div class="todo-card__title-wrap">
              <h3 class="todo-card__title">${safeTitle}</h3>
              <span class="todo-card__status ${badgeClass}" style="${badgeStyle}">${statusLabel}</span>
              ${priorityBadge}
              ${ownerBadge}
            </div>
          </div>
          ${descriptionBlock}
          <div class="todo-card__meta">
            ${completedMeta}
          </div>
          <div class="todo-card__quick-edit">
            ${quickStatus}
            ${quickDue}
          </div>
        </div>
        <div class="todo-card__actions">
          ${selectionControl}
          <button class="btn btn-sm" data-action="complete" data-next-status="${action.next}">${action.label}</button>
          ${editUrl ? `<a class="btn btn-secondary btn-sm" href="${editUrl}">${t("Edit")}</a>` : ""}
          ${deleteTarget ? `<a class="btn btn-danger btn-sm" href="${deleteTarget}">${t("Delete")}</a>` : ""}
        </div>
      </article>
    `;
  }

  function calendarRange(date) {
    const currentMonth = date.getMonth();
    const currentYear = date.getFullYear();
    const monthStart = startOfWeek(new Date(currentYear, currentMonth, 1));
    const endOfMonth = new Date(currentYear, currentMonth + 1, 0);
    const weeks = [];
    let cursor = new Date(monthStart);
    let finished = false;
    while (!finished) {
      const row = [];
      for (let j = 0; j < 7; j += 1) {
        row.push(new Date(cursor));
        cursor.setDate(cursor.getDate() + 1);
      }
      weeks.push(row);
      finished = row.some((day) => day.getMonth() === currentMonth && day.getDate() === endOfMonth.getDate());
    }
    return {
      monthLabel: new Intl.DateTimeFormat(config.locale || "en", { month: "long", year: "numeric" }).format(new Date(currentYear, currentMonth, 1)),
      weeks,
    };
  }

  function isoWeekNumber(day) {
    const dateCopy = new Date(day);
    dateCopy.setHours(0, 0, 0, 0);
    dateCopy.setDate(dateCopy.getDate() + 3 - ((dateCopy.getDay() + 6) % 7));
    const firstThursday = new Date(dateCopy.getFullYear(), 0, 4);
    return (
      1 +
      Math.round(
        ((dateCopy.getTime() - firstThursday.getTime()) / 86400000 - 3 + ((firstThursday.getDay() + 6) % 7)) /
          7
      )
    );
  }

  function renderCalendar() {
    if (!els.calendarGrid) return;
    const { weeks, monthLabel } = calendarRange(state.calendar.current);
    const highlightSourceDate = config.currentWeek
      ? new Date(`${config.currentWeek}T00:00:00`)
      : config.today
      ? new Date(`${config.today}T00:00:00`)
      : new Date();
    const highlightWeekKey = calendarDateString(startOfWeek(highlightSourceDate));
    const highlightWeekNumber = isoWeekNumber(highlightSourceDate);
    const highlightYear = highlightSourceDate.getFullYear();
    const todayKey = config.today ? calendarDateString(new Date(`${config.today}T00:00:00`)) : null;
    if (els.calendarMonth) {
      els.calendarMonth.textContent = monthLabel;
    }
    const eventMap = state.calendar.events.reduce((map, event) => {
      const key = calendarDateString(event.date);
      if (!map[key]) map[key] = [];
      map[key].push(event);
      return map;
    }, {});

    const dayNames =
      `<th role="columnheader" scope="col" class="bg-emerald-50 dark:bg-slate-800 px-2 py-2 text-center text-xs font-semibold uppercase tracking-wide text-emerald-700 dark:text-slate-200">${t(
        "WK"
      )}</th>` +
      [t("Mon"), t("Tue"), t("Wed"), t("Thu"), t("Fri"), t("Sat"), t("Sun")]
        .map(
          (day) =>
            `<th role="columnheader" scope="col" class="bg-emerald-50 dark:bg-slate-800 px-2 py-2 text-center text-xs font-semibold uppercase tracking-wide text-emerald-700 dark:text-slate-200">${day}</th>`
        )
        .join("");

    const body = weeks
      .map((week) => {
        const weekNum = isoWeekNumber(week[0]);
        const weekStartKey = calendarDateString(startOfWeek(week[0]));
        const currentWeekNumber = isoWeekNumber(week[0]);
        let isActiveWeek =
          weekStartKey === highlightWeekKey ||
          (currentWeekNumber === highlightWeekNumber && week[0].getFullYear() === highlightYear);
        if (!isActiveWeek && todayKey) {
          isActiveWeek = week.some((day) => calendarDateString(day) === todayKey);
        }
        const weekCell = `<th scope="row" class="border border-emerald-50 px-2 py-1 text-center text-xs font-semibold text-slate-500 dark:text-slate-400 ${
          isActiveWeek ? "todo-calendar-week__num--active" : ""
        }">${weekNum}</th>`;
        const cells = week
          .map((day) => {
            const dateKey = calendarDateString(day);
            const isCurrentMonth = day.getMonth() === state.calendar.current.getMonth();
            const events = eventMap[dateKey] || [];
            const count = events.length;
            const isToday = isCurrentMonth && config.today && dateKey === config.today;
            const classes = [
              "h-20 align-top border border-emerald-50 px-2 py-1 text-slate-700 dark:text-slate-100 transition",
              isCurrentMonth
                ? isToday
                  ? "bg-amber-100/90 dark:bg-amber-500/30"
                  : "bg-white/90 dark:bg-transparent"
                : "bg-transparent text-transparent border-transparent pointer-events-none",
              isActiveWeek ? "todo-calendar-week-cell--active" : "",
            ].join(" ");
            if (!isCurrentMonth) {
              return `<td role="presentation" class="${classes}"></td>`;
            }
            const badges = events
              .slice(0, 3)
              .map((event) => {
                const statusClass = badgeStatusClass(event.status);
                const override =
                  event.status === "pending"
                    ? pendingBadgeStyle()
                    : CALENDAR_BADGE_OVERRIDES[event.status] || "";
                const styleAttr = override ? ` style="${override}"` : "";
                return `<span class="todo-calendar-badge ${statusClass}"${styleAttr}>${escapeHtml(event.title)}</span>`;
              })
              .join("");
            const more = count > 3 ? `<span class="text-[10px] text-slate-500">+${count - 3}</span>` : "";
            return `
              <td role="gridcell" tabindex="0" aria-label="${formatDate(dateKey, { weekday: "long", month: "long", day: "numeric" })} (${count} tasks)" class="${classes}" data-date="${dateKey}">
                <button type="button" class="w-full text-left ${isToday ? "text-base font-black text-amber-900 dark:text-amber-100" : "text-sm font-semibold text-slate-700 dark:text-slate-100"}" data-date="${dateKey}">
                  ${day.getDate()}
                </button>
                <div class="flex flex-col" data-date="${dateKey}">${badges}${more}</div>
              </td>
            `;
          })
          .join("");
        return `<tr class="${isActiveWeek ? "todo-calendar-week--active" : ""}">${weekCell}${cells}</tr>`;
      })
      .join("");

    els.calendarGrid.innerHTML = `<div class="overflow-hidden rounded-2xl border border-emerald-100/60 dark:border-slate-700/70 shadow-inner">
      <table class="w-full border-collapse text-xs text-slate-700 dark:text-slate-100" role="grid" aria-label="Task calendar">
        <thead><tr>${dayNames}</tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
  }

  function setCalendarStatus(message) {
    if (!els.calendarStatus) return;
    els.calendarStatus.textContent = message || "";
  }

  function fetchCalendar() {
    if (!config.calendarUrl) return Promise.resolve();
    const current = state.calendar.current;
    const start = startOfWeek(new Date(current.getFullYear(), current.getMonth(), 1));
    const end = new Date(current.getFullYear(), current.getMonth() + 1, 0);
    const params = new URLSearchParams({ start: calendarDateString(start), end: calendarDateString(end) });
    return fetch(`${config.calendarUrl}?${params.toString()}`, { credentials: "same-origin" })
      .then((response) => {
        if (!response.ok) throw new Error(t("Failed to load calendar"));
        return response.json();
      })
      .then((data) => {
        state.calendar.events = data.events || [];
        renderCalendar();
      })
      .catch((error) => setCalendarStatus(error.message));
  }

  function renderDaySummary(dateStr) {
    const events = state.calendar.events.filter((event) => calendarDateString(event.date) === dateStr);
    if (!events.length) {
      setCalendarStatus(`${t("No tasks on")} ${formatDate(dateStr, { weekday: "long", month: "long", day: "numeric" })}.`);
      return;
    }
    const titles = events.map((event) => event.title).join(", ");
    const taskLabel = events.length === 1 ? t("task on") : t("tasks on");
    setCalendarStatus(`${events.length} ${taskLabel} ${formatDate(dateStr, { weekday: "long", month: "long", day: "numeric" })}: ${titles}`);
  }

  function handleCalendarInteraction(event) {
    const cell = event.target.closest("[data-date]");
    if (!cell) return;
    const dateStr = cell.dataset.date;
    renderDaySummary(dateStr);
    if (config.createUrl) {
      const separator = config.createUrl.includes("?") ? "&" : "?";
      window.location.href = `${config.createUrl}${separator}due=${dateStr}`;
    }
  }

  function fetchTasks(tab) {
    state.currentTab = tab;
    setTabActive(tab);
    showEmpty(false);
    announce(t("Calculating…"));
    els.list.innerHTML = `<div class="animate-pulse rounded-2xl border border-dashed border-emerald-200/70 bg-white/70 p-6 text-center text-sm text-slate-500">${t("Loading…")}</div>`;
    const params = new URLSearchParams(TAB_QUERIES[tab] ? TAB_QUERIES[tab]() : {});
    const pageState = getPaginationState(tab);
    params.set("per", pageState.per);
    params.set("page", pageState.page);
    if (state.search) {
      params.set("q", state.search);
    }
    if (state.owner !== undefined && !params.has("owner")) {
      params.set("owner", state.owner || "");
    }
    return fetch(`${config.apiUrl}?${params.toString()}`, {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then((response) => {
        if (!response.ok) throw new Error(t("Failed to load to-dos"));
        return response.json();
      })
      .then((payload) => {
        const results = Array.isArray(payload) ? payload : payload.results || [];
        const pagination = payload.pagination || null;
        const totalCount = typeof payload.count === "number" ? payload.count : results.length;
        state.cache[tab] = results;
        state.meta[tab] = { pagination, count: totalCount };
        renderTasks(tab, results);
        fetchTaskSummary();
        renderFilterChips();
        renderPagination(tab, pagination, state.lastResults.length, totalCount);
        renderEmptyState(totalCount);
      })
      .catch((err) => {
        els.list.innerHTML = `<div class="rounded-2xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">${err.message}</div>`;
        if (els.paginationBlocks && els.paginationBlocks.length) els.paginationBlocks.forEach((block) => block.classList.add("hidden"));
        announce(err.message || t("Failed to load to-dos"));
        delete state.meta[tab];
      });
  }

  function addTask(payload) {
    return fetch(config.apiUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrftoken(),
      },
      credentials: "same-origin",
      body: JSON.stringify(payload),
    }).then(handleApiResponse);
  }

  function updateTask(id, body) {
    return fetch(detailUrl(id), {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrftoken(),
      },
      credentials: "same-origin",
      body: JSON.stringify(body),
    }).then(handleApiResponse);
  }

  function handleApiResponse(response) {
    if (!response.ok) {
      return response.json().then((data) => {
        const message = data.error || t("Request failed");
        throw new Error(message);
      });
    }
    return response.json();
  }

  function handleListClick(event) {
    const actionEl = event.target.closest("[data-action]");
    if (!actionEl) return;
    const card = actionEl.closest("[data-todo-id]");
    if (!card) return;
    const id = card.dataset.todoId;
    const nextStatus = actionEl.dataset.nextStatus;
    if (actionEl.dataset.action === "complete") {
      const previousStatus = card.dataset.status;
      updateTask(id, { status: nextStatus })
        .then(() => {
          state.undo = { id, status: previousStatus, title: card.dataset.title };
          showUndo();
          fetchTasks(state.currentTab);
          fetchCalendar();
        })
        .catch(showError);
      return;
    }
  }

  function handleListChange(event) {
    const checkbox = event.target.closest("input[data-task-select]");
    if (checkbox) {
      const id = String(checkbox.value || "").trim();
      if (!id) return;
      if (checkbox.checked) {
        state.completedSelection.add(id);
      } else {
        state.completedSelection.delete(id);
      }
      updateBulkControls();
      return;
    }

    const statusSelect = event.target.closest("select[data-inline-status]");
    if (statusSelect) {
      const card = statusSelect.closest("[data-todo-id]");
      if (!card) return;
      const id = card.dataset.todoId;
      updateTask(id, { status: statusSelect.value })
        .then(() => {
          fetchTasks(state.currentTab);
          fetchCalendar();
        })
        .catch(showError);
      return;
    }

    const dueInput = event.target.closest("input[data-inline-due]");
    if (dueInput) {
      const card = dueInput.closest("[data-todo-id]");
      if (!card) return;
      const id = card.dataset.todoId;
      updateTask(id, { due_date: dueInput.value || null })
        .then(() => {
          fetchTasks(state.currentTab);
          fetchCalendar();
        })
        .catch(showError);
    }
  }

  function showUndo() {
    if (!state.undo || !els.undoBox || !els.undoLabel) return;
    els.undoBox.classList.remove("hidden");
    els.undoLabel.textContent = `${state.undo.title}`;
  }

  function hideUndo() {
    if (els.undoBox) {
      els.undoBox.classList.add("hidden");
      if (els.undoLabel) els.undoLabel.textContent = "";
    }
    state.undo = null;
  }

  function showError(err) {
    alert(err.message || err);
  }

  function changePage(delta) {
    const meta = state.meta[state.currentTab];
    if (!meta || !meta.pagination) return;
    const nextPage = meta.pagination.page + delta;
    if (nextPage < 1 || nextPage > (meta.pagination.pages || 1)) return;
    getPaginationState(state.currentTab).page = nextPage;
    fetchTasks(state.currentTab);
  }

  function applyPageSize(newSize) {
    const parsed = Number(newSize);
    if (!Number.isFinite(parsed) || parsed <= 0 || parsed === state.pageSize) return;
    if (els.searchInput) {
      state.search = els.searchInput.value.trim();
    }
    state.pageSize = parsed;
    resetPagination();
    fetchTasks(state.currentTab);
  }

  function applyFilters() {
    const searchValue = els.searchInput ? els.searchInput.value.trim() : "";
    let sizeValue = state.pageSize;
    if (els.pageSizeSelect) {
      const parsed = Number(els.pageSizeSelect.value);
      sizeValue = Number.isFinite(parsed) && parsed > 0 ? parsed : state.pageSize;
    }
    let ownerValue = state.owner;
    if (els.ownerFilter) {
      ownerValue = els.ownerFilter.value;
    }
    const sizeChanged = sizeValue !== state.pageSize;
    const searchChanged = searchValue !== state.search;
    const ownerChanged = ownerValue !== state.owner;
    if (!sizeChanged && !searchChanged && !ownerChanged) return;
    state.search = searchValue;
    state.pageSize = sizeValue;
    state.owner = ownerValue || "";
    resetPagination();
    fetchTasks(state.currentTab);
  }

  function initForms() {
    if (els.bulkToggle && els.bulkForm) {
      els.bulkToggle.addEventListener("click", () => {
        els.bulkForm.classList.toggle("hidden");
      });
      els.bulkForm.addEventListener("submit", (event) => {
        event.preventDefault();
        const data = new FormData(els.bulkForm);
        const lines = (data.get("bulk") || "")
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter(Boolean);
        if (!lines.length) return;
        const due = data.get("bulk_due") || null;
        const week = data.get("bulk_week") || config.currentWeek;
        Promise.all(lines.map((line) => addTask({ title: line, due_date: due, week_start: week, status: "pending" })))
          .then(() => {
            els.bulkForm.reset();
            fetchTasks(state.currentTab);
            fetchCalendar();
          })
          .catch(showError);
      });
    }

    if (els.undoBtn) {
      els.undoBtn.addEventListener("click", () => {
        if (!state.undo) return;
        updateTask(state.undo.id, { status: state.undo.status })
          .then(() => {
            hideUndo();
            fetchTasks("this-week");
            fetchCalendar();
          })
          .catch(showError);
      });
    }

    if (els.refreshBtn) {
      els.refreshBtn.addEventListener("click", () => {
        fetchTasks(state.currentTab);
        fetchCalendar();
      });
    }

    if (els.pageSizeSelect) {
      els.pageSizeSelect.value = state.pageSize;
      els.pageSizeSelect.addEventListener("change", (event) => {
        applyPageSize(event.target.value);
      });
    }

    if (els.ownerFilter) {
      els.ownerFilter.value = state.owner || "all";
    }

    if (els.searchInput) {
      els.searchInput.value = state.search;
      els.searchInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          applyFilters();
        }
      });
    }

    if (els.filterApply) {
      els.filterApply.addEventListener("click", () => {
        applyFilters();
      });
    }
    if (els.filterChips) {
      els.filterChips.addEventListener("click", (event) => {
        const btn = event.target.closest("[data-chip-remove]");
        if (!btn) return;
        const key = btn.dataset.chipRemove;
        if (key === "search") {
          state.search = "";
          if (els.searchInput) els.searchInput.value = "";
        } else if (key === "owner") {
          state.owner = config.ownerFilterDefault || "";
          if (els.ownerFilter) els.ownerFilter.value = state.owner || "all";
        } else if (key === "per") {
          state.pageSize = defaultPageSize;
          if (els.pageSizeSelect) els.pageSizeSelect.value = String(defaultPageSize);
        }
        resetPagination();
        fetchTasks(state.currentTab);
      });
    }

    if (els.filterReset) {
      els.filterReset.addEventListener("click", () => {
        resetAllFiltersAndReload();
      });
    }

    if (els.paginationBlocks && els.paginationBlocks.length) {
      els.paginationBlocks.forEach((block) => {
        const prev = block.querySelector("[data-pagination-prev]");
        const next = block.querySelector("[data-pagination-next]");
        const jumpBtn = block.querySelector("[data-pagination-jump-btn]");
        const jumpInput = block.querySelector("[data-pagination-jump-input]");
        if (prev) prev.addEventListener("click", () => changePage(-1));
        if (next) next.addEventListener("click", () => changePage(1));
        if (jumpBtn && jumpInput) {
          jumpBtn.addEventListener("click", () => {
            const meta = state.meta[state.currentTab];
            if (!meta || !meta.pagination) return;
            const target = Number(jumpInput.value);
            if (!Number.isFinite(target)) return;
            const page = Math.max(1, Math.min(target, meta.pagination.pages || 1));
            getPaginationState(state.currentTab).page = page;
            fetchTasks(state.currentTab);
          });
        }
      });
    }

    if (els.calendarPrev) {
      els.calendarPrev.addEventListener("click", () => {
        state.calendar.current.setMonth(state.calendar.current.getMonth() - 1);
        fetchCalendar();
      });
    }

    if (els.calendarNext) {
      els.calendarNext.addEventListener("click", () => {
        state.calendar.current.setMonth(state.calendar.current.getMonth() + 1);
        fetchCalendar();
      });
    }
    if (els.calendarToday) {
      els.calendarToday.addEventListener("click", () => {
        state.calendar.current = config.today ? new Date(config.today) : new Date();
        fetchCalendar();
      });
    }

    if (els.calendarGrid) {
      els.calendarGrid.addEventListener("click", handleCalendarInteraction);
      els.calendarGrid.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          handleCalendarInteraction(event);
        }
      });
    }

    if (completedDeleteBtn) {
      completedDeleteBtn.addEventListener("click", deleteCompletedTasks);
    }

    if (els.completedSelectAll) {
      els.completedSelectAll.addEventListener("change", (event) => {
        const checked = Boolean(event.target.checked);
        visibleTaskCheckboxes().forEach((checkbox) => {
          checkbox.checked = checked;
          const id = String(checkbox.value || "").trim();
          if (!id) return;
          if (checked) {
            state.completedSelection.add(id);
          } else {
            state.completedSelection.delete(id);
          }
        });
        updateBulkControls();
      });
    }

    if (els.bulkMarkDone) {
      els.bulkMarkDone.addEventListener("click", () => {
        runBulkPatch({ status: "done" })
          .then(() => {
            clearCompletedSelection();
            fetchTasks(state.currentTab);
            fetchCalendar();
          })
          .catch(showError);
      });
    }

    if (els.bulkDelete) {
      els.bulkDelete.addEventListener("click", () => {
        if (!window.confirm(t("Delete selected tasks? This cannot be undone."))) return;
        runBulkDelete()
          .then(() => {
            clearCompletedSelection();
            fetchTasks(state.currentTab);
            fetchCalendar();
          })
          .catch(showError);
      });
    }

  }

  tabs.forEach((btn) => {
    btn.addEventListener("click", () => {
      const tab = btn.dataset.tab;
      if (tab === state.currentTab) {
        setTabActive(tab);
        return;
      }
      fetchTasks(tab);
    });
  });

  if (els.list) {
    els.list.addEventListener("click", handleListClick);
    els.list.addEventListener("change", handleListChange);
  }

  function initialTab() {
    const tab = new URLSearchParams(window.location.search).get("tab");
    if (!tab) return "this-week";
    return TAB_QUERIES[tab] ? tab : "this-week";
  }

  initForms();
  fetchTaskSummary();
  fetchTasks(initialTab());
  fetchCalendar();
})();
