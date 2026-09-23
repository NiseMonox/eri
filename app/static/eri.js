/* Eri 管理页的小脚本:HUD 时钟、今日时间轴的 NOW 标记、音量条、Chart.js 主题。零依赖,零构建。 */
(function () {
  "use strict";

  const TZ = "Asia/Tokyo";
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function nowParts() {
    const f = new Intl.DateTimeFormat("en-US", {
      timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", weekday: "short", hourCycle: "h23",
    });
    const p = {};
    for (const x of f.formatToParts(new Date())) p[x.type] = x.value;
    return p;
  }

  // 今日时间轴:已过去的项变淡,在「现在」的位置插一条 NOW
  function markAgenda(p) {
    const list = document.querySelector("[data-agenda]");
    if (!list) return;
    const now = `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
    const old = list.querySelector("li.now");
    if (old) old.remove();
    let next = null;
    for (const li of list.querySelectorAll("li[data-when]")) {
      const past = li.dataset.when < now;
      li.classList.toggle("past", past);
      if (!past && !next) next = li;
    }
    const li = document.createElement("li");
    li.className = "now";
    li.innerHTML = `<time>NOW</time><span class="node"></span><span class="bar"></span>`;
    list.insertBefore(li, next);
  }

  function tick() {
    const p = nowParts();
    const wd = p.weekday.toUpperCase();
    const text = {
      hm: `${p.hour}:${p.minute}`,
      date: `${p.year}.${p.month}.${p.day} ${wd}`,
      full: `${p.year}.${p.month}.${p.day} ${wd} ${p.hour}:${p.minute} JST`,
    };
    document.querySelectorAll("[data-clock]").forEach((el) => {
      el.textContent = text[el.dataset.clock] || "";
    });
    markAgenda(p);
  }

  // 滑块左侧填色(WebKit 没有 ::-moz-range-progress)
  function paintRange(r) {
    const pct = ((r.value - r.min) / ((r.max - r.min) || 1)) * 100;
    r.style.setProperty("--fill", pct + "%");
  }

  function setVolume(v) {
    return fetch("/api/audio/volume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ volume: +v }),
    });
  }

  // ---- Chart.js 主题 ----
  // 没数据就不画空坐标轴,换成一行说明
  function noData(el, text) {
    const box = el.closest(".chart-box") || el.parentElement;
    const div = document.createElement("div");
    div.className = "empty";
    div.textContent = text;
    box.replaceWith(div);
  }

  function hatch(color) {
    const dpr = window.devicePixelRatio || 1;
    const s = Math.round(7 * dpr);
    const c = document.createElement("canvas");
    c.width = c.height = s;
    const x = c.getContext("2d");
    x.strokeStyle = color;
    x.lineWidth = 1.2 * dpr;
    x.beginPath();
    for (const o of [-s, 0, s]) { x.moveTo(o, s); x.lineTo(o + s, 0); }
    x.stroke();
    const pat = x.createPattern(c, "repeat");
    if (pat.setTransform && window.DOMMatrix) pat.setTransform(new DOMMatrix().scale(1 / dpr));
    return pat;
  }

  function theme() {
    Chart.defaults.font.family = css("--font");
    Chart.defaults.font.size = 11;
    Chart.defaults.font.weight = "500";
    Chart.defaults.color = css("--muted");
    Chart.defaults.animation.duration = 450;
    return {
      ink: css("--ink"), panel: css("--panel"), line: css("--line-2"), yellow: css("--yellow"),
      hatch: css("--hatch"),
    };
  }

  function tooltip(extra) {
    return Object.assign({
      backgroundColor: "#191919", titleColor: "#fffa00", bodyColor: "#ffffff",
      cornerRadius: 0, padding: 10, displayColors: false, caretSize: 5,
      titleFont: { weight: "600" }, bodyFont: { weight: "700", size: 13 },
    }, extra);
  }

  function axes(c, yTicks) {
    return {
      x: { grid: { display: false }, border: { color: c.ink }, ticks: { maxRotation: 0, autoSkipPadding: 14 } },
      y: { grid: { color: c.line }, border: { display: false }, ticks: Object.assign({ maxTicksLimit: 5, padding: 8 }, yTicks) },
    };
  }

  // 体重折线:方形点、最后一点黄色、线下斜线纹
  function lineChart(id, data, unit, empty) {
    const el = document.getElementById(id);
    if (!el) return;
    if (!data.length) return noData(el, empty || "この期間の記録はまだないよ");
    const c = theme();
    const last = data.length - 1;
    new Chart(el, {
      type: "line",
      data: {
        labels: data.map((d) => d.t.slice(5, 10).replace("-", "/")),
        datasets: [{
          data: data.map((d) => d.kg), borderColor: c.ink, borderWidth: 2, tension: 0,
          pointStyle: "rect", pointBorderColor: c.ink, pointBorderWidth: 1.5,
          pointRadius: (ctx) => (ctx.dataIndex === last ? 5 : 2.5),
          pointBackgroundColor: (ctx) => (ctx.dataIndex === last ? c.yellow : c.panel),
          pointHoverRadius: 6, pointHoverBackgroundColor: c.yellow,
          fill: "start", backgroundColor: hatch(c.hatch),
        }],
      },
      options: {
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: tooltip({ callbacks: {
            title: (items) => data[items[0].dataIndex].t,
            label: (item) => item.parsed.y.toFixed(2) + " " + unit,
          } }),
        },
        scales: axes(c),
      },
    });
  }

  // 步数柱状:最新一天黄色
  function barChart(id, data, unit, empty) {
    const el = document.getElementById(id);
    if (!el) return;
    if (!data.length) return noData(el, empty || "この期間の記録はまだないよ");
    const c = theme();
    const last = data.length - 1;
    new Chart(el, {
      type: "bar",
      data: {
        labels: data.map((d) => d.t.slice(5, 10).replace("-", "/")),
        datasets: [{
          data: data.map((d) => d.v), borderRadius: 0, barPercentage: 0.72, categoryPercentage: 0.9,
          backgroundColor: data.map((_, i) => (i === last ? c.yellow : c.ink)),
          borderColor: c.ink, borderWidth: data.map((_, i) => (i === last ? 1.5 : 0)),
          hoverBackgroundColor: c.yellow,
        }],
      },
      options: {
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: tooltip({ callbacks: {
            title: (items) => data[items[0].dataIndex].t.slice(0, 10),
            label: (item) => item.parsed.y.toLocaleString() + " " + unit,
          } }),
        },
        scales: axes(c, { callback: (v) => (v >= 1000 ? (v / 1000) + "k" : v) }),
      },
    });
  }

  // 窄屏时导航是横向滚动条:把当前页滚到中间(只动导航自己的 scrollLeft,不动页面)
  function centerNav() {
    const nav = document.querySelector(".nav");
    const on = nav && nav.querySelector("a.on");
    if (!on || nav.scrollWidth <= nav.clientWidth) return;
    nav.scrollLeft = on.offsetLeft - (nav.clientWidth - on.offsetWidth) / 2;
  }

  document.addEventListener("DOMContentLoaded", () => {
    centerNav();
    tick();
    setInterval(tick, 15000);
    document.querySelectorAll("input[type=range]").forEach((r) => {
      paintRange(r);
      r.addEventListener("input", () => paintRange(r));
    });
  });

  window.Eri = { setVolume, lineChart, barChart, paintRange };
})();
