/* ============================================================================
   app.js —— ANTLab 推理控制台前端

   阶段 S1：状态栏 + Tab + 预留元素提示（已完成）
   阶段 S2：对话流式渲染（本次新增）

   ── 流式实现的三条铁律（计划书 6.3）────────────────────────────────────
     ① 必须用 fetch + response.body.getReader()，不能 await resp.json()
     ② TextDecoder 必须传 {stream:true}，否则中文会被切在块边界导致缺字
     ③ 必须缓冲"半截数据"：按 \n\n 切分后把最后一段留到下一块
   ========================================================================== */

'use strict';

const STATUS_INTERVAL_MS = 3000;

/* ── 小工具 ───────────────────────────────────────────────────────────── */

const $ = (id) => document.getElementById(id);

let toastTimer = null;
function toast(msg, ms = 2600) {
  const el = $('toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ms);
}

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = (text === null || text === undefined || text === '')
    ? '—' : String(text);
}

function nowMs() { return performance.now(); }

/* ── 状态栏 ───────────────────────────────────────────────────────────── */

const LED_CLASS = {
  ok: 'led led-ok',
  loading: 'led led-warn',
  unreachable: 'led led-err',
  error: 'led led-err',
};

let serverReady = false;        // 供发送按钮判断
let switching = false;          // ⭐ S3：模型是否正在切换
let modelListLoaded = false;    // 模型列表是否已加载
let modelListStale = false;     // 切换后需要重新拉列表

async function refreshStatus() {
  let data;
  try {
    const resp = await fetch('/api/status', { cache: 'no-store' });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    data = await resp.json();
  } catch (e) {
    $('led-server').className = 'led led-err';
    setText('st-health', '控制层异常');
    setText('st-time', '状态获取失败：' + e.message);
    serverReady = false;
    updateSendButton();
    return;
  }

  if (data.control) setText('svc-version', data.control.version || '');

  const ms = data.model_server || {};
  serverReady = (ms.health === 'ok');

  // ⭐ S3：优先显示"切换中"状态
  const sw = data.model_switch || {};
  switching = !!sw.switching;
  if (switching) {
    $('led-server').className = 'led led-warn';
    setText('st-health', '切换中');
  } else {
    $('led-server').className = LED_CLASS[ms.health] || 'led led-gray';
    setText('st-health', {
      ok: '正常', loading: '加载中', unreachable: '不可达', error: '异常',
    }[ms.health] || ms.health || '未知');
  }

  setText('st-model', ms.model_name);
  setText('st-quant', ms.quant);
  setText('st-ctx', ms.n_ctx ? ms.n_ctx + ' tok' : null);

  if (Array.isArray(ms.slots) && ms.slots.length) {
    const busy = ms.slots.filter(s => s.is_processing).length;
    setText('st-slots', `${busy}/${ms.slots.length} 占用`);
  } else {
    setText('st-slots', ms.total_slots ? `0/${ms.total_slots}` : null);
  }

  const gpu = data.gpu || {};
  if (gpu.available) {
    const pct = Math.round(gpu.used_mib / gpu.total_mib * 100);
    setText('st-vram', `${gpu.used_mib} / ${gpu.total_mib} MiB (${pct}%)`);
  } else {
    setText('st-vram', '不可用');
  }

  const errBox = $('status-errors');
  const errs = data.errors || [];
  if (switching) {
    errBox.className = 'notice notice-warn';
    errBox.textContent = `⏳ 正在切换到 ${sw.target || '…'}，请稍候（约 3~8 秒，期间无法对话）`;
    errBox.hidden = false;
  } else if (errs.length) {
    errBox.className = 'notice ' + (ms.health === 'ok' ? 'notice-warn' : 'notice-err');
    errBox.textContent = '⚠ ' + errs.join('；');
    errBox.hidden = false;
  } else {
    errBox.hidden = true;
  }

  setText('st-time', '更新于 ' + new Date().toLocaleTimeString('zh-CN'));

  // 模型列表：首次加载，或切换结束后刷新
  if (!modelListLoaded || (!switching && modelListStale)) loadModels();

  updateSendButton();
}

/* ══ 模型列表与切换（阶段 S3）══════════════════════════════════════════ */

async function loadModels() {
  const sel = $('model-select');
  if (!sel) return;
  try {
    const resp = await fetch('/api/models', { cache: 'no-store' });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const d = await resp.json();
    renderModelSelect(d, sel);
    modelListLoaded = true;
    modelListStale = false;
  } catch (e) {
    sel.innerHTML = '<option>（模型列表加载失败）</option>';
  }
}

function renderModelSelect(d, sel) {
  sel.innerHTML = '';
  const models = d.models || [];
  if (!models.length) {
    sel.innerHTML = '<option>（注册表里没有模型）</option>';
    return;
  }
  models.forEach(m => {
    const opt = document.createElement('option');
    const size = m.size_mb ? ` · ${(m.size_mb / 1024).toFixed(1)}G` : '';
    const state = m.installed ? '' : '（未下载）';
    opt.value = m.id;
    opt.textContent = `${m.name}${size}${state}`;
    opt.disabled = !m.installed;                  // ⭐ 未安装的灰化
    opt.dataset.active = m.is_active ? '1' : '';
    if (m.is_active) opt.selected = true;
    sel.appendChild(opt);
  });
  sel.disabled = switching;
  const cur = models.find(m => m.is_active);
  sel.title = cur ? `当前：${cur.name}` : '选择模型';
}

async function switchModel(modelId) {
  if (switching) { toast('正在切换中，请稍候'); return; }

  const sel = $('model-select');
  switching = true;
  sel.disabled = true;
  updateSendButton();

  const errBox = $('status-errors');
  errBox.className = 'notice notice-warn';
  errBox.textContent = `⏳ 正在切换到 ${modelId}…（约 3~8 秒，期间无法对话）`;
  errBox.hidden = false;
  toast('正在切换模型，请稍候…', 5000);

  try {
    const resp = await fetch('/api/models/switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: modelId }),
    });
    const d = await resp.json();

    if (d.ok) {
      toast(`✅ 已切换到 ${d.active}（耗时 ${(d.elapsed_ms / 1000).toFixed(1)} 秒）`, 4500);
    } else {
      // ⭐ 失败时把下拉框回退到实际生效的模型 —— 不能显示错的
      toast('❌ ' + d.message, 7000);
      errBox.className = 'notice notice-err';
      errBox.textContent = '❌ 切换失败：' + d.message;
      errBox.hidden = false;
    }
  } catch (e) {
    toast('❌ 切换请求失败：' + e.message, 7000);
  } finally {
    switching = false;
    sel.disabled = false;
    modelListStale = true;
    // 等模型服务状态稳定后再刷新（刚启动时 /health 可能还没 ok）
    setTimeout(async () => { await loadModels(); await refreshStatus(); }, 600);
  }
}

function initModelSelect() {
  const sel = $('model-select');
  if (!sel) return;
  sel.disabled = false;
  sel.removeAttribute('title');
  sel.addEventListener('change', () => {
    const opt = sel.options[sel.selectedIndex];
    if (opt && opt.dataset.active === '1') {
      toast('已经是当前模型');
      return;
    }
    switchModel(sel.value);
  });
  loadModels();
}

/* ══════════════════════════════════════════════════════════════════════════
   A：输入文件选择 + Token 预算核算
   ──────────────────────────────────────────────────────────────────────────
   对应题目要求：
     ·「系统能够接收纯提示词，也能够读取指定文件，将文件内容与关联提示词
        组织为模型输入」
     ·「记录聊天模板处理后的实际输入 token 数及输出上限」
     ·「输入提示词、关联文件、系统消息与聊天模板合计控制在 50,000 tokens 以内」
     ·「同时满足模型的上下文容量，为生成预留空间」
   ══════════════════════════════════════════════════════════════════════════ */

