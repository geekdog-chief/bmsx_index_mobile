/* 宝妈指数 GUI — 表现层渲染（数据源：window.INDEX_DATA，由 data.js 注入） */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const DATA = window.INDEX_DATA || {};
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v, d = 0) => { const n = Number(v); return Number.isFinite(n) ? n : d; };
  const fmt1 = (v) => num(v).toFixed(1);
  const hhmm = (iso) => { try { const d = new Date(iso); return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; } catch (e) { return "--:--"; } };

  /* ---------- 状态条 ---------- */
  const lamp = $("lamp"), statxt = $("statxt"), progwrap = $("progwrap"), prog = $("prog"), timepill = $("timepill"), daypill = $("daypill"), btnupd = $("btnupd");
  function setState(s, txt) { lamp.className = "lamp " + s; statxt.className = "statxt " + s; statxt.textContent = txt; }

  /* ---------- 宝妈指数 ---------- */
  function gaugeColor(v) {
    if (v >= 80) return "#b83e3e"; if (v >= 65) return "#de684e"; if (v >= 50) return "#efa946";
    if (v >= 35) return "#2d9278"; return "#2563eb";
  }
  function gaugeState(v, histLen) {
    if (v >= 80) return "极热";
    if (v >= 65) return "亢奋";
    if (v >= 50) return "活跃";
    if (v >= 35) return "温和";
    return "偏冷";
  }
  function renderGauge() {
    const scores = DATA.scores || {};
    const v = num(scores.composite, NaN);
    const hist = DATA.history || [];
    if (!Number.isFinite(v)) { $("gauge-state").textContent = "暂无数据"; return; }
    const C = 439.8;
    $("gauge-svg").innerHTML = `
      <circle cx="85" cy="85" r="70" fill="none" stroke="#f0f0ee" stroke-width="14"/>
      <circle cx="85" cy="85" r="70" fill="none" stroke="${gaugeColor(v)}" stroke-width="14" stroke-linecap="round"
        stroke-dasharray="${C}" stroke-dashoffset="${(C * (1 - v / 100)).toFixed(1)}" transform="rotate(-90 85 85)"/>
      <text x="85" y="93" text-anchor="middle" font-size="42" font-weight="600" fill="#1a1a1a">${Math.round(v)}</text>
      <text x="85" y="115" text-anchor="middle" font-size="11" fill="#999">/ 100</text>`;
    $("gauge-state").textContent = "当前状态：" + gaugeState(v);
    const prev = hist.length >= 2 ? num(hist[hist.length - 2].composite) : null;
    const diff = prev === null ? "首个记录点，无历史对比" : `较前一交易日 ${prev > v ? "降温" : prev < v ? "升温" : "持平"} ${Math.abs(prev - v).toFixed(1)} 点`;
    $("gauge-desc").textContent = diff + "。由东方财富股吧、新浪股吧、同花顺板块社区、淘股吧四个平台普通用户公开讨论合成，四渠道固定各25%，缺失席位保留中性不转权。观察情绪，不预测涨跌。";
    if (DATA.marketDate) daypill.textContent = "最近交易日 " + DATA.marketDate;
    if (DATA.updatedAt) timepill.textContent = "更新于 " + hhmm(DATA.updatedAt);
  }

  /* ---------- 宝妈情绪（贪婪/恐惧）：羊驼直读优先，旧合成兜底（2026-11-25 接管） ---------- */
  function sigmoid(x, c, s) { return 100 / (1 + Math.exp(-(x - c) / s)); }
  function emotionTier(v, kind) {  // 分档借 CNN 恐惧贪婪指数的刻度，两侧各自命名
    if (kind === "greed") {  // 贪婪条：低=市场冷淡，高=狂热（禁用恐惧字眼）
      if (v <= 24) return "极度冷静"; if (v <= 44) return "偏冷";
      if (v <= 55) return "中性"; if (v <= 75) return "贪婪"; return "极度贪婪";
    }
    if (v <= 24) return "平静"; if (v <= 44) return "轻度恐慌";
    if (v <= 55) return "中度恐慌"; if (v <= 75) return "恐慌"; return "极度恐慌";
  }
  function renderGreedFear() {
    const sig = (DATA.signals) || {};
    let greed, fear, src;
    if (Number.isFinite(Number(sig.llmGreed)) && Number.isFinite(Number(sig.llmFear))) {
      greed = Math.max(0, Math.min(100, num(sig.llmGreed)));
      fear = Math.max(0, Math.min(100, num(sig.llmFear)));
      src = "llm";
    } else {  // 存量旧数据无情绪字段 → 回落词典合成
      const mean6 = [sig.heat, sig.optimism, sig.intensity, sig.fomo, sig.newcomer, sig.attention].map((x) => num(x, 50));
      greed = sigmoid(mean6.reduce((a, b) => a + b, 0) / mean6.length, 50, 4);
      fear = Math.max(0, Math.min(100, num(sig.panic, 0)));
      src = "dict";
    }
    $("gbar-i").style.width = greed.toFixed(1) + "%"; $("gval").textContent = greed.toFixed(1);
    $("fbar-i").style.width = fear.toFixed(1) + "%"; $("fval").textContent = fear.toFixed(1);
    const gt = $("gval-tier"), ft = $("fval-tier");
    // 双轴独立展示，不做单轴合成：贪婪与恐惧可同时高企（追高被套盘），一合成就被对冲掉
    // 档位徽章：五级热力配色（冷灰→淡黄→素灰→橙→红），JS 直写内联色，不借 .tag 类
    const chipColor = (v) => v <= 24 ? ["#eef2f6", "#64748b"] : v <= 44 ? ["#fdf3df", "#a16207"]
      : v <= 55 ? ["#e8edf2", "#475569"] : v <= 75 ? ["#fdeede", "#c2410c"] : ["#fde2e2", "#b91c1c"];
    if (gt) { gt.textContent = emotionTier(greed, "greed"); const c = chipColor(greed); gt.style.background = c[0]; gt.style.color = c[1]; }
    if (ft) { ft.textContent = emotionTier(fear, "fear"); const c = chipColor(fear); ft.style.background = c[0]; ft.style.color = c[1]; }
    const note = $("gf-note");
    if (note) note.textContent = src === "llm"
      ? "贪婪与恐惧是两个独立的情绪强度指标，读数0-100，越高该情绪越强，两者可同时高企、不互相抵消。贪婪侧分档：≤24极度冷静，25-44偏冷，45-55中性，56-75贪婪，≥76极度贪婪；恐惧侧分档：≤24平静，25-44轻度恐慌，45-55中度恐慌，56-75恐慌，≥76极度恐慌。由本地模型逐帖读取（0-3/帖，四平台固定25%，营销文不计）。"
      : "贪婪与恐惧由六项讨论信号合成（旧数据）。读数0-100，越高越强。";
  }

  /* ---------- 宝妈热度（最热/最冷） ---------- */
  function heatTier(v) { if (v >= 65) return ["高热", "hot"]; if (v >= 50) return ["活跃", "warm"]; return ["偏冷", "cold"]; }
  function renderRank(mode) {
    // heatRank 为 null（数据不足、暂不硬排名）的板块不参与榜单：Number(null)===0 会把它们顶到榜首
    const ranked = (DATA.sectorIndices || []).filter((s) => s && s.available && s.heatRank !== null && s.heatRank !== undefined)
      .sort((a, b) => num(a.heatRank) - num(b.heatRank));
    const list = mode === "hot" ? ranked.slice(0, 12) : ranked.slice(-12).reverse();
    if (!list.length) { $("ranklist").innerHTML = `<div class="muted small">暂无可排名板块。</div>`; return; }
    const heats = list.map((s) => num(s.heat));
    const max = Math.max(...heats), min = Math.min(...heats);
    $("ranklist").innerHTML = list.map((s) => {
      const v = num(s.heat);
      const w = max > min ? ((v - min) / (max - min) * 70 + 30).toFixed(1) : 50;
      const [txt, cls] = heatTier(v);
      return `<div class="row"><span class="rank">${String(num(s.heatRank)).padStart(2, "0")}</span>` +
        `<span class="name" title="${esc(s.name)}">${esc(s.name)}</span>` +
        `<div class="bar"><i style="width:${w}%;background:#2563eb"></i></div>` +
        `<span class="num2">${fmt1(v)}</span><span class="tag ${cls}">${txt}</span></div>`;
    }).join("");
  }

  /* ---------- 宝妈地图（四象限散点，移植自原版画法） ---------- */
  function sectorAvailable(s) {
    const metrics = s.metrics || {};
    const fairness = metrics.fairness || {};
    if (fairness.available === false) return false;
    return s.available === true || (s.signals && Number.isFinite(Number(s.relativeHeat)));
  }
  function tradingDayPostStats(metrics) {
    const counts = (metrics && metrics.dailyPostCounts) || [];
    let total = null;
    for (const item of counts) {
      const n = Number(item.postCount);
      if (!Number.isFinite(n)) continue;
      total = (total === null ? 0 : total) + n;
    }
    return { total };
  }
  function sectorQuadrant(sector) {
    const heat = Number(sector.relativeHeat ?? 50);
    const direction = Number(sector.legacyHeat ?? sector.direction ?? 50);
    if (heat >= 50 && direction >= 50) return "hot-bull";
    if (heat >= 50) return "hot-bear";
    if (direction >= 50) return "cool-bull";
    return "cool-bear";
  }
  function colorFor(heat) {
    if (heat >= 80) return "#b83e3e"; if (heat >= 65) return "#de684e"; if (heat >= 50) return "#efa946";
    if (heat >= 35) return "#2d9278"; return "#647d77";
  }
  function computeRanges(sectors) {
    // 数据自适应：轴范围=实际分布±25%padding，保底跨度（方向≥8、热度≥8），50中性线保留
    const hs = sectors.map((s) => num(s.relativeHeat, 50));
    const ds = sectors.map((s) => num(s.legacyHeat ?? s.direction, 50));
    let hMin = Math.min(50, ...hs), hMax = Math.max(50, ...hs);
    let dMin = Math.min(50, ...ds), dMax = Math.max(50, ...ds);
    const padH = Math.max(2, (hMax - hMin) * 0.25), padD = Math.max(2, (dMax - dMin) * 0.25);
    hMin = Math.max(0, hMin - padH); hMax = Math.min(100, hMax + padH);
    dMin = Math.max(0, dMin - padD); dMax = Math.min(100, dMax + padD);
    if (hMax - hMin < 8) { const c = (hMin + hMax) / 2; hMin = Math.max(0, c - 4); hMax = Math.min(100, c + 4); }
    if (dMax - dMin < 8) { const c = (dMin + dMax) / 2; dMin = Math.max(0, c - 4); dMax = Math.min(100, c + 4); }
    return { hMin, hMax, dMin, dMax };
  }
  const clampRange = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const sizeFor = (raw, min, max) => {
    const r = max > min ? (Math.sqrt(raw) - Math.sqrt(min)) / (Math.sqrt(max) - Math.sqrt(min)) : 0.5;
    return 12 + Math.max(0, Math.min(1, r)) * 13;
  };

  function renderMap() {
    const sectors = (DATA.sectorIndices || []).filter(sectorAvailable);
    const R = computeRanges(sectors);
    const samples = sectors.map((s) => { const t = tradingDayPostStats(s.metrics || {}); return t.total === null ? 0 : t.total; });
    const present = samples.filter((x) => x > 0);
    const minS = present.length ? Math.min(...present) : 0, maxS = present.length ? Math.max(...present) : 1;
    const counts = { "hot-bull": 0, "hot-bear": 0, "cool-bull": 0, "cool-bear": 0 };
    const W = 1040, H = 520;
    const xFor = (v) => 6 + (Math.max(R.dMin, Math.min(R.dMax, v)) - R.dMin) / (R.dMax - R.dMin) * 88;
    const yFor = (v) => 94 - (Math.max(R.hMin, Math.min(R.hMax, v)) - R.hMin) / (R.hMax - R.hMin) * 88;
    const px = (x) => x / 100 * W, py = (y) => y / 100 * H;
    const order = sectors.map((s, i) => i).sort((a, b) => samples[b] - samples[a]);
    const placed = [];
    const pts = order.map((i) => {
      const s = sectors[i];
      const heat = num(s.relativeHeat, 50), direction = num(s.legacyHeat ?? s.direction, 50);
      const q = sectorQuadrant(s); counts[q] += 1;
      const w = String(s.name || "").length * 11.5 + 8, h = 13;
      const dotX = px(xFor(direction)), dotY = py(yFor(heat));
      const cands = [
        { x: dotX + 16, y: dotY - h / 2, css: "left:16px" },
        { x: dotX - w - 16, y: dotY - h / 2, css: "right:16px" },
        { x: dotX - w / 2, y: dotY - h - 14, css: "left:50%;transform:translate(-50%,-100%);top:-14px" },
        { x: dotX - w / 2, y: dotY + 14, css: "left:50%;transform:translate(-50%,0);top:100%" },
      ];
      let pick = null;
      for (const c of cands) {
        let hit = false;
        for (const b of placed) { if (c.x < b.x + b.w + 3 && c.x + w + 3 > b.x && c.y < b.y + b.h + 2 && c.y + h + 2 > b.y) { hit = true; break; } }
        if (!hit) { pick = c; break; }
      }
      if (pick) placed.push({ x: pick.x, y: pick.y, w, h });
      const lbl = pick ? `<strong style="${pick.css}">${esc(s.name)}</strong>` : "";
      const t = tradingDayPostStats(s.metrics || {});
      const title = `${s.name}：热度 ${heat.toFixed(1)}，方向 ${direction.toFixed(1)}` + (t.total !== null ? `，三交易日有效样本 ${Math.round(t.total)} 条` : "");
      const pt = sizeFor(Math.max(samples[i], minS || 1), minS || 0, maxS || 1);
      return `<button type="button" class="smap-point" title="${esc(title)}" style="left:${(dotX / W * 100).toFixed(2)}%;top:${(dotY / H * 100).toFixed(2)}%;--pt:${pt.toFixed(1)}px;--pc:${colorFor(heat)}"><i></i>${lbl}</button>`;
    }).join("");
    const meta = [["hot-bull", "高热偏多", "讨论活跃且偏多"], ["hot-bear", "高热偏空", "讨论活跃但偏空"], ["cool-bull", "低热偏多", "讨论不拥挤但偏多"], ["cool-bear", "低热偏空", "讨论较冷且偏空"]];
    $("smap-summary").innerHTML = meta.map(([k, l, c]) => `<div class="qcard is-${k}"><span>${l}</span><strong>${counts[k]} 个</strong><small>${c}</small></div>`).join("");
    const neutralY = yFor(50);
    const neutralX = xFor(50);
    $("smap").innerHTML = `
      <div class="smap-quad is-hot-bear" style="left:0;width:${neutralX}%;top:0;height:${neutralY}%"><span>高热偏空</span></div>
      <div class="smap-quad is-hot-bull" style="left:${neutralX}%;width:${100 - neutralX}%;top:0;height:${neutralY}%"><span>高热偏多</span></div>
      <div class="smap-quad is-cool-bear" style="left:0;width:${neutralX}%;top:${neutralY}%;height:${100 - neutralY}%"><span>低热偏空</span></div>
      <div class="smap-quad is-cool-bull" style="left:${neutralX}%;width:${100 - neutralX}%;top:${neutralY}%;height:${100 - neutralY}%"><span>低热偏多</span></div>
      <div class="smap-axis is-x" style="top:${neutralY}%"><span>${R.dMin.toFixed(0)} 偏空</span><strong>情绪方向 50</strong><span>${R.dMax.toFixed(0)} 偏多</span></div>
      <div class="smap-axis is-y" style="left:${neutralX}%"><span class="is-high">热 ${R.hMax.toFixed(0)}</span><strong style="top:${neutralY}%">热度 50</strong><span class="is-low">${R.hMin.toFixed(0)} 冷</span></div>
      ${pts}`;
  }

  /* ---------- 宝妈曲线 ---------- */
  function renderHistory() {
    const hist = (DATA.history || []).filter((p) => Number.isFinite(Number(p.composite)));
    const svg = $("history-svg");
    if (!hist.length) { svg.innerHTML = ""; $("history-range").textContent = "暂无历史数据。"; return; }
    const X = (i) => 60 + (hist.length === 1 ? 880 : i / (hist.length - 1) * 880);
    const Y = (v) => 180 - (clampRange(v, 0, 100) / 100) * 160;
    const pts = hist.map((p, i) => `${X(i).toFixed(1)},${Y(num(p.composite)).toFixed(1)}`).join(" ");
    const last = hist[hist.length - 1];
    svg.innerHTML = `
      <line x1="40" y1="180" x2="940" y2="180" stroke="#eee"/>
      <line x1="40" y1="20" x2="940" y2="20" stroke="#eee"/>
      <text x="8" y="184" font-size="11" fill="#aaa">0</text>
      <text x="8" y="24" font-size="11" fill="#aaa">100</text>
      ${hist.length > 1 ? `<polyline fill="none" stroke="#2563eb" stroke-width="2.5" points="${pts}"/>` : ""}
      <circle cx="${X(hist.length - 1)}" cy="${Y(num(last.composite))}" r="4" fill="#2563eb"/>
      <text x="${(X(hist.length - 1) - 8).toFixed(1)}" y="${(Y(num(last.composite)) - 12).toFixed(1)}" font-size="12" font-weight="600" fill="#2563eb">${Math.round(num(last.composite))}</text>`;
    $("history-range").textContent = `${hist[0].date} → ${last.date}（单日一点，共 ${hist.length} 个记录点）`;
  }

  /* ---------- 原帖抽样 ---------- */
  function sentClass(label) {
    const t = String(label || "");
    if (/恐慌|看空|割肉|绝望/.test(t)) return "neg";
    if (/中性/.test(t)) return "mid";
    return "pos";
  }
  function renderPosts() {
    const posts = (DATA.community && DATA.community.samplePosts) || [];
    if (!posts.length) { $("posts").innerHTML = `<div class="muted small">本次没有可展示的样本帖。</div>`; return; }
    $("posts").innerHTML = posts.map((p) => `<div class="post">` +
      `<span class="tag cold">${esc(p.platformName || p.source || "社区")}</span>` +
      `<div><div class="ptext">${esc(p.title || "无标题讨论")}</div>` +
      `<div class="pmeta"><span class="tag sent ${sentClass(p.label)}">${esc(p.label || "中性")}</span><span>${esc(p.publishTime || "")}</span></div></div></div>`).join("");
  }

  /* ---------- 指数参考 ---------- */
  function renderTrio() {
    const scores = DATA.scores || {};
    const defs = [
      ["宝妈", scores.baoma, "家庭资金/稳健大众表达视角：观察乐观扩散、追涨语言和讨论出圈程度，不识别作者真实身份"],
      ["小白", scores.novice, "新手式表达视角：强调“能买吗、怎么操作、求教”等语言，不识别作者真实投资年限"],
      ["散户", scores.retail, "主动交易式大众表达视角：观察看多看空、情绪强度和讨论活跃，不识别真实账户类型"],
    ];
    $("trio").innerHTML = defs.map(([l, v, cap]) => `<div class="card tcard"><div class="tlabel">${l}</div><div class="tval">${Number.isFinite(Number(v)) ? Math.round(num(v)) : "--"}</div><div class="tcap muted">${cap}</div></div>`).join("");
  }

  /* ---------- 主选项卡 ---------- */
  function bindTabs() {
    const tabs = document.querySelectorAll(".tab");
    tabs.forEach((t) => t.addEventListener("click", () => {
      tabs.forEach((x) => x.classList.remove("on"));
      document.querySelectorAll(".panel").forEach((x) => x.classList.remove("on"));
      t.classList.add("on");
      $(t.dataset.p).classList.add("on");
    }));
    document.querySelectorAll(".subtab").forEach((t) => t.addEventListener("click", () => {
      document.querySelectorAll(".subtab").forEach((x) => x.classList.remove("on"));
      t.classList.add("on");
      renderRank(t.dataset.t);
    }));
  }

  /* ---------- GUI 状态推送（gui.py → receiveGuiStatus） ---------- */
  let updating = false;
  window.receiveGuiStatus = function (payload) {
    if (!payload || typeof payload !== "object") return;
    if (payload.type === "progress" || (payload.done !== undefined && payload.total)) {
      progwrap.style.display = "block";
      const pct = payload.total ? Math.min(100, payload.done / payload.total * 100) : 0;
      prog.style.width = pct + "%";
      const plat = payload.platform ? `：${payload.platform}` : "";
      setState("run", `正在读取${plat}（总计 ${payload.done}/${payload.total}）`);
      return;
    }
    if (payload.state === "run") { updating = true; btnupd.disabled = true; progwrap.style.display = "block"; setState("run", "正在后台更新"); return; }
    if (payload.state === "ok") {
      updating = false; btnupd.disabled = false; progwrap.style.display = "none"; prog.style.width = "0";
      setState("ok", "数据已更新");
      if (payload.updatedAt) timepill.textContent = "更新于 " + hhmm(payload.updatedAt);
      setTimeout(() => location.reload(), 400);
      return;
    }
    if (payload.state === "bad") { updating = false; btnupd.disabled = false; progwrap.style.display = "none"; setState("bad", `更新失败，保留 ${payload.keptAt || "--:--"} 旧数据`); return; }
  };

  /* ---------- 立即更新 ---------- */
  function bindUpdate() {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.update_now) {
      btnupd.addEventListener("click", () => { if (!updating) window.pywebview.api.update_now(); });
    } else {
      btnupd.disabled = true;
      btnupd.title = "桌面版（启动民间温度计.cmd）可用自动更新";
    }
  }

  /* ---------- 羊驼抽检：人工核对模型读向/情绪质量 ---------- */
  function renderLlmaudit() {
    const box = $("llmaudit"); if (!box) return;
    const rows = DATA.llmSample || [];
    if (!rows.length) { box.innerHTML = '<div class="muted">暂无抽检样本（羊驼打分后下一轮更新自动生成）</div>'; return; }
    const esc = (t) => String(t == null ? "" : t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" }[c]));
    const dirLabel = (x) => x >= 3 ? "强看多" : x >= 1 ? "看多" : x <= -3 ? "强看空" : x <= -1 ? "看空" : "中性";
    const dirCls = (x) => x >= 1 ? "tag warm" : x <= -1 ? "tag cold" : "tag";
    box.innerHTML = rows.map((r) => `<div style="display:flex;gap:8px;align-items:center;padding:7px 0;border-bottom:1px solid #f1f3f5">
      <div style="flex:1;min-width:0">
        <div style="font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.title)}</div>
        <div class="muted" style="font-size:12px">${esc(r.platform)} · ${esc(r.persona)}${r.marketing ? " · 营销文" : ""}${r.reason ? " · " + esc(r.reason) : ""}</div>
      </div>
      <span class="${dirCls(r.direction)}" style="white-space:nowrap">${dirLabel(r.direction)}</span>
      <span style="white-space:nowrap;font-size:12px" title="恐惧/贪婪强度0-3">恐${r.fear}/贪${r.greed}</span>
    </div>`).join("");
  }

  /* ---------- 启动 ---------- */
  renderGauge(); renderGreedFear(); renderRank("hot"); renderMap(); renderHistory(); renderPosts(); renderTrio(); renderLlmaudit();
  bindTabs(); bindUpdate();
  if (DATA.updatedAt) setState("ok", "数据已更新");
})();
