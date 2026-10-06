"""MoviePilot 原生状态卡片与手动推送表单；生成页面时不访问网络。"""
from html import escape


STATUS_LABELS = {
    "success": "成功", "failed": "失败", "partial": "部分成功",
    "running": "运行中", "skipped": "已跳过", "pending": "等待提交",
    "duplicated": "已跳过重复",
}

# PageRender 的 events 仅支持固定参数，表单通过宿主的认证 API 读取用户输入。
# 无需读取或拼接 token；服务端返回的标题及错误始终使用 textContent 显示。
MANUAL_HANDLER = r"""
event.preventDefault();
(async function(form, submit) {
  if (form.dataset.busy === '1') return;
  const api = window.MoviePilotAPI;
  const output = form.querySelector('[data-output]');
  const button = form.querySelector('[data-submit]');
  const refresh = form.querySelector('[data-refresh]');
  const input = form.querySelector('textarea');
  const force = form.querySelector('input[type=checkbox]');
  if (!api) { output.textContent = '页面 API 未就绪，请重新打开插件页面'; return; }
  const labels = {pending:'等待提交',success:'已提交',failed:'失败',duplicated:'已跳过重复'};
  function render(job) {
    output.replaceChildren();
    const summary = document.createElement('p');
    summary.textContent = (job.status === 'running' ? '正在推送' : '推送结果') +
      '：成功 ' + (job.submitted || 0) + ' · 失败 ' + (job.failed || 0) +
      ' · 重复 ' + (job.duplicated || 0);
    output.append(summary);
    if (job.target_folder) {
      const folder = document.createElement('p');
      folder.textContent = '本批保存目录：' + job.target_folder;
      output.append(folder);
    }
    if (job.error) { const err = document.createElement('p'); err.textContent = job.error; output.append(err); }
    const list = document.createElement('ol');
    for (const item of job.results || []) {
      const row = document.createElement('li');
      row.textContent = (labels[item.status] || item.status) + ' · ' + item.title + ' — ' + item.message;
      list.append(row);
    }
    output.append(list);
    button.disabled = job.status === 'running';
    input.disabled = job.status === 'running';
    force.disabled = job.status === 'running';
  }
  form.dataset.busy = '1'; refresh.disabled = true; button.disabled = true;
  let jobId = '';
  try {
    let response;
    if (submit) {
      response = await api.post('plugin/CloudAutoSearch/manual_submit', {links:input.value, force:force.checked});
    } else {
      response = await api.get('plugin/CloudAutoSearch/manual_status');
    }
    if (!response.success) throw new Error(response.message || '提交失败');
    let job = response.data || {};
    jobId = job.id;
    render(job);
    while (job.status === 'running' && form.isConnected) {
      await new Promise(resolve => setTimeout(resolve, 1500));
      if (!form.isConnected) return;
      response = await api.get('plugin/CloudAutoSearch/manual_status');
      if (!response.success) throw new Error(response.message || '读取进度失败');
      job = response.data || {};
      if (job.id !== jobId) throw new Error('已有新的推送记录，请刷新状态查看');
      render(job);
    }
  } catch (error) {
    const warning = document.createElement('p');
    warning.textContent = (error.response && error.response.data && error.response.data.message) ||
      error.message || '网络异常，请点击刷新进度确认结果';
    output.append(warning);
  } finally {
    form.dataset.busy = '0'; refresh.disabled = false;
    button.disabled = false; input.disabled = false; force.disabled = false;
  }
})(FORM, SUBMIT);
"""


def manual_form(job: dict) -> str:
    handler = escape(MANUAL_HANDLER.replace("FORM, SUBMIT", "this, true"), quote=True)
    refresh_handler = escape(
        MANUAL_HANDLER.replace("FORM, SUBMIT", "this.closest('form'), false"), quote=True
    )
    results = "".join(
        "<li>{} · {} — {}</li>".format(
            escape(STATUS_LABELS.get(item.get("status"), item.get("status", ""))),
            escape(str(item.get("title", ""))), escape(str(item.get("message", ""))),
        ) for item in job.get("results", [])
    )
    summary = (
        f"成功 {job.get('submitted', 0)} · 失败 {job.get('failed', 0)} · 重复 {job.get('duplicated', 0)}"
        if job else "提交后会在这里显示每条链接的结果"
    )
    error = escape(str(job.get("error") or ""))
    job_folder = escape(str(job.get("target_folder") or ""))
    return f"""
<style>
.cas-manual textarea {{ width:100%; padding:14px; border:1px solid rgba(var(--v-theme-on-surface),.25);
  border-radius:10px; background:transparent; color:inherit; font:inherit; resize:vertical; min-height:130px; }}
.cas-manual textarea:focus-visible,.cas-manual button:focus-visible {{ outline:2px solid rgb(var(--v-theme-primary)); outline-offset:3px; }}
.cas-manual .cas-hint {{ opacity:.72; font-size:.875rem; margin:8px 0 16px; }}
.cas-manual .cas-actions {{ display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin:16px 0; }}
.cas-manual label {{ display:block; cursor:pointer; }}
.cas-manual label input {{ margin-right:8px; accent-color:rgb(var(--v-theme-primary)); }}
.cas-manual button {{ padding:10px 18px; border-radius:8px; font:inherit; font-weight:600; cursor:pointer;
  color:rgb(var(--v-theme-primary)); background:rgba(var(--v-theme-primary),.10); }}
.cas-manual button[data-submit] {{ color:rgb(var(--v-theme-on-primary)); background:rgb(var(--v-theme-primary)); }}
.cas-manual button:disabled {{ opacity:.5; cursor:wait; }}
.cas-manual [data-output] {{ padding:14px 16px; background:rgba(var(--v-theme-on-surface),.04); border-radius:10px;
  overflow-wrap:anywhere; font-size:.875rem; }}
.cas-manual [data-output] p {{ margin:0 0 8px; }}
.cas-manual [data-output] ol {{ padding-left:22px; }}
.cas-manual [data-output] li {{ margin:8px 0; }}
</style>
<form class="cas-manual" onsubmit="{handler}">
  <label for="cas-manual-links">磁力链接或种子下载链接</label>
  <textarea id="cas-manual-links" name="links" rows="4" required maxlength="65536"
    placeholder="magnet:?xt=urn:btih:…&#10;https://example.com/download.php?id=…" spellcheck="false"></textarea>
  <p class="cas-hint">每行一个，单批最多 20 条。种子链接须能直接下载；手动推送不受 RSS 关键词和大小过滤影响。</p>
  <label><input type="checkbox" name="force">重新提交已有成功记录的资源</label>
  <div class="cas-actions">
    <button type="submit" data-submit>推送到 115</button>
    <button type="button" data-refresh onclick="{refresh_handler}">刷新进度</button>
  </div>
  <div data-output role="status" aria-live="polite" aria-atomic="true">
    <p>{escape(summary)}</p><p>{job_folder}</p><p>{error}</p><ol>{results}</ol>
  </div>
</form>"""