let taskList = [];           // 规定任务清单
let attachedFile = null;     // 当前挂载的文件 {id,name,tokens,...}
let currentTaskId = null;    // 当前选中的规定任务
let lastPrepare = null;      // 最近一次 /api/prepare 的结果

/* ── 任务清单 ─────────────────────────────────────────────────────────── */

async function loadTasks() {
  const sel = $('task-template');
  if (!sel) return;
  try {
    const r = await fetch('/api/tasks', { cache: 'no-store' });
    const d = await r.json();
    taskList = d.tasks || [];
    sel.innerHTML = '<option value="">（不使用规定任务）</option>';
    taskList.forEach(t => {
      const o = document.createElement('option');
      o.value = t.id;
      o.textContent = `${t.id}｜${t.title}`;
      sel.appendChild(o);
    });
    if (d.system_message) {
      // 题目推荐的系统消息作为默认值（可编辑，实际版本会被记录）
      const sp = $('sys-prompt-input');
      if (sp) sp.value = d.system_message;
    }
  } catch (e) {
    sel.innerHTML = '<option value="">（任务清单加载失败）</option>';
  }
}

/* ── 文件列表弹窗 ─────────────────────────────────────────────────────── */

async function openFileModal() {
  const modal = $('file-modal');
  const list = $('file-list');
  const hint = $('file-modal-hint');
  modal.hidden = false;
  list.innerHTML = '';
  hint.textContent = '加载中…';

  try {
    const r = await fetch('/api/files', { cache: 'no-store' });
    const d = await r.json();
    const files = d.files || [];

    if (!files.length) {
      hint.textContent = '没有可选文件。可把资料放到 uploads 目录后刷新。';
      return;
    }

    const taskFiles = files.filter(f => f.source === 'task');
    const upFiles = files.filter(f => f.source === 'upload');
    hint.textContent =
      `共 ${files.length} 个文件（题包 ${taskFiles.length} 个，上传 ${upFiles.length} 个）`;

    // 先渲染条目，token 数逐个异步补上（避免一次性等所有计数）
    const groups = [
      ['题包自带输入文档（只读）', taskFiles],
      ['上传的资料', upFiles],
    ];
    groups.forEach(([title, arr]) => {
      if (!arr.length) return;
      const h = document.createElement('div');
      h.className = 'file-group';
      h.textContent = title;
      list.appendChild(h);

      arr.forEach(f => {
        const row = document.createElement('button');
        row.className = 'file-row';
        row.innerHTML = `<span class="file-name">${f.name}</span>
          <span class="file-tok" data-fid="${f.id}">统计中…</span>`;
        row.addEventListener('click', () => pickFile(f));
        list.appendChild(row);
        fetchFileTokens(f);
      });
    });
  } catch (e) {
    hint.textContent = '加载失败：' + e.message;
  }
}

async function fetchFileTokens(f) {
  const el = document.querySelector(`.file-tok[data-fid="${CSS.escape(f.id)}"]`);
  if (!el) return;
  try {
    const r = await fetch('/api/files/meta?id=' + encodeURIComponent(f.id),
                          { cache: 'no-store' });
    const d = await r.json();
    if (d.error) { el.textContent = '读取失败'; return; }
    el.textContent = `${d.tokens.toLocaleString()} tok`;
    el.dataset.tokens = d.tokens;
    f.tokens = d.tokens;
    f.sha256 = d.sha256;
  } catch (e) {
    el.textContent = '失败';
  }
}

function closeFileModal() { $('file-modal').hidden = true; }

function pickFile(f) {
  attachedFile = { id: f.id, name: f.name, source: f.source };
  $('attached-name').textContent = f.name;
  $('attached-meta').textContent =
    `${f.source === 'task' ? '题包' : '上传'}`
    + (f.tokens ? ` · ${f.tokens.toLocaleString()} tok` : '');
  $('attached-file').hidden = false;
  closeFileModal();
  // 如果已选规定任务，任务本身带文件 → 重新核算
  refreshPrepare();
}

function detachFile() {
  attachedFile = null;
  $('attached-file').hidden = true;
  refreshPrepare();
}

/* ── 预算核算 ─────────────────────────────────────────────────────────── */

function setBudgetUI(t, b, note) {
  const f = (v) => (v === null || v === undefined ? '—' : v.toLocaleString());
  setText('budget-sys', f(t.system));
  setText('budget-doc', f(t.doc));
  setText('budget-task', f(t.task));
  setText('budget-tpl', f(t.template_overhead));
  setText('budget-in', f(b.total_input_tokens));
  setText('budget-out', f(b.max_output_tokens));
  setText('budget-head', f(b.headroom_tokens));

  const bar = $('budget-bar');
  if (bar) {
    // 进度条按"输入占上限的比例"显示（上限 50000）
    const pct = Math.min(100, (b.total_input_tokens / b.limit_total) * 100);
    bar.style.width = pct.toFixed(2) + '%';
    bar.className = b.ok ? '' : 'over';
  }
  const noteEl = $('budget-note');
  if (noteEl) {
    noteEl.textContent = note || '';
    noteEl.className = 'hint' + (b.ok ? '' : ' err');
  }
}

function clearBudgetUI() {
  ['budget-sys', 'budget-doc', 'budget-task', 'budget-tpl',
   'budget-in', 'budget-out', 'budget-head'].forEach(id => setText(id, null));
  const bar = $('budget-bar');
  if (bar) { bar.style.width = '0%'; bar.className = ''; }
  setText('budget-note', '选中任务或文件后自动核算');
}

/**
 * 请求 /api/prepare，把结果显示在预算面板上。
 * 这是预算的【唯一来源】—— 数字都来自后端的精确 token 计数，
 * 前端不做估算（题目要求"按所选模型 tokenizer 实测"）。
 */
async function refreshPrepare() {
  const taskId = currentTaskId;
  const fileId = attachedFile ? attachedFile.id : null;

  if (!taskId && !fileId) { clearBudgetUI(); return; }

  const body = { max_output_tokens: parseNum('gen-max-tokens', 2048) };
  if (taskId) body.task_id = taskId;
  else {
    body.prompt = $('chat-input').value.trim();
    if (!body.prompt) {
      setText('budget-note', '已挂文件；请输入任务提示词后核算');
      return;
    }
  }
  if (fileId && !taskId) body.file_id = fileId;   // 规定任务的文件由后端决定
  const sm = $('sys-prompt-input').value;
  if (sm !== null) body.system_message = sm === '' ? '' : sm;

  setText('budget-note', '核算中…');
  try {
    const r = await fetch('/api/prepare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!d.ok && d.error) {
      setText('budget-note', '核算失败：' + (d.message || d.error));
      $('budget-note').className = 'hint err';
      return;
    }
    lastPrepare = d;
    setBudgetUI(d.tokens, d.budget,
      d.budget.ok
        ? `✅ ${d.task.id || '自定义'} 预算通过（计数模型 ${d.count_model}）`
        : '❌ ' + d.budget.problems.join('；'));
    return d;
  } catch (e) {
    setText('budget-note', '核算请求失败：' + e.message);
    $('budget-note').className = 'hint err';
  }
}

function parseNum(id, dflt) {
  const v = parseInt($(id)?.value, 10);
  return Number.isFinite(v) ? v : dflt;
}

