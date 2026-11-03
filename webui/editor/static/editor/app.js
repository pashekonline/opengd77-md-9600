(function () {
  "use strict";

  function getCookie(name) {
    const m = document.cookie.match("(^|;)\\s*" + name + "\\s*=\\s*([^;]+)");
    return m ? m.pop() : "";
  }

  const fmt = (s) => {
    s = Math.max(0, Math.floor(s));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`
             : `${m}:${String(r).padStart(2, "0")}`;
  };

  // ---------------- прогресс задач рации ----------------
  const overlay = document.getElementById("job-overlay");
  if (overlay) {
    const elMsg = document.getElementById("job-message");
    const elWrap = document.getElementById("job-progress-wrap");
    const elFill = document.getElementById("job-fill");
    const elText = document.getElementById("job-text");
    const elHint = document.getElementById("job-hint");
    const elElapsed = document.getElementById("job-elapsed");
    const elCancel = document.getElementById("job-cancel");

    let cancellable = false;

    function show(j) {
      overlay.classList.remove("hidden");
      elMsg.textContent = j.message || "Работа с рацией…";
      const hasTotal = j.total_raw > 0;
      elWrap.classList.toggle("hidden", !hasTotal);
      elFill.style.width = (j.pct || 0) + "%";
      elText.textContent = j.total_kb
        ? `${j.pct || 0}% · ${j.done_kb || 0} / ${j.total_kb || 0} КБ`
        : `${j.pct || 0}% · ${j.done_raw || 0} / ${j.total_raw || 0}`;
      elHint.textContent = j.hint || "";
      let line = "Прошло " + fmt(j.elapsed || 0);
      if (j.eta != null) line += " · осталось ~" + fmt(j.eta);
      elElapsed.textContent = line;
      cancellable = !!j.cancellable;
      elCancel.classList.toggle("hidden", !cancellable);
    }

    function hide() {
      overlay.classList.add("hidden");
    }

    async function cancelJob() {
      if (!cancellable) return;
      cancellable = false;
      elCancel.classList.add("hidden");
      elHint.textContent = "Отмена… ждём завершения текущего пакета.";
      await fetch("/api/job/cancel/", {
        method: "POST",
        headers: { "X-CSRFToken": getCookie("csrftoken") },
      });
    }

    elCancel.addEventListener("click", cancelJob);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !overlay.classList.contains("hidden")) {
        cancelJob();
      }
    });

    async function tick() {
      let j;
      try {
        const r = await fetch("/api/job/");
        j = await r.json();
      } catch (e) {
        return;
      }
      if (!j || !j.kind) return;

      if (j.active) {
        show(j);
      } else {
        hide();
        // Перезагрузить один раз по завершении задачи: id завершённой
        // задачи запоминаем в отдельном ключе, активную не трогаем.
        const done = localStorage.getItem("md9600_job_done");
        if (j.id && done !== j.id) {
          localStorage.setItem("md9600_job_done", j.id);
          setTimeout(() => location.reload(), 300);
        }
      }
    }

    setInterval(tick, 600);
    tick();
  }

  // ---------------- подтверждение опасных форм ----------------
  document.querySelectorAll("form[data-confirm]").forEach((f) => {
    f.addEventListener("submit", (e) => {
      if (!window.confirm(f.dataset.confirm)) e.preventDefault();
    });
  });

  // ---------------- фильтры: авторасплет ----------------
  const filters = document.getElementById("filters");
  if (filters) {
    ["zone", "mode"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener("change", () => filters.submit());
    });
    const q = document.getElementById("q");
    if (q) {
      let t = null;
      q.addEventListener("input", () => {
        clearTimeout(t);
        t = setTimeout(() => filters.submit(), 400);
      });
    }
  }

  // ---------------- редактор канала: DMR-поля ----------------
  const modeSel = document.getElementById("mode-select");
  if (modeSel) {
    const cc = document.getElementById("cc-select");
    const contact = document.getElementById("contact-input");
    const update = () => {
      const dmr = modeSel.value === "DMR";
      cc.disabled = !dmr;
      contact.disabled = !dmr;
    };
    modeSel.addEventListener("change", update);
    update();
  }

  // ---------------- поиск NORAD по имени (Celestrak) ----------------
  const findBtn = document.getElementById("find-norad");
  if (findBtn) {
    const nameEl = document.getElementById("sat-name");
    const noradEl = document.getElementById("sat-norad");
    const box = document.getElementById("norad-results");

    findBtn.addEventListener("click", async () => {
      const q = nameEl.value.trim();
      box.innerHTML = "";

      if (q.length < 2) {
        box.textContent = "Введите имя (минимум 2 символа)";
        return;
      }

      findBtn.disabled = true;
      box.textContent = "Поиск в каталоге…";

      try {
        const r = await fetch(
          `/satellites/norad-search?q=${encodeURIComponent(q)}`
        );
        const data = await r.json();
        box.textContent = "";

        if (data.error) {
          box.textContent = `Ошибка каталога: ${data.error}`;
        } else if (!data.results.length) {
          box.textContent = "Ничего не найдено";
        }

        for (const item of data.results) {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "norad-item";
          b.textContent = `${item.norad_id} · ${item.name}`;
          b.addEventListener("click", () => {
            noradEl.value = item.norad_id;
            const nameEl2 = document.getElementById("sat-name");
            if (nameEl2) nameEl2.value = item.name;
            box.innerHTML = "";
            noradEl.dispatchEvent(new Event("change"));
          });
          box.appendChild(b);
        }
      } catch (e) {
        box.textContent = "Ошибка поиска: нет интернета?";
      } finally {
        findBtn.disabled = false;
      }
    });
  }

  // ---------------- транспондеры: загрузка и drag&drop ----------------
  const txPanel = document.getElementById("tx-panel");
  if (txPanel) {
    const noradEl = document.getElementById("sat-norad");
    const loadBtn = document.getElementById("load-tx");
    const listEl = document.getElementById("tx-list");
    const titleEl = document.getElementById("tx-title");

    const PAIRS = {
      1: { rx: "rx_freq_mhz", tx: "tx_freq_mhz", tone: "ctcss_tx_hz" },
      2: { rx: "secondary_rx_freq_mhz", tx: "secondary_tx_freq_mhz" },
      3: { rx: "tertiary_rx_freq_mhz", tx: "tertiary_tx_freq_mhz" },
    };

    const DASH = "—";

    const setVal = (name, v) => {
      const el = document.querySelector(`[name="${name}"]`);
      if (!el) return;
      el.value = (v === null || v === undefined || v === "") ? DASH : v;
    };

    function render(list, norad) {
      listEl.innerHTML = "";

      if (list.error) {
        listEl.textContent = `Ошибка загрузки: ${list.error}`;
        return;
      }

      if (!list.rows.length) {
        listEl.textContent = "Нет транспондеров для NORAD " + norad;
        return;
      }

      const table = document.createElement("table");
      table.className = "channels tx-table";
      table.innerHTML =
        "<thead><tr><th>RX, МГц</th><th>TX, МГц</th><th>Режим</th>" +
        "<th>Тон</th><th>Описание</th></tr></thead>";
      const tb = document.createElement("tbody");
      table.appendChild(tb);

      for (const t of list.rows) {
        const tr = document.createElement("tr");
        tr.draggable = true;
        tr.className = "tx-row" + (t.active ? "" : " inactive");
        tr.innerHTML =
          `<td>${t.rx ?? "—"}</td><td>${t.tx ?? "—"}</td>` +
          `<td>${t.mode || "—"}${t.umode ? " / " + t.umode : ""}</td>` +
          `<td>${t.tone ?? "—"}</td>` +
          `<td class="zones-col">${t.desc}</td>`;
        tr.addEventListener("dragstart", (e) => {
          e.dataTransfer.setData("text/plain", JSON.stringify(t));
          e.dataTransfer.effectAllowed = "copy";
        });
        tr.addEventListener("dblclick", () => {
          const zone = document.querySelector('.pair[data-pair="1"]');
          applyDrop(zone, t);
        });
        tb.appendChild(tr);
      }

      listEl.appendChild(table);
    }

    function applyDrop(zone, t) {
      const f = PAIRS[zone.dataset.pair];
      setVal(f.rx, t.rx);
      setVal(f.tx, t.tx);
      if (f.tone) setVal(f.tone, t.tone);
      zone.classList.add("dropped");
      setTimeout(() => zone.classList.remove("dropped"), 600);
    }

    async function loadTx() {
      const n = (noradEl.value || "").trim();
      txPanel.hidden = true;
      if (!/^\d+$/.test(n)) return;

      txPanel.hidden = false;
      titleEl.textContent = `NORAD ${n}`;
      listEl.textContent = "Загрузка…";

      try {
        const r = await fetch(`/satellites/transmitters?norad=${n}`);
        render(await r.json(), n);
      } catch (e) {
        listEl.textContent = "Ошибка загрузки: нет интернета?";
      }
    }

    for (const zone of document.querySelectorAll(".pair")) {
      zone.addEventListener("dragover", (e) => {
        e.preventDefault();
        zone.classList.add("drop");
      });
      zone.addEventListener("dragleave", () => zone.classList.remove("drop"));
      zone.addEventListener("drop", (e) => {
        e.preventDefault();
        zone.classList.remove("drop");
        try {
          applyDrop(zone, JSON.parse(e.dataTransfer.getData("text/plain")));
        } catch (err) { /* ignore */ }
      });
    }

    // «—» из дропа — это не данные: при вводе очищаем
    for (const el of document.querySelectorAll(".pair input")) {
      el.addEventListener("focus", () => {
        if (el.value === DASH) el.value = "";
      });
    }

    // на всякий случай: не отправлять прочерки как значения
    document.getElementById("sat-form").addEventListener("submit", () => {
      for (const el of document.querySelectorAll(".pair input")) {
        if (el.value === DASH) el.value = "";
      }
    });

    loadBtn.addEventListener("click", loadTx);
    noradEl.addEventListener("change", loadTx);

    if ((noradEl.value || "").trim()) loadTx();
  }
})();
