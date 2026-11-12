(function () {
  const menuBtn = document.getElementById("menuBtn");
  const sidebar = document.getElementById("sidebar");
  if (menuBtn && sidebar) {
    menuBtn.addEventListener("click", () => sidebar.classList.toggle("open"));
    document.addEventListener("click", (event) => {
      if (window.innerWidth <= 760 && sidebar.classList.contains("open") &&
          !sidebar.contains(event.target) && !menuBtn.contains(event.target)) {
        sidebar.classList.remove("open");
      }
    });
  }

  document.querySelectorAll("[data-tabs]").forEach((box) => {
    const buttons = box.querySelectorAll("[data-tab]");
    const root = box.parentElement;
    buttons.forEach((btn) => {
      btn.addEventListener("click", () => {
        buttons.forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        root.querySelectorAll("[data-panel]").forEach((panel) => {
          panel.classList.toggle("active", panel.dataset.panel === btn.dataset.tab);
        });
      });
    });
  });

  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const text = btn.dataset.copy || "";
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
      } catch (_) {
        const area = document.createElement("textarea");
        area.value = text;
        document.body.appendChild(area);
        area.select();
        document.execCommand("copy");
        area.remove();
      }
      const old = btn.textContent;
      btn.textContent = "已复制";
      setTimeout(() => { btn.textContent = old; }, 1200);
    });
  });

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll("[data-rename]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const form = document.getElementById("rename-" + btn.dataset.rename);
      if (!form) return;
      form.hidden = !form.hidden;
      if (!form.hidden) {
        const input = form.querySelector("input[name=name]");
        input.focus();
        input.select();
      }
    });
  });

  const flash = document.getElementById("flash");
  if (flash) {
    setTimeout(() => {
      flash.style.transition = "opacity .3s, transform .3s";
      flash.style.opacity = "0";
      flash.style.transform = "translateY(-6px)";
      setTimeout(() => flash.remove(), 350);
    }, 5000);
  }

  // 积分刷新：AJAX 原地更新，不整页跳转、不改变滚动位置
  function fmtMoney(value) {
    if (value === null || value === undefined || value < 0) return "—";
    return "$" + Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function fmtCredits(value) {
    if (value === null || value === undefined) return "—";
    const number = Number(value);
    if (!Number.isFinite(number) || number < 0) return "—";
    return number.toLocaleString("zh-CN", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  }

  function fmtTime(ts) {
    const value = Number(ts);
    if (!Number.isFinite(value) || value <= 0) return "—";
    const d = new Date(value * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function toast(message, isError) {
    const node = document.createElement("div");
    node.className = "toast" + (isError ? " toast-err" : "");
    node.textContent = message;
    document.body.appendChild(node);
    setTimeout(() => node.classList.add("show"), 10);
    setTimeout(() => {
      node.classList.remove("show");
      setTimeout(() => node.remove(), 250);
    }, 3200);
  }

  function setStatus(form, message, isError) {
    const pill = form.closest(".panel")?.querySelector("[data-refresh-status]")
      || document.querySelector("[data-refresh-status]");
    if (!pill) {
      toast(message, isError);
      return;
    }
    pill.textContent = message;
    pill.hidden = false;
    pill.classList.toggle("success", !isError);
    pill.classList.toggle("danger", !!isError);
    clearTimeout(pill._timer);
    pill._timer = setTimeout(() => { pill.hidden = true; }, 6000);
  }

  function renderPacks(row, packs) {
    const cell = row.querySelector(".credit-packs");
    if (!cell) return;
    cell.replaceChildren();
    if (!Array.isArray(packs) || !packs.length) {
      cell.textContent = "—";
      return;
    }
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = packs.length + " 个积分包";
    details.appendChild(summary);
    const list = document.createElement("ul");
    packs.slice(0, 8).forEach((pack) => {
      const item = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = pack.name || "积分";
      const remain = document.createElement("strong");
      remain.textContent = (pack.unit === "USD" ? "$" : "") + fmtCredits(pack.remain);
      const expiry = document.createElement("span");
      expiry.className = "muted";
      expiry.textContent = pack.expire_at ? fmtTime(pack.expire_at) : "长期";
      item.append(name, remain, expiry);
      list.appendChild(item);
    });
    details.appendChild(list);
    if (packs.length > 8) {
      const more = document.createElement("div");
      more.className = "muted small";
      more.textContent = "另有 " + (packs.length - 8) + " 个积分包未展开";
      details.appendChild(more);
    }
    cell.appendChild(details);
  }

  function applyPayload(data) {
    (data.accounts || []).forEach((account) => {
      const row = document.querySelector('tr[data-account-id="' + account.id + '"]');
      if (!row) return;
      const set = (field, text) => {
        const cell = row.querySelector('[data-field="' + field + '"]');
        if (cell) cell.textContent = text;
      };
      const money = (value) =>
        account.unit === "USD" ? fmtMoney(value) : fmtCredits(value);
      set("credits", money(account.credits));
      set("credits_used", money(account.credits_used));
      set("credits_total", money(account.credits_total));
      set("updated_at", fmtTime(account.updated_at));
      renderPacks(row, account.packs);
    });
    const totals = data.totals || {};
    const totalMoney = (value) =>
      totals.unit === "USD" ? fmtMoney(value) : fmtCredits(value);
    ["remaining", "used", "total", "expiring"].forEach((name) => {
      if (totals[name] === undefined) return;
      const node = document.querySelector('[data-stat="' + name + '"]');
      if (node) node.textContent = totalMoney(totals[name]);
    });
  }

  document.querySelectorAll("form.js-refresh").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      const original = button ? button.textContent : "";
      if (button) {
        button.disabled = true;
        button.textContent = "刷新中…";
      }
      setStatus(form, "正在刷新积分…", false);
      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          credentials: "same-origin",
          headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" }
        });
        const data = await response.json().catch(() => ({ ok: false, message: "响应解析失败" }));
        if (!response.ok || !data.ok) throw new Error(data.message || "刷新失败");
        applyPayload(data);
        setStatus(form, data.message || "已刷新", false);
      } catch (error) {
        setStatus(form, error.message || "刷新失败", true);
      } finally {
        if (button) {
          button.disabled = false;
          button.textContent = original;
        }
      }
    });
  });

  // 轻量操作（签到等）：AJAX 提交 + 弹窗提示，不跳转、不改变滚动位置
  document.querySelectorAll("form.js-toast").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      const original = button ? button.textContent : "";
      if (button) {
        button.disabled = true;
        button.textContent = "处理中…";
      }
      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          credentials: "same-origin",
          headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" }
        });
        const data = await response.json().catch(() => ({ ok: false, message: "响应解析失败" }));
        toast(data.message || (data.ok ? "完成" : "操作失败"), !data.ok);
      } catch (error) {
        toast(error.message || "操作失败", true);
      } finally {
        if (button) {
          button.disabled = false;
          button.textContent = original;
        }
      }
    });
  });

  // 平台连通性测试：只读探测，展示耗时
  document.querySelectorAll("form.js-test").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      const original = button ? button.textContent : "";
      if (button) {
        button.disabled = true;
        button.textContent = "测试中…";
      }
      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          credentials: "same-origin",
          headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" }
        });
        const data = await response.json().catch(() => ({ ok: false, message: "响应解析失败" }));
        const latency = data.latency_ms === undefined ? "" : " · " + data.latency_ms + "ms";
        toast((data.ok ? "✅ " : "❌ ") + (data.message || (data.ok ? "连通正常" : "连通失败")) + latency, !data.ok);
        if (button) button.textContent = data.ok ? "测试 " + data.latency_ms + "ms" : original;
      } catch (error) {
        toast(error.message || "连通测试失败", true);
        if (button) button.textContent = original;
      } finally {
        if (button) button.disabled = false;
      }
    });
  });

  // 同一页面刷新后回到原来的滚动位置（表单提交会整页跳转时使用）
  const SCROLL_KEY = "aiall:scroll:" + window.location.pathname;
  try {
    const saved = Number(window.sessionStorage.getItem(SCROLL_KEY) || 0);
    if (saved > 0) {
      window.requestAnimationFrame(() => window.scrollTo({ top: saved, behavior: "instant" in window ? "instant" : "auto" }));
    }
  } catch (_) {}
  window.addEventListener("pagehide", () => {
    try {
      window.sessionStorage.setItem(SCROLL_KEY, String(window.scrollY || 0));
    } catch (_) {}
  });
  window.addEventListener("beforeunload", () => {
    try {
      window.sessionStorage.setItem(SCROLL_KEY, String(window.scrollY || 0));
    } catch (_) {}
  });
})();