/* ── 载入规定任务 ─────────────────────────────────────────────────────── */

async function applyTask() {
  const sel = $('task-template');
  const id = sel.value;
  if (!id) { toast('请先选择一个任务'); return; }

  const t = taskList.find(x => x.id === id);
  if (!t) return;

  currentTaskId = id;
  // 任务区提示词自动填进输入框，用户可直接发送
  $('chat-input').value = t.prompt;

  // 任务自带关联文件时，右侧显示出来
  if (t.file) {
    attachedFile = { id: null, name: t.file, source: 'task' };
    $('attached-name').textContent = t.file;
    $('attached-meta').textContent = '（由任务自动关联）';
    $('attached-file').hidden = false;
  } else {
    attachedFile = null;
    $('attached-file').hidden = true;
  }

  const d = await refreshPrepare();
  if (d && d.doc) {
    // ⚠️ token 数在 d.tokens.doc，不在 d.doc.tokens —— 别读错层级
    const toks = (d.tokens && d.tokens.doc) ? d.tokens.doc : null;
    $('attached-meta').textContent =
      `题包 · ${toks !== null ? toks.toLocaleString() + ' tok' : '—'}`
      + ` · ${d.doc.chars} 字符`;
  }
  toast(`已载入任务 ${id}：${t.title}`, 3200);
}

/* ── 初始化 ───────────────────────────────────────────────────────────── */

function initFilesAndBudget() {
  $('btn-pick-file').addEventListener('click', openFileModal);
  $('btn-close-file-modal').addEventListener('click', closeFileModal);
  $('btn-close-file-modal2').addEventListener('click', closeFileModal);
  $('file-modal').addEventListener('click', (e) => {
    if (e.target.id === 'file-modal') closeFileModal();
  });
  $('btn-detach').addEventListener('click', detachFile);

  const sel = $('task-template');
  sel.addEventListener('change', () => {
    $('btn-apply-task').disabled = !sel.value;
    currentTaskId = sel.value || null;
    if (!sel.value) clearBudgetUI();
  });
  $('btn-apply-task').addEventListener('click', applyTask);

  // 输出上限变化 → 重新核算（因为它影响"是否为生成留出空间"）
  $('gen-max-tokens').addEventListener('change', refreshPrepare);

  loadTasks();
}

/* ══════════════════════════════════════════════════════════════════════════
   结果页：浏览测试运行
   ──────────────────────────────────────────────────────────────────────────
   数据来源：results/ 下的真实目录 + runs.json 登记表
   设计原则：⭐ 以真实目录为准。三种状态都显式显示，不静默出错：
       ok          目录齐全，summary.json 可读
       incomplete  有目录但缺关键文件（跑到一半被中断）
       missing_dir 登记表有记录，目录没了
   ══════════════════════════════════════════════════════════════════════════ */

let runsAll = [];            // 全部运行（来自 API）
let picked = [];             // 勾选对比的 run_id（最多 2 个）
let currentRun = null;       // 当前查看的 run_id
let currentRunData = null;   // 当前运行的详情缓存
let compareData = null;      // 对比结果缓存
let activeRTab = 'summary';
let reqPage = { offset: 0, limit: 50, total: 0, only: null };

/* ── 工具 ─────────────────────────────────────────────────────────────── */

const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function fmtNum(v, digits = 0, suffix = '') {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return Number(v).toFixed(digits) + suffix;
}

function fmtTime(iso) {
  if (!iso) return '—';
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : String(iso);
}

function fmtStatus(status) {
  const map = {
    success: ['✅', '成功'], failed: ['❌', '失败'],
    timeout: ['⏰', '超时'], incomplete: ['⚠️', '中断'],
    cancelled: ['⏹', '取消'], harness_error: ['🐞', '程序错误'],
  };
  const [icon, text] = map[status] || ['?', status || '未知'];
  return `<span class="st st-${esc(status)}">${icon} ${esc(text)}</span>`;
}

/* ── 刷新运行列表 ─────────────────────────────────────────────────────── */

async function refreshRuns(showToast = true) {
  const meta = $('runs-meta');
  const list = $('runs-list');
  if (meta) meta.textContent = '扫描中…';
  try {
    const r = await fetch('/api/bench/runs', { cache: 'no-store' });
    const d = await r.json();
    runsAll = d.runs || [];
    if (meta) {
      meta.textContent = d.root_exists
        ? `共 ${d.count} 个运行，${d.count_usable} 个可用`
        : '结果目录不存在（还没跑过测试）';
    }
    buildRunFilters();
    renderRunsList();
    if (showToast) toast(`已刷新：${runsAll.length} 个运行`, 2200);
  } catch (e) {
    if (meta) meta.textContent = '加载失败：' + e.message;
    if (list) list.innerHTML = '';
  }
}

function buildRunFilters() {
  const labels = [...new Set(runsAll.map(r => r.label).filter(Boolean))].sort();
  const concs = [...new Set(runsAll.flatMap(r => r.concurrency || []))]
    .sort((a, b) => a - b);
  const fill = (sel, items, fmt) => {
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="">全部</option>';
    items.forEach(v => {
      const o = document.createElement('option');
      o.value = String(v);
      o.textContent = fmt ? fmt(v) : String(v);
      sel.appendChild(o);
    });
    if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
  };
  fill($('run-filter-label'), labels);
  fill($('run-filter-conc'), concs, v => `并发 ${v}`);
}

function filteredRuns() {
  const lb = $('run-filter-label')?.value || '';
  const cc = $('run-filter-conc')?.value || '';
  const st = $('run-filter-status')?.value || '';
  return runsAll.filter(r => {
    if (lb && r.label !== lb) return false;
    if (cc && !(r.concurrency || []).map(String).includes(cc)) return false;
    if (st && r.status !== st) return false;
    return true;
  });
}

function renderRunsList() {
  const box = $('runs-list');
  if (!box) return;
  const rows = filteredRuns();
  if (!rows.length) {
    box.innerHTML = '<div class="hint" style="padding:12px">没有匹配的运行</div>';
    return;
  }
  box.innerHTML = '';
  rows.forEach(r => {
    const el = document.createElement('div');
    el.className = 'run-item' + (r.run_id === currentRun ? ' active' : '')
      + (r.status !== 'ok' ? ' bad' : '');
    const isPicked = picked.includes(r.run_id);
    const brief = r.by_concurrency_brief || {};
    const bits = Object.keys(brief).sort((a, b) => a - b).map(k => {
      const v = brief[k];
      return `<span class="rb"><b>c${esc(k)}</b> ${fmtNum(v.ttft_median_ms)}ms · ${fmtNum(v.throughput_tps)}t/s</span>`;
    }).join('');

    el.innerHTML = `
      <div class="run-top">
        <label class="run-pick" title="勾选两个进行对比">
          <input type="checkbox" ${isPicked ? 'checked' : ''}>
        </label>
        <div class="run-title">
          <div class="run-label">${esc(r.label || r.run_id)}</div>
          <div class="run-id">${esc(r.run_id)}</div>
        </div>
      </div>
      <div class="run-line">${r.status === 'ok' ? '✅' : '⚠️'} ${esc(r.status_text || '')}</div>
      <div class="run-line dim">
        ${esc(r.model || '—')} · ctx ${esc(r.n_ctx ?? '—')} · ${esc(r.total_slots ?? '—')} slot
        ${r.vram_after_load_mib ? ' · 显存 ' + esc(r.vram_after_load_mib) + ' MiB' : ''}
      </div>
      <div class="run-line dim">
        ${esc(fmtTime(r.started_at))}
        ${r.duration_s ? ' · ' + fmtNum(r.duration_s) + 's' : ''}
        ${r.n_requests ? ' · ' + esc(r.n_requests) + ' 请求' : ''}
        ${(r.success_rate !== undefined && r.success_rate !== null)
          ? ' · 成功率 ' + fmtNum(r.success_rate * 100, 1) + '%' : ''}
      </div>
      ${bits ? `<div class="run-brief">${bits}</div>` : ''}
      ${r.model_verified === false
        ? '<div class="run-line warn">⚠️ 模型校验未通过（配置与实跑模型不一致）</div>' : ''}
    `;
    el.addEventListener('click', (ev) => {
      if (ev.target.matches('input[type=checkbox]')) return;
      selectRun(r.run_id);
    });
    el.querySelector('input[type=checkbox]')?.addEventListener('change', (ev) => {
      ev.stopPropagation();
      togglePick(r.run_id);
    });
    box.appendChild(el);
  });
  renderCompareBar();
}

