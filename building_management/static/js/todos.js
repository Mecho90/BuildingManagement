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
      pending: null,
    },
    pagination: {
      "this-week": { page: 1, per: defaultPageSize },
    },
    meta: {},
    pageSize: defaultPageSize,
    search: "",
    owner: config.ownerFilterDefault || "",
  };

  const els = {
    list: document.getElementById("todo-list"),
    empty: document.getElementById("todo-empty"),
    completedDeleteBtn: document.getElementById("todo-completed-delete-all"),
    calendarGrid: document.getElementById("todo-calendar-grid") || document.getElementById("todo-calendar"),
    calendarMonth: document.getElementById("todo-calendar-month"),
    calendarPrev: document.getElementById("todo-calendar-prev"),
    calendarNext: document.getElementById("todo-calendar-next"),
    calendarToday: document.getElementById("todo-calendar-today"),
    calendarStatus: document.getElementById("todo-calendar-status"),
    calendarCancel: document.getElementById("todo-calendar-cancel"),
    undoBox: document.getElementById("todo-undo"),
    undoLabel: document.getElementById("todo-undo-label"),
    undoBtn: document.getElementById("todo-undo-btn"),
    refreshBtn: document.getElementById("todo-refresh"),
    pageSizeSelect: document.getElementById("todo-page-size"),
    filterApply: document.getElementById("todo-filter-apply"),
    searchInput: document.getElementById("todo-search"),
    ownerFilter: document.getElementById("todo-owner-filter"),
    pagination: document.getElementById("todo-pagination"),
    paginationLabel: document.getElementById("todo-pagination-label"),
    paginationPage: document.getElementById("todo-pagination-page"),
    paginationPrev: document.getElementById("todo-pagination-prev"),
    paginationNext: document.getElementById("todo-pagination-next"),
  };

  const tabs = document.querySelectorAll(".todo-tab");

  const t = (msg) => (window.gettext ? gettext(msg) : msg);

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

  function detailUrl(id) {
    return config.detailUrl.replace("{id}", id);
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

  const ACTIVE_TAB_CLASSES = [
    "border-orange-500",
    "bg-orange-50",
    "text-orange-600",
    "shadow-sm",
    "dark:border-orange-400",
    "dark:bg-orange-500/10",
    "dark:text-orange-200",
  ];
  const INACTIVE_TAB_CLASSES = ["border-transparent", "text-slate-500", "dark:text-slate-300"];

  function setTabActive(targetTab) {
    tabs.forEach((btn) => {
      const isActive = btn.dataset.tab === targetTab;
      ACTIVE_TAB_CLASSES.forEach((cls) => btn.classList.toggle(cls, isActive));
      INACTIVE_TAB_CLASSES.forEach((cls) => btn.classList.toggle(cls, !isActive));
    });
    if (completedDeleteBtn) {
      completedDeleteBtn.classList.toggle("hidden", targetTab !== "completed");
    }
  }

  function showEmpty(show) {
    if (!els.empty) return;
    els.empty.classList.toggle("hidden", !show);
  }

  function renderTasks(tab, tasks) {
    const activeStatuses = ["pending", "in_progress"];
    const activeTasks = tab === "this-week" ? tasks.filter((t) => activeStatuses.includes(t.status)) : tasks;
    const cards = activeTasks.map(renderCard).join("");
    els.list.innerHTML = cards;
    showEmpty(activeTasks.length === 0);
  }

  function renderPagination(tab, pagination, visibleCount, totalCount) {
    if (!els.pagination) return;
    if (!pagination) {
      els.pagination.classList.add("hidden");
      return;
    }
    state.pagination[tab] = { page: pagination.page, per: pagination.per };
    if (pagination.pages <= 1 && totalCount <= pagination.per) {
      els.pagination.classList.add("hidden");
      return;
    }
    const start = visibleCount ? (pagination.page - 1) * pagination.per + 1 : 0;
    const end = visibleCount ? (pagination.page - 1) * pagination.per + visibleCount : 0;
    if (els.paginationLabel) {
      if (visibleCount) {
        els.paginationLabel.textContent = `${t("Showing")} ${start}–${end} ${t("of")} ${totalCount}`;
      } else {
        els.paginationLabel.textContent = t("No tasks to show.");
      }
    }
    if (els.paginationPage) {
      els.paginationPage.textContent = `${t("Page")} ${pagination.page} / ${pagination.pages}`;
    }
    if (els.paginationPrev) {
      els.paginationPrev.disabled = !pagination.has_previous;
    }
    if (els.paginationNext) {
      els.paginationNext.disabled = !pagination.has_next;
    }
    els.pagination.classList.remove("hidden");
  }

  function completedFilters() {
    if (state.currentTab !== "completed") return null;
    const builder = TAB_QUERIES[state.currentTab];
    if (!builder) return null;
    const params = { ...builder() };
    delete params.history;
    delete params.include_history;
    params.status = "done";
    return params;
  }

  function deleteCompletedTasks() {
    if (!completedDeleteBtn) return;
    const filters = completedFilters();
    if (!filters) return;
    const confirmMessage = completedDeleteBtn.dataset.confirm || t("Delete completed tasks?");
    if (!window.confirm(confirmMessage)) return;
    const params = new URLSearchParams(filters);
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
        fetchTasks(state.currentTab === "completed" ? "completed" : "this-week");
        fetchCalendar();
      })
      .catch(showError);
  }

  function calendarLinks(item) {
    return "";
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
        ? `${deleteUrl}?next=${encodeURIComponent(config.listUrl || window.location.pathname)}`
        : deleteUrl;
    const ownerName = item.owner && item.owner.name ? escapeHtml(item.owner.name) : "";
    const showOwner = ownerName && item.owner.id !== config.currentUserId;
    const ownerBadge = showOwner ? `<span class="todo-card__owner">${ownerName}</span>` : "";
    const completedMeta = item.completed_at
      ? `<span class="inline-flex items-center gap-1"><span class="font-semibold">${t("Completed")}:</span> ${relativeTime(item.completed_at)}</span>`
      : "";
    const cardClass = cardStatusClass(status);
    return `
      <article class="${cardClass}" data-todo-id="${item.id}" data-title="${safeTitle}" data-status="${status}" data-date="${item.due_date || item.week_start || ""}">
        <div class="todo-card__body">
          <div class="todo-card__header">
            <div class="todo-card__title-wrap">
              <h3 class="todo-card__title">${safeTitle}</h3>
              <span class="todo-card__status ${badgeClass}" style="${badgeStyle}">${statusLabel}</span>
              ${ownerBadge}
            </div>
          </div>
          ${descriptionBlock}
          <div class="todo-card__meta">
            <span><span class="font-semibold">${t("Due")}:</span> ${dueLabel || "–"}</span>
            ${completedMeta}
          </div>
        </div>
        <div class="todo-card__actions">
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
            const isPending = isCurrentMonth && state.calendar.pending && state.calendar.pending.highlight === dateKey;
            const classes = [
              "h-20 align-top border border-emerald-50 px-2 py-1 text-slate-700 dark:text-slate-100 transition",
              isCurrentMonth
                ? isToday
                  ? "bg-amber-100/90 dark:bg-amber-500/30"
                  : "bg-white/90 dark:bg-transparent"
                : "bg-transparent text-transparent border-transparent pointer-events-none",
              isPending ? "ring-2 ring-amber-400" : "",
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

  function isoWeekStartFrom(dateStr) {
    const d = startOfWeek(new Date(dateStr));
    return calendarDateString(d);
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
    if (state.calendar.pending) {
      updateTask(state.calendar.pending.id, { due_date: dateStr, week_start: isoWeekStartFrom(dateStr) })
        .then(() => {
          setCalendarStatus(`${t("Moved")} ${state.calendar.pending.title} ${t("to")} ${formatDate(dateStr)}`);
          state.calendar.pending = null;
          if (els.calendarCancel) els.calendarCancel.classList.add("hidden");
          fetchTasks(state.currentTab);
          fetchCalendar();
        })
        .catch(showError);
    } else {
      renderDaySummary(dateStr);
      if (config.createUrl) {
        const separator = config.createUrl.includes("?") ? "&" : "?";
        window.location.href = `${config.createUrl}${separator}due=${dateStr}`;
      }
    }
  }

  function setPendingReschedule(card) {
    state.calendar.pending = {
      id: card.dataset.todoId,
      title: card.dataset.title,
      highlight: card.dataset.date || config.currentWeek,
    };
    setCalendarStatus(`${t("Select a new date for")} ${card.dataset.title}.`);
    if (els.calendarCancel) els.calendarCancel.classList.remove("hidden");
    renderCalendar();
  }

  function cancelReschedule() {
    state.calendar.pending = null;
    setCalendarStatus(t("Reschedule cancelled."));
    if (els.calendarCancel) els.calendarCancel.classList.add("hidden");
    renderCalendar();
  }

  function fetchTasks(tab) {
    state.currentTab = tab;
    setTabActive(tab);
    showEmpty(false);
    els.list.innerHTML = `<div class="animate-pulse rounded-2xl border border-dashed border-emerald-200/70 bg-white/70 p-6 text-center text-sm text-slate-500">${t("Loading…")}</div>`;
    const params = new URLSearchParams(TAB_QUERIES[tab] ? TAB_QUERIES[tab]() : {});
    const pageState = getPaginationState(tab);
    params.set("per", pageState.per);
    params.set("page", pageState.page);
    if (state.search) {
      params.set("q", state.search);
    }
    if (state.owner !== undefined) {
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
        renderPagination(tab, pagination, results.length, totalCount);
      })
      .catch((err) => {
        els.list.innerHTML = `<div class="rounded-2xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">${err.message}</div>`;
        if (els.pagination) {
          els.pagination.classList.add("hidden");
        }
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
    if (actionEl.dataset.action === "reschedule") {
      setPendingReschedule(card);
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

    if (els.paginationPrev) {
      els.paginationPrev.addEventListener("click", () => changePage(-1));
    }
    if (els.paginationNext) {
      els.paginationNext.addEventListener("click", () => changePage(1));
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

    if (els.calendarCancel) {
      els.calendarCancel.addEventListener("click", cancelReschedule);
    }

    if (completedDeleteBtn) {
      completedDeleteBtn.addEventListener("click", deleteCompletedTasks);
    }

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && state.calendar.pending) {
        cancelReschedule();
      }
    });
  }

  tabs.forEach((btn) => {
    btn.addEventListener("click", () => {
      const tab = btn.dataset.tab;
      if (tab === state.currentTab) return;
      fetchTasks(tab);
    });
  });

  if (els.list) {
    els.list.addEventListener("click", handleListClick);
  }

  initForms();
  fetchTasks("this-week");
  fetchCalendar();
})();