def _text(text):
    return {"component": "span", "text": str(text)}


def _card(title, content):
    return {"component": "VCard", "props": {"variant": "outlined", "class": "mb-4"},
            "content": [
                {"component": "VCardTitle", "props": {"class": "text-h6 py-3"}, "content": [_text(title)]},
                {"component": "VDivider"},
                {"component": "VCardText", "content": content},
            ]}


def render_dashboard(state: dict, run_log: list) -> list:
    """保留 MoviePilot 主题及移动端布局；所有运行状态使用中文。"""
    metrics = [
        ("115 登录", "已保存登录" if state["logged_in"] else "未登录",
         state.get("username") or ("提交前自动校验会话" if state["logged_in"] else "请先在配置页登录")),
        ("RSS 自动下载", "已启用" if state["enabled"] else "已暂停", f"{state['rss_count']} 个订阅"),
        ("成功推送记录", state["total_submitted"], "按资源去重计数"),
        ("当前任务", "运行中" if state["running"] else "空闲", "手动推送与 RSS 共用队列"),
    ]
    columns = []
    for label, value, hint in metrics:
        columns.append({"component": "VCol", "props": {"cols": 12, "sm": 6, "lg": 3},
                        "content": [_card(label, [
                            {"component": "div", "props": {"class": "text-h5 font-weight-bold mb-2"}, "text": str(value)},
                            {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": hint},
                        ])]})
    context = [
        {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "mb-4",
         "text": f"保存目录：{state['target_folder']}  ·  RSS 周期：{state['cron']}"}},
        {"component": "VBtn", "props": {"variant": "tonal", "color": "primary", "class": "mb-4 mr-3",
         "prepend-icon": "mdi-refresh"}, "text": "刷新状态",
         "events": {"click": {"api": "plugin/CloudAutoSearch/status", "method": "GET"}}},
        {"component": "VBtn", "props": {"variant": "tonal", "color": "primary", "class": "mb-4",
         "prepend-icon": "mdi-rss", "disabled": state["running"]}, "text": "立即运行 RSS",
         "events": {"click": {"api": "plugin/CloudAutoSearch/run", "method": "POST"}}},
    ]
    if not state["logged_in"]:
        context.insert(0, {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "class": "mb-4",
                           "text": "尚未保存 115 登录凭据，请先打开插件配置完成登录。"}})
    job = state.get("manual_job") or {}
    if job.get("status") == "running":
        context.append({"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "mb-4",
                        "text": "手动推送正在后台运行，点击“刷新进度”可继续查看。"}})
    page = [{"component": "VRow", "content": columns}] + context
    page.append(_card("手动推送", [{"component": "div", "html": manual_form(job)}]))
    rows = []
    for run in run_log[:10]:
        source = "手动推送" if run.get("source") == "manual_links" else (
            "手动运行 RSS" if run.get("manual") else "定时 RSS"
        )
        status = STATUS_LABELS.get(run.get("status"), run.get("status", ""))
        # 老版 RSS 记录 status=success 但 failed>0 时，也明确展示部分失败。
        if run.get("failed") and status == "成功":
            status = "部分失败"
        cells = [run.get("start_time", ""), source, status,
                 f"成功 {run.get('submitted', 0)} / 失败 {run.get('failed', 0)} / 重复 {run.get('duplicated', 0)}",
                 run.get("error") or "—"]
        rows.append({"component": "tr", "content": [
            {"component": "td", "props": {"class": "text-caption"}, "content": [_text(cell)]} for cell in cells
        ]})
    history = [{"component": "VTable", "props": {"density": "compact"}, "content": [
        {"component": "thead", "content": [{"component": "tr", "content": [
            {"component": "th", "content": [_text(label)]} for label in ["时间", "来源", "状态", "统计", "说明"]
        ]}]}, {"component": "tbody", "content": rows},
    ]}] if rows else [{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "暂无运行记录，可先手动推送链接。"}}]
    page.append(_card("最近运行记录", history))
    return page