function togglePick(runId) {
  if (picked.includes(runId)) {
    picked = picked.filter(x => x !== runId);
  } else {
    if (picked.length >= 2) {
      toast('最多选两个（先取消一个）');
      renderRunsList();
      return;
    }
    picked.push(runId);
  }
  renderCompareBar();
}

function renderCompareBar() {
  const txt = $('compare-picked');
  const btn = $('btn-do-compare');
  if (txt) txt.textContent = `已选 ${picked.length} / 2`;
  if (btn) btn.disabled = picked.length !== 2;
}

/* ── 选中一个运行 ─────────────────────────────────────────────────────── */

async function selectRun(runId) {
  currentRun = runId;
  currentRunData = null;
  compareData = null;
  reqPage = { offset: 0, limit: 50, total: 0, only: null };
  renderRunsList();
  setRTab(activeRTab || 'summary', true);

  const body = $('results-body');
  body.innerHTML = '<div class="hint" style="padding:16px">加载中…</div>';
  try {
    const r = await fetch('/api/bench/runs/' + encodeURIComponent(runId),
                          { cache: 'no-store' });
    const d = await r.json();
    if (d.error) throw new Error(d.message || d.error);
    currentRunData = d;
    renderRTab();
  } catch (e) {
    body.innerHTML = `<div class="notice notice-err">加载失败：${esc(e.message)}</div>`;
  }
}

function setRTab(name, silent = false) {
  activeRTab = name;
  document.querySelectorAll('#results-tabs .tab2').forEach(b =>
    b.classList.toggle('active', b.dataset.rtab === name));
  if (!silent) renderRTab();
}

function renderRTab() {
  const body = $('results-body');
  if (!body) return;
  if (activeRTab === 'compare') return renderCompareTab();
  if (!currentRunData) return;
  switch (activeRTab) {
    case 'summary': return renderSummaryTab();
    case 'requests': return renderRequestsTab();
    case 'errors': return renderErrorsTab();
    case 'config': return renderConfigTab();
  }
}

/* ── 标签页 1：汇总 ───────────────────────────────────────────────────── */

function renderSummaryTab() {
  const d = currentRunData;
  const s = d.summary || {};
  const m = d.meta || {};
  const body = $('results-body');
  const formal = s.formal || {};
  const keys = Object.keys(formal).sort((a, b) => a - b);

  const rows = keys.map(k => {
    const f = formal[k];
    const bd = f.status_breakdown || {};
    const bad = (bd.failed || 0) + (bd.incomplete || 0)
      + (bd.timeout || 0) + (bd.harness_error || 0);
    return `<tr>
      <td><b>${esc(k)}</b></td>
      <td class="${f.success_rate === 1 ? 'good' : 'bad'}">
        ${esc(f.n_success)}/${esc(f.n_requests)}${bad ? ` <span class="dim">(${bad} 异常)</span>` : ''}
      </td>
      <td>${fmtNum(f.ttft_ms?.median)} / ${fmtNum(f.ttft_ms?.p95)}</td>
      <td>${fmtNum(f.tpot_excl_first_ms?.median, 2)}</td>
      <td>${fmtNum(f.throughput?.output_tokens_per_s, 1)}</td>
      <td>${fmtNum(f.total_ms?.median)} / ${fmtNum(f.total_ms?.p95)}</td>
      <td>${fmtNum(f.vram_peak_mib)}</td>
    </tr>`;
  }).join('');

  const cs = s.cold_start;
  const warm = (s.warmup || []).map(w => fmtNum(w.ttft_ms) + 'ms').join(' / ');

  body.innerHTML = `
    <div class="rt-head">
      <div>
        <div class="rt-title">${esc(s.label || d.run_id)}</div>
        <div class="dim mono">${esc(d.run_id)}</div>
      </div>
      <div class="dim">
        ${esc(m.model?.model_name || '—')} · 量化 ${esc(m.model?.quant || '—')}
        · ctx ${esc(m.model?.n_ctx ?? '—')} · ${esc(m.model?.total_slots ?? '—')} slot
      </div>
    </div>

    <div class="rt-cards">
      <div class="rc"><span>总耗时</span><b>${fmtNum(s.duration_s)}s</b></div>
      <div class="rc"><span>请求总数</span><b>${esc(s.overall?.n_requests ?? '—')}</b></div>
      <div class="rc"><span>整体成功率</span>
        <b class="${s.overall?.success_rate === 1 ? 'good' : 'bad'}">
          ${fmtNum((s.overall?.success_rate ?? 0) * 100, 1)}%</b></div>
      <div class="rc"><span>冷启动 TTFT</span><b>${fmtNum(cs?.ttft_ms)}ms</b></div>
      <div class="rc"><span>预热后 TTFT</span><b>${warm || '—'}</b></div>
      <div class="rc"><span>加载后显存</span>
        <b>${fmtNum(m.server_config?.vram_after_load_mib)} MiB</b></div>
    </div>

    <h4 class="rt-h">各并发档指标</h4>
    <table class="grid">
      <thead><tr>
        <th>并发</th><th>成功/总</th>
        <th>TTFT 中位 / P95 (ms)</th><th>TPOT (ms)</th>
        <th>吞吐 (tok/s)</th><th>完成 中位 / P95 (ms)</th><th>显存峰 (MiB)</th>
      </tr></thead>
      <tbody>${rows || '<tr><td colspan="7">无数据</td></tr>'}</tbody>
    </table>

    <div class="rt-note">
      <b>口径说明</b>：成功率分母为该并发档的请求总数；
      只有拿到 <code>finish_reason</code> 的请求才算成功；
      吞吐窗口用该档实测墙钟时间，失败/中断请求不计入 token 吞吐。
    </div>
  `;
}

/* ── 标签页 2：逐请求 ─────────────────────────────────────────────────── */

function renderRequestsTab() {
  const body = $('results-body');
  body.innerHTML = `
    <div class="rt-head">
      <div><div class="rt-title">逐请求记录</div>
        <div class="dim">共 ${esc(currentRunData.n_requests_recorded ?? '—')} 条原始记录</div></div>
      <div class="req-tools">
        <label class="chk"><input type="checkbox" id="req-only-bad"
          ${reqPage.only === 'bad' ? 'checked' : ''}> 只看异常</label>
      </div>
    </div>
    <div id="req-table-wrap"><div class="hint" style="padding:12px">加载中…</div></div>
  `;
  $('req-only-bad')?.addEventListener('change', (e) => {
    reqPage.only = e.target.checked ? 'bad' : null;
    loadRequests(0);
  });
  loadRequests(0);
}

async function loadRequests(offset = 0) {
  if (!currentRun) return;
  const wrap = $('req-table-wrap');
  const q = new URLSearchParams({ offset: String(offset), limit: String(reqPage.limit) });
  if (reqPage.only) q.set('only', reqPage.only);
  try {
    const r = await fetch(`/api/bench/runs/${encodeURIComponent(currentRun)}/requests?${q}`,
                          { cache: 'no-store' });
    const j = await r.json();
    if (j.error) throw new Error(j.message);
    reqPage.offset = j.offset;
    reqPage.total = j.total;
    renderRequestRows(j.rows);
  } catch (e) {
    if (wrap) wrap.innerHTML = `<div class="notice notice-err">加载失败：${esc(e.message)}</div>`;
  }
}

function renderRequestRows(rows) {
  const wrap = $('req-table-wrap');
  if (!wrap) return;
  if (!rows.length) {
    wrap.innerHTML = '<div class="hint" style="padding:12px">没有记录</div>';
    return;
  }
  const trs = rows.map(r => `
    <tr data-rid="${esc(r.request_id)}" class="req-row">
      <td class="mono">${esc(r.request_id)}</td>
      <td>${esc(r.task_id || '—')}</td>
      <td>${esc(r.concurrency ?? '—')}</td>
      <td>${fmtStatus(r.status)}</td>
      <td>${fmtNum(r.ttft_ms)}</td>
      <td>${fmtNum(r.tpot_excl_first_ms, 2)}</td>
      <td>${esc(r.prompt_tokens ?? '—')}</td>
      <td>${esc(r.output_tokens ?? '—')}</td>
      <td>${esc(r.finish_reason || '—')}</td>
      <td class="prev">${esc((r.output_preview || '').replace(/\n/g, ' '))}</td>
    </tr>`).join('');

  const from = reqPage.offset + 1;
  const to = reqPage.offset + rows.length;
  wrap.innerHTML = `
    <div class="pager">
      <button class="btn btn-tiny" id="req-prev" ${reqPage.offset > 0 ? '' : 'disabled'}>← 上一页</button>
      <span class="dim">${from}–${to} / ${reqPage.total}</span>
      <button class="btn btn-tiny" id="req-next" ${to < reqPage.total ? '' : 'disabled'}>下一页 →</button>
    </div>
    <table class="grid compact">
      <thead><tr>
        <th>request_id</th><th>任务</th><th>并发</th><th>状态</th>
        <th>TTFT ms</th><th>TPOT ms</th><th>入tok</th><th>出tok</th>
        <th>finish</th><th>输出预览</th>
      </tr></thead>
      <tbody>${trs}</tbody>
    </table>
    <div class="hint" style="padding:8px 2px">点任意一行查看该请求的完整记录与模型输出。</div>
  `;
  wrap.querySelectorAll('.req-row').forEach(tr =>
    tr.addEventListener('click', () => showRequestDetail(tr.dataset.rid)));
  $('req-prev')?.addEventListener('click',
    () => loadRequests(Math.max(0, reqPage.offset - reqPage.limit)));
  $('req-next')?.addEventListener('click',
    () => loadRequests(reqPage.offset + reqPage.limit));
}

async function showRequestDetail(requestId) {
  const mask = document.createElement('div');
  mask.className = 'modal-mask';
  mask.innerHTML = `
    <div class="modal wide">
      <div class="modal-head">
        <h3>请求详情 <span class="mono dim">${esc(requestId)}</span></h3>
        <button class="btn btn-tiny" data-close>✕</button>
      </div>
      <div class="modal-body"><div class="hint">加载中…</div></div>
    </div>`;
  document.body.appendChild(mask);
  const close = () => mask.remove();
  mask.querySelector('[data-close]').addEventListener('click', close);
  mask.addEventListener('click', (e) => { if (e.target === mask) close(); });

  const kv = (o) => Object.entries(o)
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => `<tr><td class="dim">${esc(k)}</td><td class="mono">${esc(v)}</td></tr>`)
    .join('') || '<tr><td class="dim">（无）</td></tr>';

  try {
    const r = await fetch(`/api/bench/runs/${encodeURIComponent(currentRun)}/request/${encodeURIComponent(requestId)}`,
                          { cache: 'no-store' });
    const d = await r.json();
    if (d.error) throw new Error(d.message);
    mask.querySelector('.modal-body').innerHTML = `
      <div class="detail-grid">
        <div>
          <h4 class="rt-h">基本信息</h4>
          <table class="kv">${kv({
            request_id: d.request_id, task_id: d.task_id,
            concurrency: d.concurrency, phase: d.phase,
            status: d.status, http_status: d.http_status,
            finish_reason: d.finish_reason,
            error_type: d.error_type, error_message: d.error_message,
          })}</table>
        </div>
        <div>
          <h4 class="rt-h">时间与指标</h4>
          <table class="kv">${kv({
            'ttft_ms（客户端）': fmtNum(d.ttft_ms, 1),
            'total_ms（客户端）': fmtNum(d.total_ms, 1),
            'tpot_excl_first_ms': fmtNum(d.tpot_excl_first_ms, 3),
            'tpot_incl_first_ms': fmtNum(d.tpot_incl_first_ms, 3),
            'prompt_ms（服务端 prefill）': fmtNum(d.prompt_ms, 2),
            'predicted_ms（服务端 decode）': fmtNum(d.predicted_ms, 2),
          })}</table>
        </div>
        <div>
          <h4 class="rt-h">token 数</h4>
          <table class="kv">${kv({
            prompt_tokens: d.prompt_tokens,
            prompt_tokens_source: d.prompt_tokens_source,
            output_tokens: d.output_tokens,
            output_tokens_source: d.output_tokens_source,
            cache_hit_tokens: d.cache_hit_tokens,
            prompt_n_computed: d.prompt_n_computed,
            output_chars: d.output_chars,
            n_chunks: d.n_chunks,
          })}</table>
        </div>
      </div>
      <h4 class="rt-h">模型输出原文</h4>
      <pre class="output">${esc(d.output_text || '（无输出）')}</pre>
      <div class="hint" style="padding:8px 2px">
        客户端时间由本地计时得到（含排队与传输）；服务端时间来自 llama.cpp 的
        timings 字段，两者口径不同。
      </div>`;
  } catch (e) {
    mask.querySelector('.modal-body').innerHTML =
      `<div class="notice notice-err">加载失败：${esc(e.message)}</div>`;
  }
}

/* ── 标签页 3：错误 ───────────────────────────────────────────────────── */

async function renderErrorsTab() {
  const body = $('results-body');
  body.innerHTML = '<div class="hint" style="padding:16px">加载中…</div>';
  try {
    const r = await fetch(`/api/bench/runs/${encodeURIComponent(currentRun)}/errors`,
                          { cache: 'no-store' });
    const d = await r.json();
    if (d.error) throw new Error(d.message);
    if (!d.n) {
      body.innerHTML = `
        <div class="rt-head"><div class="rt-title">错误记录</div></div>
        <div class="notice notice-ok">✅ 本次运行没有失败、超时或中断的请求</div>
        <div class="rt-note">
          状态分类：<b>成功</b>（拿到 finish_reason）／<b>中断</b>（流在结束标记前断开）／
          <b>失败</b>（HTTP 错误）／<b>超时</b>／<b>程序错误</b>（测试程序自身 bug）。
          只有第一类计入成功率与延迟统计。
        </div>`;
      return;
    }
    const trs = d.errors.map(e => `
      <tr>
        <td class="mono">${esc(e.request_id)}</td>
        <td>${esc(e.task_id || '—')}</td>
        <td>${esc(e.concurrency ?? '—')}</td>
        <td>${fmtStatus(e.status)}</td>
        <td>${esc(e.error_type || '—')}</td>
        <td class="prev">${esc((e.error_message || '').slice(0, 120))}</td>
      </tr>`).join('');
    body.innerHTML = `
      <div class="rt-head"><div>
        <div class="rt-title">错误记录</div>
        <div class="dim">共 ${d.n} 条非成功请求</div></div></div>
      <table class="grid">
        <thead><tr><th>request_id</th><th>任务</th><th>并发</th>
          <th>状态</th><th>错误类型</th><th>错误信息</th></tr></thead>
        <tbody>${trs}</tbody>
      </table>`;
  } catch (e) {
    body.innerHTML = `<div class="notice notice-err">加载失败：${esc(e.message)}</div>`;
  }
}

/* ── 标签页 4：配置 ───────────────────────────────────────────────────── */

function renderConfigTab() {
  const d = currentRunData;
  const body = $('results-body');
  const cfg = d.run_config || {};
  const m = d.meta || {};
  const lc = m.load_config || {};
  const sc = m.server_config || {};
  const hw = m.hardware || {};

  const block = (title, obj) => {
    const rows = Object.entries(obj)
      .filter(([k]) => !k.startsWith('_'))
      .map(([k, v]) => `<tr><td class="dim">${esc(k)}</td>
        <td class="mono">${esc(typeof v === 'object' ? JSON.stringify(v) : v)}</td></tr>`)
      .join('') || '<tr><td class="dim">（无）</td></tr>';
    return `<h4 class="rt-h">${esc(title)}</h4><table class="kv">${rows}</table>`;
  };

  const idx = d.prompt_index || {};
  const idxRows = Object.entries(idx).map(([k, v]) => `<tr>
    <td><b>${esc(k)}</b></td>
    <td>${esc(v.kind)}</td>
    <td>${esc(v.file || '—')}</td>
    <td class="mono dim">${esc((v.file_sha256 || '').slice(0, 16) || '—')}</td>
    <td>${esc(v.tokens?.counted_total_input ?? v.tokens?.sum_without_template ?? '—')}</td>
    <td class="dim">${esc(v.tokens?.template_overhead ?? '—')}</td>
  </tr>`).join('');

  body.innerHTML = `
    <div class="rt-head"><div>
      <div class="rt-title">配置与追踪</div>
      <div class="dim">运行编号 / 提示词编号 / 文件校验值 / 配置 在这里互相对应</div>
    </div></div>

    <div class="cfg-cols">
      <div>${block('运行配置（run-config.json）', cfg)}</div>
      <div>
        ${block('负载配置（实际生效值）', lc)}
        ${block('服务端启动参数', sc.server_args || {})}
      </div>
      <div>
        ${block('硬件', { cpu: hw.cpu, gpu: hw.gpu, cuda_version: hw.cuda_version,
                          mem_total_mib: hw.mem_total_mib, python: hw.python })}
        ${block('模型与校验', { model_name: m.model?.model_name,
                                quant: m.model?.quant, n_ctx: m.model?.n_ctx,
                                total_slots: m.model?.total_slots,
                                build_info: m.model?.build_info,
                                model_verified: JSON.stringify(m.model_verified) })}
      </div>
    </div>

    <h4 class="rt-h">提示词索引（任务编号 ↔ 文件 ↔ 校验值 ↔ token 数）</h4>
    <table class="grid compact">
      <thead><tr><th>任务</th><th>类型</th><th>关联文件</th>
        <th>文件 sha256（前16位）</th><th>输入 token</th><th>模板开销</th></tr></thead>
      <tbody>${idxRows || '<tr><td colspan="6">无</td></tr>'}</tbody>
    </table>

    <div class="rt-note">
      <b>为什么要有这一页</b>：题目要求「请求 ID、运行编号、提示词编号、配置和数据能够相互追踪」。
      这里把四者放在一起：运行编号在标题、配置在左侧、提示词编号与文件校验值在下方表格。
    </div>
  `;
}

/* ── 标签页 5：对比 ───────────────────────────────────────────────────── */

async function doCompare() {
  if (picked.length !== 2) { toast('请先勾选两个运行'); return; }
  setRTab('compare');
  const body = $('results-body');
  body.innerHTML = '<div class="hint" style="padding:16px">对比中…</div>';
  try {
    const r = await fetch(`/api/bench/compare?a=${encodeURIComponent(picked[0])}&b=${encodeURIComponent(picked[1])}`,
                          { cache: 'no-store' });
    const d = await r.json();
    if (d.error) throw new Error(d.message);
    compareData = d;
    renderRTab();
  } catch (e) {
    body.innerHTML = `<div class="notice notice-err">对比失败：${esc(e.message)}</div>`;
  }
}

function renderCompareTab() {
  const body = $('results-body');
  if (!compareData) {
    body.innerHTML = `<div class="empty-state">
      <p>还没有对比结果。</p>
      <p class="hint">在左侧勾选两个运行，然后点"对比"。</p></div>`;
    return;
  }
  const d = compareData;
  const concs = Object.keys(d.metrics).sort((a, b) => a - b);
  // ⚠️ 这里曾经用"颜色标改善/恶化"，但那是错的，原因：
  //    优化往往是【权衡】——同一轮里 c1 的 TTFT 变差、吞吐变好，
  //    c10 又反过来。把"越大越好/越小越好"硬编码进指标定义，
  //    会在某些并发档上给出相反的（误导的）颜色。
  //    所以改成中性标注：↑ 表示 B 比 A 大，↓ 表示小；
  //    谁好谁坏由读表的人结合业务判断，界面不替他下结论。
  const metricDefs = [
    ['n_success', '成功数', 0],
    ['success_rate', '成功率 %', 1],
    ['ttft_median_ms', 'TTFT 中位 (ms)', 0],
    ['ttft_p95_ms', 'TTFT P95 (ms)', 0],
    ['tpot_median_ms', 'TPOT (ms)', 2],
    ['throughput_tps', '吞吐 (tok/s)', 1],
    ['total_median_ms', '完成中位 (ms)', 0],
    ['total_p95_ms', '完成 P95 (ms)', 0],
    ['vram_peak_mib', '显存峰 (MiB)', 0],
    ['n_incomplete', '中断数', 0],
    ['n_failed', '失败数', 0],
  ];

  const tableHead = `<tr><th>指标</th>${concs.map(c =>
    `<th colspan="3">并发 ${esc(c)}</th>`).join('')}</tr>
    <tr><th></th>${concs.map(() =>
    '<th class="sub">A</th><th class="sub">B</th><th class="sub">Δ</th>').join('')}</tr>`;

  const tableBody = metricDefs.map(([key, label, digits]) => {
    const cells = concs.map(c => {
      const m = d.metrics[c][key];
      if (!m) return '<td>—</td><td>—</td><td>—</td>';
      let a = m.a, b = m.b;
      if (key === 'success_rate') {
        a = a == null ? null : a * 100;
        b = b == null ? null : b * 100;
      }
      const dp = m.delta_pct;
      const arrow = (dp === null || dp === 0) ? ''
        : (dp > 0 ? ' ↑' : ' ↓');
      const dstr = dp === null ? '—'
        : (dp > 0 ? '+' : '') + dp.toFixed(1) + '%' + arrow;
      return `<td>${fmtNum(a, digits)}</td><td>${fmtNum(b, digits)}</td>
              <td class="delta">${dstr}</td>`;
    }).join('');
    return `<tr><td class="dim">${esc(label)}</td>${cells}</tr>`;
  }).join('');

  // 注：这里不再渲染「配置文件差异」表。
  // 原因（用户指出）：它和上方的"服务端参数差异 + 控制变量"重复，
  // 而且 note 这类长中文串会把表格格子撑爆。
  // 完整的运行配置在「配置」标签页里按运行分别列出，不在这里重复堆。
  // 后端 compare() 仍然返回 config_diff 字段，方便脚本取用，只是界面不显示。
  const srvRows = (d.server_args_changed || []).map(x => `<tr>
      <td class="mono">${esc(x.key)}</td>
      <td class="mono dim">${esc(JSON.stringify(x.a))}</td>
      <td class="mono">${esc(JSON.stringify(x.b))}</td></tr>`).join('')
    || '<tr><td colspan="3" class="hint">服务端启动参数完全相同</td></tr>';

  const same = (d.controlled_same || []).map(x => x.key);
  const changed = (d.controlled_changed || []);
  const hintCls = d.conclusion_hint.startsWith('✅') ? 'notice-ok' : 'notice-warn';

  body.innerHTML = `
    <div class="rt-head"><div>
      <div class="rt-title">对比</div>
      <div class="dim">
        <b>A</b> = ${esc(d.a.label || d.a.run_id)}　${esc(d.a.note || '')}<br>
        <b>B</b> = ${esc(d.b.label || d.b.run_id)}　${esc(d.b.note || '')}
      </div>
    </div></div>

    <div class="notice ${hintCls}">${esc(d.conclusion_hint)}</div>

    <h4 class="rt-h">指标对比（A → B，Δ 为变化百分比）</h4>
    <table class="grid compare">
      <thead>${tableHead}</thead>
      <tbody>${tableBody}</tbody>
    </table>

    <div class="cfg-cols">
      <div>
        <h4 class="rt-h">服务端参数差异（机器读到的进程参数）</h4>
        <table class="grid compact">
          <thead><tr><th>参数</th><th>A</th><th>B</th></tr></thead>
          <tbody>${srvRows}</tbody>
        </table>
      </div>
      <div>
        <h4 class="rt-h">控制变量（题目要求保持一致）</h4>
        <table class="grid compact">
          <thead><tr><th>变量</th><th>A</th><th>B</th></tr></thead>
          <tbody>
            ${changed.map(x => `<tr class="bad"><td class="mono">${esc(x.key)}</td>
              <td class="mono dim">${esc(JSON.stringify(x.a))}</td>
              <td class="mono">${esc(JSON.stringify(x.b))}</td></tr>`).join('')}
            <tr><td colspan="3" class="hint">
              一致（${same.length} 个）：${esc(same.join('、'))}</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="rt-note">
      上面两张表覆盖了"改动"与"控制变量"两类差异，足够判断对比是否成立。
      <b>完整的运行配置</b>（含备注、输入文件、提示词索引、硬件信息）
      请看「配置」标签页 —— 那里按运行分别列出，不在这里重复堆一张长表。
    </div>
  `;
}

/* ── 初始化 ───────────────────────────────────────────────────────────── */

function initResults() {
  $('btn-refresh-runs')?.addEventListener('click', () => refreshRuns(true));
  ['run-filter-label', 'run-filter-conc', 'run-filter-status'].forEach(id =>
    $(id)?.addEventListener('change', renderRunsList));
  $('btn-do-compare')?.addEventListener('click', doCompare);
  $('btn-clear-pick')?.addEventListener('click', () => {
    picked = [];
    renderRunsList();
  });
  document.querySelectorAll('#results-tabs .tab2').forEach(b =>
    b.addEventListener('click', () => setRTab(b.dataset.rtab)));
  refreshRuns(false);
}

/* ── Tab 切换 ─────────────────────────────────────────────────────────── */

function initTabs() {
  document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => {
      const name = btn.dataset.tab;
      document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b === btn));
      document.querySelectorAll('.panel').forEach(p =>
        p.classList.toggle('active', p.id === 'panel-' + name));
      try { localStorage.setItem('antlab.tab', name); } catch (_) {}
    });
  });
  let saved = null;
  try { saved = localStorage.getItem('antlab.tab'); } catch (_) {}
  if (saved) {
    const btn = document.querySelector(`.tab[data-tab="${saved}"]`);
    if (btn) btn.click();
  }
}

/* ── 预留元素 ─────────────────────────────────────────────────────────── */

function initTodoElements() {
  document.querySelectorAll('[data-todo]').forEach(el => {
    el.addEventListener('click', (ev) => {
      ev.preventDefault();
      toast('🔒 ' + el.dataset.todo);
    });
  });
  // ⭐ S3 起 model-select 已经实现了，只保留 task-template 为未实现
  const tt = $('task-template');
  if (tt) tt.addEventListener('mousedown', () =>
    toast('🔒 任务模板尚未实现（阶段 2 计划）'));
}

/* ══════════════════════════════════════════════════════════════════════════
   对话：流式渲染（阶段 S2）
   ══════════════════════════════════════════════════════════════════════════ */

const conversation = [];       // [{role, content}] —— 多轮历史
let streaming = false;
let abortCtl = null;

/* ⭐ A：附件轮的输入前缀
   为什么需要：
     挂了文件的那一轮，发给模型的是【系统消息 + 资料区 + 任务区】，
     而 conversation 里只留了用户的那句任务提示词。
     如果不记住这个前缀，第二轮对话时就会【再次拼接资料区】，
     输入 token 直接翻倍，还可能超上下文。
   所以：第一次发送时把完整前缀存下来，后续轮次复用它 + 新增的对话。 */
let sentPrefix = null;         // 已发送的完整 messages 前缀
let sentPrefixLength = 0;      // 前缀对应的 conversation 长度

function resetAttachmentTurn() {
  sentPrefix = null;
  sentPrefixLength = 0;
  lastPrepare = null;
}

/* ── 渲染一条消息 ─────────────────────────────────────────────────────── */

function addMessage(role, text, opts = {}) {
  const empty = $('chat-empty');
  if (empty) empty.remove();

  const wrap = document.createElement('div');
  wrap.className = 'msg ' + role + (opts.error ? ' error' : '');

  const roleEl = document.createElement('div');
  roleEl.className = 'msg-role';
  roleEl.textContent = { user: '你', assistant: '模型', system: '系统' }[role] || role;

  const body = document.createElement('div');
  body.className = 'msg-body';
  body.textContent = text;

  wrap.appendChild(roleEl);
  wrap.appendChild(body);
  $('messages').appendChild(wrap);
  scrollToBottom();
  return body;
}

function scrollToBottom() {
  const m = $('messages');
  m.scrollTop = m.scrollHeight;
}

/* ── 指标显示 ─────────────────────────────────────────────────────────── */

function showMetrics(m) {
  const el = $('metrics');
  const parts = [];
  if (m.ttft !== null)      parts.push(`TTFT ${m.ttft.toFixed(0)} ms`);
  if (m.totalMs !== null)   parts.push(`总耗时 ${(m.totalMs / 1000).toFixed(2)} s`);
  if (m.tokens !== null)    parts.push(`输出 ≈${m.tokens} 块`);
  if (m.tps !== null)       parts.push(`${m.tps.toFixed(1)} 块/s`);
  el.textContent = parts.join('  ·  ');
  el.hidden = parts.length === 0;
}

/* ── 核心：流式发送 ───────────────────────────────────────────────────── */

async function sendMessage() {
  if (streaming) return;

  const input = $('chat-input');
  const text = input.value.trim();
  if (!text) { toast('请输入内容'); return; }
  if (!serverReady) { toast('⚠ 模型服务不可达，无法对话'); return; }

  // 1. 上屏用户消息
  addMessage('user', text);
  input.value = '';
  conversation.push({ role: 'user', content: text });

  // ⭐ A：如果这是挂了文件后的第一轮，记住"这次带上了完整输入"，
  //       后续轮次复用前缀，避免重复拼接资料区
  if (!sentPrefix && lastPrepare) {
    sentPrefixLength = conversation.length;   // 前缀只包含到这条用户消息为止
  }

  // 2. 准备助手气泡与计时
  const bodyEl = addMessage('assistant', '');
  const cursor = document.createElement('span');
  cursor.className = 'cursor';
  bodyEl.appendChild(cursor);

  streaming = true;
  updateSendButton();

  const metrics = { ttft: null, totalMs: null, tokens: null, tps: null };
  const t0 = nowMs();
  let firstAt = null;
  let nChunks = 0;
  let acc = '';
  let gotError = null;

  abortCtl = new AbortController();

  // ⭐ 构造要发给后端的 messages。
  //
  // 优先级说明（这里最容易出错，所以写清楚）：
  //   ① sentPrefix —— 本轮已经发过一次，前缀已确定
  //        用途：多轮对话时不能重复拼接资料区，否则第二轮输入暴涨
  //   ② lastPrepare 有结果（选了规定任务 / 挂了文件）
  //        → 用后端组装好的 messages（系统消息 → 资料区 → 任务区），
  //          它已按题目规定组织，且 token 已核算
  //        → 【不发 system_prompt】，否则控制层会再插一条 system，重复
  //   ③ 普通聊天 → 本地对话历史 + 界面上的系统提示词
  let outMessages;
  let outSystem = null;
  if (sentPrefix) {
    outMessages = sentPrefix.concat(conversation.slice(sentPrefixLength));
  } else if (lastPrepare && lastPrepare.task && lastPrepare.messages) {
    outMessages = lastPrepare.messages.concat(conversation.slice(sentPrefixLength));
    sentPrefix = outMessages.slice();
    sentPrefixLength = outMessages.length;
  } else {
    outMessages = conversation;
    outSystem = $('sys-prompt-input').value;
  }

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: abortCtl.signal,
      body: JSON.stringify({
        messages: outMessages,
        system_prompt: outSystem,
        max_tokens: parseInt($('gen-max-tokens').value, 10) || 512,
        temperature: parseFloat($('gen-temperature').value) || 0.7,
      }),
    });

    if (!resp.ok) {
      // 上游在发响应头之前就失败了 → 后端返回的是 JSON 错误
      let msg = 'HTTP ' + resp.status;
      try {
        const j = await resp.json();
        if (j.message) msg = j.message;
      } catch (_) {}
      throw new Error(msg);
    }

    // ── 流式读取 ───────────────────────────────────────────────────────
    const reader = resp.body.getReader();
    // ⭐ 铁律②：必须传 {stream:true}，否则中文会被切在块边界 → 缺字
    const decoder = new TextDecoder('utf-8');
    let buf = '';                     // ⭐ 铁律③：存放"半截数据"

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buf += decoder.decode(value, { stream: true });

      // SSE 用空行分隔事件
      const parts = buf.split('\n\n');
      buf = parts.pop();              // 最后一段可能不完整，留到下一块

      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith('data:')) continue;

        const data = line.slice(5).trim();
        if (data === '[DONE]') continue;

        let obj;
        try { obj = JSON.parse(data); } catch (_) { continue; }

        // 后端转发的中途错误
        if (obj.error) { gotError = obj.error; continue; }

        const delta = obj.choices?.[0]?.delta?.content;
        if (!delta) continue;

        if (firstAt === null) {
          firstAt = nowMs();
          metrics.ttft = firstAt - t0;
        }
        nChunks++;
        acc += delta;
        // 在光标之前插入文字
        bodyEl.textContent = acc;
        bodyEl.appendChild(cursor);
        scrollToBottom();
      }
    }

    cursor.remove();

    if (gotError) {
      bodyEl.textContent = acc ? acc + '\n\n[流中断] ' + gotError : '[错误] ' + gotError;
      bodyEl.parentElement.classList.add('error');
      conversation.pop();          // 出错的这轮不入历史
    } else if (acc) {
      conversation.push({ role: 'assistant', content: acc });
    } else {
      bodyEl.textContent = '[没有收到内容]';
      bodyEl.parentElement.classList.add('error');
      conversation.pop();
    }

  } catch (e) {
    cursor.remove();
    if (e.name === 'AbortError') {
      bodyEl.textContent = acc ? acc + '\n\n[已停止]' : '[已停止]';
      if (acc) conversation.push({ role: 'assistant', content: acc });
      else conversation.pop();
    } else {
      bodyEl.textContent = '[请求失败] ' + e.message;
      bodyEl.parentElement.classList.add('error');
      conversation.pop();
      toast('❌ ' + e.message, 4200);
    }
  } finally {
    metrics.totalMs = nowMs() - t0;
    if (nChunks > 0) {
      metrics.tokens = nChunks;
      metrics.tps = nChunks / (metrics.totalMs / 1000);
    }
    showMetrics(metrics);
    streaming = false;
    abortCtl = null;
    updateSendButton();
    $('chat-input').focus();
    refreshStatus();               // 让状态栏的 slot 占用及时刷新
  }
}

function stopStreaming() {
  if (abortCtl) {
    abortCtl.abort();
    toast('已发送停止请求');
  }
}

/* ── 按钮状态 ─────────────────────────────────────────────────────────── */

function updateSendButton() {
  const send = $('btn-send');
  const stop = $('btn-stop');
  const input = $('chat-input');
  // ⭐ S3：切换模型期间也禁止发送（后端同样会返回 409，这里做双保险）
  const blocked = streaming || switching;
  if (send) {
    send.disabled = blocked || !serverReady;
    send.textContent = streaming ? '生成中…' : (switching ? '切换模型中…' : '发送');
  }
  if (stop) stop.disabled = !streaming;
  if (input) input.disabled = blocked;
}

/* ── 对话区初始化 ─────────────────────────────────────────────────────── */

function initChat() {
  $('btn-send').addEventListener('click', sendMessage);
  $('btn-stop').addEventListener('click', stopStreaming);

  $('btn-clear').addEventListener('click', () => {
    if (streaming) { toast('正在生成，先点停止'); return; }
    conversation.length = 0;
    const box = $('messages');
    box.innerHTML = '';
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.id = 'chat-empty';
    empty.innerHTML = '<p>还没有对话。</p><p class="hint">在下方输入问题开始。本阶段仅支持纯文本对话。</p>';
    box.appendChild(empty);
    $('metrics').hidden = true;
    toast('对话已清空');
  });

  const input = $('chat-input');
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (!streaming) sendMessage();
    }
  });

  updateSendButton();
}

/* ── 启动 ─────────────────────────────────────────────────────────────── */

function boot() {
  initTabs();
  initTodoElements();
  initModelSelect();      // ⭐ S3：模型下拉框
  initFilesAndBudget();   // ⭐ A：文件选择 + token 预算
  initResults();          // ⭐ R：测试结果浏览
  initChat();

  refreshStatus();
  setInterval(refreshStatus, STATUS_INTERVAL_MS);

  const rf = $('btn-refresh');
  if (rf) rf.addEventListener('click', () => { refreshStatus(); toast('已刷新状态'); });

  console.log('[ANTLab] 前端已加载（S3 模型切换 + A 文件输入 + R 结果浏览）');
}

document.addEventListener('DOMContentLoaded', boot);
