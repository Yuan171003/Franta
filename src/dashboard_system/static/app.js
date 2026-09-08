import katex from './vendor/katex.js';

const $ = (id) => document.getElementById(id);
const page = location.pathname === '/main-memory' ? 'main' : location.pathname === '/explorer-memory' ? 'explorer' : 'overview';
const state = { csrf: null, overview: null, overviewAt: 0, advisorKey: null, advisorSubmitted: null, choices: [], monitorBusy: false, monitorSubmitting: false, monitorEpoch: 0,
  main: { kind: 'all', q: '', offset: 0, limit: 20, serial: 0, selected: null, detailSerial: 0, mapSerial: 0, graphKey: null, graphModel: null },
  explorer: { q: '', serial: 0, scratch: { offset: 0, limit: 15 }, summary: { offset: 0, limit: 15 } }, openJournal: new Set() };
const statusNames = { running: 'Running', pending: 'Pending delivery', ready: 'Ready', completed: 'Completed', delivered: 'Delivered', consumed: 'Delivered', queued: 'Queued', claimed: 'In progress', accepted: 'Accepted', assigned: 'Assigned', rejected: 'Rejected', disabled: 'Disabled', unknown: 'Unknown', failed: 'Failed', error: 'Error', stopped: 'Stopped', paused: 'Paused', idle: 'Idle', waiting: 'Waiting', waiting_for_human: 'Waiting for your feedback', waiting_for_feedback: 'Waiting for your feedback', awaiting_human: 'Waiting for your feedback', active: 'Active', done: 'Done', closed: 'Closed', verified: 'Verified', refuted: 'Refuted', blocked: 'Blocked', scheduled: 'Scheduled', planned: 'Planned', submitted: 'Submitted', proposed: 'Awaiting verification', launching: 'Launching', postprocessing: 'Post-processing', retry_pending: 'Retry pending' };
const label = (value) => statusNames[value] || String(value || 'Not reported');
const number = (value) => Number.isFinite(Number(value)) && value !== null && value !== undefined ? new Intl.NumberFormat('en-US').format(Number(value)) : '—';
const shortNumber = (value) => value === null || value === undefined ? '—' : Number(value) >= 1000000 ? `${(Number(value) / 1000000).toFixed(2)}M` : number(value);
const text = (value) => value == null ? '' : typeof value === 'string' ? value : JSON.stringify(value, null, 2);
const date = (value) => { if (!value) return 'Not reported'; const d = new Date(value); return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString('en-US', { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' }); };
function duration(value) { if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—'; const seconds = Math.max(0, Math.floor(Number(value))); const hours = Math.floor(seconds / 3600); return `${hours}h ${String(Math.floor(seconds % 3600 / 60)).padStart(2, '0')}m`; }
function node(tag, className, ...children) { const element = document.createElement(tag); if (className) element.className = className; for (const child of children.flat()) if (child != null) element.append(child instanceof Node ? child : document.createTextNode(String(child))); return element; }
function button(title, className, handler) { const result = node('button', className, title); result.type = 'button'; if (handler) result.addEventListener('click', handler); return result; }
function badge(value) { return node('span', `badge ${['error', 'failed', 'refuted'].includes(value) ? 'error' : /waiting|paused|blocked/.test(value || '') ? 'warn' : /completed|stopped|idle|closed/.test(value || '') ? 'muted' : ''}`, label(value)); }
function errorAt(id, error) { const target = $(id); target.textContent = error ? error.message || String(error) : ''; target.hidden = !error; }
function feedback(id, message, error = false) { $(id).textContent = message; $(id).classList.toggle('error', error); }

// Research records are untrusted text. Markdown is built as DOM nodes; raw HTML
// is never inserted. Only the local KaTeX renderer creates mathematical markup.
function math(source, display = false) {
  const element = node(display ? 'div' : 'span', display ? 'math-block' : 'math-inline');
  try { katex.render(source, element, { displayMode: display, throwOnError: false, trust: false, strict: 'ignore', maxExpand: 1000, maxSize: 20 }); }
  catch { element.classList.add('math-fallback'); element.textContent = source; }
  return element;
}
function safeLink(href) { try { const url = new URL(href, location.origin); return ['https:', 'http:', 'mailto:'].includes(url.protocol) ? url.href : null; } catch { return null; } }
function inline(source, parent, depth = 0) {
  if (depth > 8) { parent.append(document.createTextNode(source)); return; }
  const pattern = /(`+)([\s\S]*?)\1|\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<![\\$])\$(?!\s)([^$\n]+?)(?<!\s)\$(?!\d)|\[([^\]\n]+)\]\(([^\s)]+)(?:\s+"[^"\n]*")?\)|\*\*([\s\S]+?)\*\*|__([\s\S]+?)__|(?<!\*)\*([^*\n]+)\*(?!\*)|(?<![\w_])_([^_\n]+)_(?!\w)|~~([^~\n]+)~~|\\([\\`*{}\[\]()#+.!_$>\-])/g;
  let last = 0;
  for (const match of source.matchAll(pattern)) {
    parent.append(document.createTextNode(source.slice(last, match.index)));
    if (match[1]) parent.append(node('code', '', match[2]));
    else if (match[3] || match[4]) parent.append(math(match[3] || match[4], true));
    else if (match[5] || match[6]) parent.append(math(match[5] || match[6]));
    else if (match[7]) { const url = safeLink(match[8]); const link = node(url ? 'a' : 'span'); if (url) { link.href = url; if (new URL(url).origin !== location.origin) { link.target = '_blank'; link.rel = 'noopener noreferrer'; } } inline(match[7], link, depth + 1); parent.append(link); }
    else if (match[9] || match[10]) { const strong = node('strong'); inline(match[9] || match[10], strong, depth + 1); parent.append(strong); }
    else if (match[11] || match[12]) { const em = node('em'); inline(match[11] || match[12], em, depth + 1); parent.append(em); }
    else if (match[13]) { const strike = node('del'); inline(match[13], strike, depth + 1); parent.append(strike); }
    else parent.append(document.createTextNode(match[14]));
    last = match.index + match[0].length;
  }
  parent.append(document.createTextNode(source.slice(last)));
}
function markdown(value) {
  const root = node('div', 'prose'); const lines = text(value).replace(/\r\n?/g, '\n').split('\n'); let i = 0;
  const startsBlock = (line) => /^\s*$|^ {0,3}(?:#{1,6}\s|```|~~~|>|[-*+]\s|\d+[.)]\s|\$\$|\\\[|(?:---+|___+|\*\*\*+)\s*$)/.test(line);
  while (i < lines.length) {
    const line = lines[i]; if (!line.trim()) { i++; continue; }
    const fence = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (fence) { const body = []; i++; while (i < lines.length && !new RegExp(`^ {0,3}${fence[1][0]}{${fence[1].length},}\\s*$`).test(lines[i])) body.push(lines[i++]); if (i < lines.length) i++; root.append(node('pre', '', node('code', '', body.join('\n')))); continue; }
    const mathStart = line.match(/^\s*(\$\$|\\\[)(.*)$/);
    if (mathStart) { const close = mathStart[1] === '$$' ? '$$' : '\\]'; let body = mathStart[2]; i++; let end = body.indexOf(close); while (end < 0 && i < lines.length) { body += `\n${lines[i++]}`; end = body.indexOf(close); } if (end >= 0) { root.append(math(body.slice(0, end).trim(), true)); const rest = body.slice(end + close.length).trim(); if (rest) { const p = node('p'); inline(rest, p); root.append(p); } } else { const p = node('p'); p.textContent = `${mathStart[1]}${body}`; root.append(p); } continue; }
    const heading = line.match(/^ {0,3}(#{1,6})\s+(.+?)\s*#*$/);
    if (heading) { const h = node(`h${heading[1].length}`); inline(heading[2], h); root.append(h); i++; continue; }
    if (/^ {0,3}(?:---+|___+|\*\*\*+)\s*$/.test(line)) { root.append(node('hr')); i++; continue; }
    if (/^ {0,3}>/.test(line)) { const body = []; while (i < lines.length && /^ {0,3}>/.test(lines[i])) body.push(lines[i++].replace(/^ {0,3}> ?/, '')); root.append(node('blockquote', '', markdown(body.join('\n')))); continue; }
    if (i + 1 < lines.length && line.includes('|') && /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[i + 1])) {
      const split = (row) => row.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map((cell) => cell.trim());
      const table = node('table'); const tr = node('tr'); for (const cell of split(line)) { const th = node('th'); inline(cell, th); tr.append(th); } table.append(node('thead', '', tr)); i += 2; const tbody = node('tbody');
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) { const row = node('tr'); for (const cell of split(lines[i++])) { const td = node('td'); inline(cell, td); row.append(td); } tbody.append(row); } table.append(tbody); root.append(node('div', 'table-scroll', table)); continue;
    }
    const list = line.match(/^( {0,3})([-*+]|\d+[.)])\s+(.*)$/);
    if (list) { const ordered = /^\d/.test(list[2]); const listNode = node(ordered ? 'ol' : 'ul'); if (ordered) listNode.start = parseInt(list[2], 10); while (i < lines.length) { const item = lines[i].match(/^( {0,3})([-*+]|\d+[.)])\s+(.*)$/); if (!item || /^\d/.test(item[2]) !== ordered || item[1].length !== list[1].length) break; const body = [item[3]]; const indent = item[1].length + item[2].length + 1; i++; while (i < lines.length) { if (!lines[i].trim() && i + 1 < lines.length && /^\s+\S/.test(lines[i + 1])) { body.push(''); i++; continue; } if (lines[i].startsWith(' '.repeat(indent))) { body.push(lines[i++].slice(indent)); continue; } break; } listNode.append(node('li', '', markdown(body.join('\n')))); } root.append(listNode); continue; }
    const body = [line]; i++; while (i < lines.length && !startsBlock(lines[i]) && !(i + 1 < lines.length && lines[i].includes('|') && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1]))) body.push(lines[i++]); const p = node('p'); inline(body.join('\n'), p); root.append(p);
  }
  return root;
}

async function api(path, body) {
  const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    if (body !== undefined && !state.csrf) { const session = await api('/api/session'); state.csrf = session.csrf_token; }
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store', signal: controller.signal, headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json', 'X-Dashboard-Token': state.csrf }, body: body === undefined ? undefined : JSON.stringify(body) });
    let value; try { value = await response.json(); } catch { throw new Error(`The service returned an unreadable response (HTTP ${response.status}).`); }
    if (!response.ok) { if (response.status === 403) state.csrf = null; throw new Error(typeof value.error === 'string' ? value.error : value.error?.message || value.message || `Request failed (HTTP ${response.status})`); }
    return value;
  } catch (error) { if (error.name === 'AbortError') throw new Error('The request timed out. Please try again.'); throw error; } finally { clearTimeout(timeout); }
}
function setConnection(ok) { const target = document.querySelector('.connection'); target.classList.toggle('online', ok); target.classList.toggle('offline', !ok); $('connection-label').textContent = ok ? 'Local service connected' : 'Connection lost · retrying'; }
function sourceLink(id) { const link = node('a', 'source-link', id); link.href = /^E(?:S|X)|scratch|summary/i.test(id) ? `/explorer-memory?q=${encodeURIComponent(id)}` : `/main-memory?id=${encodeURIComponent(id)}`; return link; }
function updateClock() { const run = state.overview?.run; if (!run) return; const extra = run.status === 'running' ? Math.max(0, (Date.now() - state.overviewAt) / 1000) : 0; $('elapsed').textContent = duration(run.elapsed_seconds == null ? null : Number(run.elapsed_seconds) + extra); $('active-time').textContent = run.active_seconds == null ? 'Active time has not been reported' : `Active for ${duration(Number(run.active_seconds) + extra)}`; }
const workerRoles = [
  { kind: 'main', title: 'Main agent', description: 'Coordinates the research cycle' },
  { kind: 'worker', title: 'Franta workers', description: 'Execute assigned tasks and modes' },
  { kind: 'explorer-worker', title: 'Explorer workers', description: 'Test independent directions' },
  { kind: 'main-sort', title: 'Main sort', description: 'Reviews Explorer findings' },
];
function renderWorkers(items) {
  const workers = Array.isArray(items) ? items : []; const key = JSON.stringify(workers); const roster = $('worker-roster');
  $('worker-count').textContent = number(workers.length); if (roster.dataset.key === key) return; roster.dataset.key = key;
  roster.replaceChildren(...workerRoles.map((role) => {
    const active = workers.filter((item) => item.kind === role.kind);
    const lane = node('section', `worker-lane${active.length ? ' has-active' : ''}`, node('div', 'worker-lane-heading', node('div', '', node('h3', '', role.title), node('p', '', role.description)), node('span', 'worker-lane-count', number(active.length))));
    if (!active.length) lane.append(node('div', 'worker-idle', node('span', 'worker-idle-dot'), node('span', '', 'Idle')));
    for (const item of active) lane.append(node('article', 'worker-card', node('div', 'worker-card-top', node('span', 'live-dot'), node('strong', '', item.mode || role.title), badge(item.status || 'running')), node('div', 'worker-card-id', item.worker_id || item.call_id), node('p', '', item.objective || 'Work objective not reported'), node('div', 'worker-card-meta', [item.call_id, item.attempt ? `Attempt ${item.attempt}` : ''].filter(Boolean).join(' · '))));
    return lane;
  }));
}
function renderOverview(data) {
  state.overview = data; state.overviewAt = Date.now(); const run = data.run || {}; const usage = data.usage || {};
  $('project-name').textContent = data.project?.name || 'Research overview'; document.title = `${data.project?.name || 'Franta'} · Research Observatory`;
  const problem = text(data.project?.root_problem); if ($('root-problem').dataset.value !== problem) { $('root-problem').replaceChildren(markdown(problem || 'The research problem has not been reported.')); $('root-problem').dataset.value = problem; }
  $('run-status').replaceWith(Object.assign(badge(run.status), { id: 'run-status' })); $('run-phase').textContent = [run.phase, run.cycle == null ? '' : `Cycle ${run.cycle}`].filter(Boolean).join(' · ');
  updateClock(); $('total-tokens').textContent = shortNumber(usage.total_tokens); $('token-breakdown').textContent = [usage.input_tokens, usage.cached_input_tokens, usage.output_tokens].map(shortNumber).join(' / ');
  $('token-calls').textContent = `${number(usage.reported_calls ?? 0)} calls reported${usage.unreported_calls ? ` · ${number(usage.unreported_calls)} unreported` : ''}`;
  $('monitor-tokens').textContent = usage.monitor ? `Separate Monitor usage: ${shortNumber(usage.monitor.total_tokens)} tokens` : 'Separate Monitor usage · not reported';
  $('monitor-tokens').title = usage.monitor ? `Input ${number(usage.monitor.input_tokens)} / Cached input ${number(usage.monitor.cached_input_tokens)} / Output ${number(usage.monitor.output_tokens)}` : '';
  const directions = Array.isArray(data.directions) ? data.directions : []; $('active-count').textContent = number(directions.length); $('direction-count').textContent = number(directions.length); $('cycle-label').textContent = run.cycle == null ? 'Current research cycle' : `Research cycle ${run.cycle}`;
  const directionsKey = JSON.stringify(directions); if ($('directions').dataset.key !== directionsKey) { $('directions').dataset.key = directionsKey; $('directions').replaceChildren(...directions.map((item) => { const system = node('div', 'system', item.system || 'Worker', node('small', '', item.task_id || item.id || '')); const objective = node('div', 'direction-objective', markdown(item.objective || 'Research direction not reported'), node('div', 'direction-meta', [item.session_id, item.attempt ? `Attempt ${item.attempt}` : '', item.mode, item.updated_at ? `Updated ${date(item.updated_at)}` : ''].filter(Boolean).join(' · '))); return node('div', 'direction-row', system, objective, badge(item.status || 'running')); })); if (!directions.length) $('directions').append(node('p', 'empty', 'No research directions are active right now.')); }
  renderWorkers(data.workers || []);
  renderGuidance(data.guidance || []); renderAdvisor(data.advisor || {}); renderAdvisorReceipts(data.advisor?.feedback_receipts || data.feedback_receipts || []);
  $('last-sync').textContent = `Data updated ${date(data.as_of || new Date().toISOString())}`;
}
function renderGuidance(items) {
  const key = JSON.stringify(items); if ($('guidance-list').dataset.key === key) return; $('guidance-list').dataset.key = key;
  const opened = new Set([...$('guidance-list').querySelectorAll('details[open]')].map((el) => el.dataset.id));
  $('guidance-list').replaceChildren(...[...items].sort((a, b) => String(b.received_at || b.created_at || b.guidance_id || b.id || '').localeCompare(String(a.received_at || a.created_at || a.guidance_id || a.id || ''))).map((item) => { const id = item.guidance_id || item.id || ''; const content = item.text || item.guidance || item.content || item.preview || ''; const row = node('details', 'receipt'); row.dataset.id = id; row.open = opened.has(id); row.append(node('summary', '', node('div', 'receipt-top', node('span', 'receipt-id', id), badge(item.status), node('time', 'receipt-time', date(item.received_at || item.created_at))), node('div', 'receipt-preview', content || 'Saved guidance'))); row.append(markdown(content)); const delivery = [item.relative_path, item.task_id, item.call_id, item.worker_session_id].filter(Boolean).join(' · '); if (delivery) row.append(node('p', 'quiet', delivery)); return row; }));
  if (!items.length) $('guidance-list').append(node('p', 'empty', 'No guidance submitted.'));
}
function renderAdvisor(advisor) {
  const report = advisor.report; const requestId = advisor.status === 'waiting_for_human' ? advisor.request_id : null;
  $('advisor-status').replaceWith(Object.assign(badge(advisor.status || 'idle'), { id: 'advisor-status' }));
  const key = `${requestId || ''}:${JSON.stringify(report)}`; if (key === state.advisorKey) return;
  state.advisorKey = key; state.choices = [];
  for (const input of $('advisor-form').querySelectorAll('input,textarea,button')) input.disabled = false;
  feedback('advisor-feedback', '');
  $('advisor-choices').replaceChildren();
  $('advisor-form').hidden = !requestId || !Array.isArray(report?.obligations) || state.advisorSubmitted === requestId;
  if (report && typeof report === 'string') { $('advisor-report').replaceChildren(markdown(report)); return; }
  if (!report?.obligations?.length) { $('advisor-report').replaceChildren(node('p', 'empty', 'When the Advisor submits a report, choose the next-cycle obligations here.')); return; }
  const intro = node('div', 'advisor-report-text'); if (report.human_question) intro.append(markdown(report.human_question)); if (report.target_cycle != null) intro.append(node('p', 'quiet', `Next: Cycle ${report.target_cycle} · ${requestId || report.selection_report_id || ''}`)); $('advisor-report').replaceChildren(intro);
  $('advisor-choices').replaceChildren(...report.obligations.map((item) => { const checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.value = item.obligation_id; checkbox.disabled = !requestId || state.advisorSubmitted === requestId; checkbox.addEventListener('change', () => { if (checkbox.checked) state.choices.push(item.obligation_id); else state.choices = state.choices.filter((id) => id !== item.obligation_id); updateChoiceOrder(); }); const row = node('div', 'advisor-candidate'); row.append(node('label', 'candidate-title', checkbox, node('span', 'rank', String(item.rank).padStart(2, '0')), node('span', '', item.title || item.obligation_id))); const details = node('details'); details.append(node('summary', '', 'View statement and Advisor rationale')); const content = node('div', 'prose'); for (const [field, title] of [['statement', 'Task statement'], ['importance', 'Importance'], ['landscape_change', 'Expected progress'], ['relationship_to_root', 'Relationship to the root problem'], ['novelty', 'Novelty'], ['repeat_justification', 'Reason to revisit']]) if (item[field]) content.append(node('div', 'advisor-field', node('strong', '', title), markdown(item[field]))); details.append(content); row.append(details); return row; }));
  $('custom-obligation').value = ''; $('custom-obligation-2').value = ''; $('advisor-instructions').value = ''; updateChoiceOrder();
}
function renderAdvisorReceipts(receipts) {
  const key = JSON.stringify(receipts); if ($('advisor-receipts').dataset.key === key) return; $('advisor-receipts').dataset.key = key;
  $('advisor-receipts').replaceChildren(...receipts.slice(0, 5).map((receipt) => node('div', `notice${receipt.status === 'rejected' ? ' error' : ''}`, node('div', 'receipt-top', badge(receipt.status), node('span', 'receipt-id', receipt.request_id || receipt.feedback_request_id || receipt.id || ''), node('span', 'receipt-time', date(receipt.processed_at || receipt.created_at))), receipt.error ? node('div', '', text(receipt.error)) : node('div', 'quiet', receipt.status === 'accepted' ? 'The research runtime accepted the next-cycle tasks.' : 'Waiting for the research runtime to process this submission.'))));
}
function updateChoiceOrder() { $('selected-order').textContent = state.choices.length ? `Candidate order: ${state.choices.join(' → ')}. Custom tasks follow.` : ''; }

let overviewPending = false;
async function loadOverview() { if (overviewPending) return; overviewPending = true; try { const data = await api('/api/overview'); if (page === 'overview') renderOverview(data); else { state.overview = data; $('last-sync').textContent = `Data updated ${date(data.as_of)}`; } setConnection(true); errorAt('global-error', null); } catch (error) { setConnection(false); errorAt('global-error', new Error(`Unable to read research status: ${error.message} The page will retry automatically; unsent text is preserved.`)); } finally { overviewPending = false; } }
let monitorPending = false;
const qualityDimensions = [
  ['creativity', 'Divergent thinking and creativity'],
  ['synthesis', 'Combining approaches into progress'],
  ['critical_obstacles', 'Attacking critical obstacles'],
  ['breakthrough_potential', 'Major breakthrough potential'],
  ['proof_closure_potential', 'Proof closure potential'],
];
function renderMonitorQuality(data) {
  const currentCycle = state.overview?.run?.cycle;
  const previousCycle = data.source_cycle != null && currentCycle != null && data.source_cycle !== currentCycle;
  $('quality-meta').textContent = [
    data.source_cycle == null ? '' : `Assessment of Cycle ${data.source_cycle}`,
    previousCycle ? `Current research is in Cycle ${currentCycle}; this assessment is from an earlier cycle` : '',
    data.generated_at ? `Updated ${date(data.generated_at)}` : 'Updated with the research outlook',
    'Evidence-based assessments, not probabilities',
  ].filter(Boolean).join(' · ');
  const quality = data.result?.quality;
  const key = JSON.stringify([quality, data.status]);
  if ($('monitor-quality').dataset.key === key) return;
  $('monitor-quality').dataset.key = key;
  if (!quality) {
    $('monitor-quality').replaceChildren(node('p', 'empty', data.status === 'running'
      ? 'The Monitor is assessing research quality. The assessment will appear here.'
      : 'No quality assessment yet. Refresh the summary to assess current progress.'));
    return;
  }
  $('monitor-quality').replaceChildren(...qualityDimensions.map(([id, title]) => {
    const item = quality[id];
    const score = Number.isInteger(item?.score) && item.score >= 1 && item.score <= 10 ? item.score : null;
    const meter = document.createElement('meter');
    meter.min = 1; meter.max = 10; meter.value = score ?? 1;
    meter.setAttribute('aria-label', `${title}: ${score ?? 'Not reported'} out of 10`);
    meter.hidden = score === null;
    return node('article', 'quality-card',
      node('div', 'quality-card-heading', node('h3', '', title),
        node('strong', 'quality-score', score == null ? '—' : String(score), node('small', '', ' / 10'))),
      meter, markdown(item?.reason || 'No assessment reported.'),
      node('div', 'source-links', ...(item?.source_ids || []).map(sourceLink)));
  }));
}
function monitorButton() { $('refresh-monitor').disabled = state.monitorBusy || state.monitorSubmitting; $('refresh-monitor').textContent = state.monitorSubmitting ? 'Starting summary…' : state.monitorBusy ? 'Generating summary…' : '↻ Refresh summary'; }
async function loadMonitor() {
  if (monitorPending || state.monitorSubmitting) return; monitorPending = true; const epoch = state.monitorEpoch;
  try { const data = await api('/api/monitor'); if (epoch !== state.monitorEpoch) return; state.monitorBusy = data.status === 'running'; monitorButton();
    $('monitor-meta').textContent = ['Read-only Monitor', data.generated_at ? `Last summary ${date(data.generated_at)}` : 'No summary generated', data.next_refresh_at ? `Next update ${date(data.next_refresh_at)}` : 'Automatically refreshes every 90 minutes', data.source_as_of ? `Memory snapshot ${date(data.source_as_of)}` : ''].filter(Boolean).join(' · ');
    errorAt('monitor-error', data.error ? new Error(`The summary was not updated: ${text(data.error)}${data.result ? ' The previous result is still shown.' : ''}`) : null);
    renderMonitorQuality(data);
    const directions = data.result?.directions; const key = JSON.stringify(directions); if ($('monitor-results').dataset.key === key) return; $('monitor-results').dataset.key = key;
    if (!Array.isArray(directions) || !directions.length) { $('monitor-results').replaceChildren(node('p', 'empty', data.status === 'running' ? 'The Monitor is reading research memory. Its summary will appear here.' : 'No research outlook is available. Refresh to summarize the current memory.')); return; }
    $('monitor-results').replaceChildren(...directions.slice(0, 5).map((item, index) => { const content = node('div', '', node('h3', '', item.title || `Direction ${index + 1}`), markdown(item.summary || '')); const dl = node('dl'); for (const [key, title] of [['why_promising', 'Why promising'], ['obstacles', 'Main obstacles'], ['next_step', 'Next step']]) if (item[key]) dl.append(node('dt', '', title), node('dd', '', markdown(Array.isArray(item[key]) ? item[key].join('\n') : item[key]))); content.append(dl); if (item.source_ids?.length) content.append(node('div', 'source-links', ...item.source_ids.map(sourceLink))); return node('article', 'outlook-item', node('span', 'outlook-number', String(index + 1).padStart(2, '0')), content); }));
  } catch (error) { if (epoch === state.monitorEpoch) errorAt('monitor-error', error); } finally { monitorPending = false; }
}
function pagination(target, data, callback) { const previous = button('← Previous', 'button', () => callback(Math.max(0, data.offset - data.limit))); previous.disabled = data.offset <= 0; const next = button('Next →', 'button', () => callback(data.offset + data.limit)); next.disabled = data.offset + data.items.length >= data.total; const range = data.total ? `${number(data.offset + 1)}–${number(Math.min(data.offset + data.items.length, data.total))} / ${number(data.total)}` : '0 records'; $(target).replaceChildren(previous, node('span', '', range), next); }
async function loadMain() {
  const serial = ++state.main.serial; const params = new URLSearchParams({ kind: state.main.kind, q: state.main.q, offset: state.main.offset, limit: state.main.limit });
  try { const data = await api(`/api/main-memory?${params}`); if (serial !== state.main.serial) return; errorAt('main-error', null); $('main-total').textContent = `${number(data.total)} records`; const key = JSON.stringify(data.items); if ($('main-records').dataset.key !== key) { $('main-records').dataset.key = key; $('main-records').replaceChildren(...data.items.map((item) => { const row = button('', `record-button${state.main.selected === item.id ? ' selected' : ''}`, () => loadDetail(item.id, true)); row.dataset.id = item.id; row.append(node('div', 'record-top', node('span', `type-label ${item.type}`, item.type), node('span', 'record-id', item.id)), node('div', 'record-abstract', item.abstract || item.content || 'No abstract')); return row; })); if (!data.items.length) $('main-records').append(node('p', 'empty', state.main.q ? 'No records match this search.' : 'No research records of this type.')); } pagination('main-pagination', data, (offset) => { state.main.offset = offset; loadMain(); }); }
  catch (error) { if (serial === state.main.serial) errorAt('main-error', error); }
}
function svgNode(tag, attrs, content) { const el = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value); if (content != null) el.textContent = content; return el; }
const memoryTypeShades = {
  route: { hue: 160, saturation: [34, 58], lightness: [78, 31] },
  fact: { hue: 207, saturation: [34, 61], lightness: [81, 36] },
  obligation: { hue: 36, saturation: [31, 52], lightness: [80, 36] },
  claim: { hue: 282, saturation: [24, 43], lightness: [82, 37] },
};
function memoryNodeColor(type, recency, active) {
  const shade = memoryTypeShades[type] || { hue: 150, saturation: [20, 35], lightness: [80, 38] }; const activity = active === false ? .58 : 1; const amount = Math.pow(Math.max(0, Math.min(1, recency)) * activity, .78);
  const saturation = shade.saturation[0] + (shade.saturation[1] - shade.saturation[0]) * amount; const lightness = shade.lightness[0] + (shade.lightness[1] - shade.lightness[0]) * amount; return `hsl(${shade.hue} ${saturation.toFixed(1)}% ${lightness.toFixed(1)}%)`;
}
function hashId(value) { let hash = 2166136261; for (const char of String(value)) { hash ^= char.charCodeAt(0); hash = Math.imul(hash, 16777619); } return hash >>> 0; }
function resetMemoryGraph() {
  const model = state.main.graphModel; if (!model) return; model.scale = 1; model.tx = 0; model.ty = 0; model.viewport.setAttribute('transform', 'translate(0 0) scale(1)'); for (const item of model.nodes) item.fixed = false; reheatMemoryGraph(model, .9);
}
function renderMemoryGraph(data) {
  const graphKey = JSON.stringify([(data.nodes || []).map((item) => [item.id, item.status, item.updated_at]), (data.edges || []).map((item) => [item.source, item.target, item.kind])]); if (state.main.graphKey === graphKey && state.main.graphModel) return;
  if (state.main.graphModel) state.main.graphModel.cancelled = true; state.main.graphKey = graphKey;
  const width = 1180; const height = 700; const sourceNodes = Array.isArray(data.nodes) ? data.nodes : []; const sourceEdges = Array.isArray(data.edges) ? data.edges : [];
  $('memory-map-meta').textContent = `${number(sourceNodes.length)} nodes · ${number(sourceEdges.length)} dependency edges`;
  if (!sourceNodes.length) { $('memory-map').replaceChildren(node('p', 'empty', 'No structured research memory is available.')); state.main.graphModel = null; return; }
  const degree = new Map(); for (const edge of sourceEdges) { degree.set(edge.source, (degree.get(edge.source) || 0) + 1); degree.set(edge.target, (degree.get(edge.target) || 0) + 1); }
  const times = [...new Set(sourceNodes.map((item) => Date.parse(item.updated_at || item.created_at || '')).filter(Number.isFinite))].sort((a, b) => a - b); const timeRank = new Map(times.map((value, index) => [value, times.length <= 1 ? 1 : index / (times.length - 1)]));
  const golden = Math.PI * (3 - Math.sqrt(5)); const nodes = sourceNodes.map((item, index) => { const angle = index * golden + (hashId(item.id) % 1000) / 1000; const radius = 19 * Math.sqrt(index + 1); const timestamp = Date.parse(item.updated_at || item.created_at || ''); return { ...item, recency: Number.isFinite(timestamp) ? timeRank.get(timestamp) ?? 0 : 0, x: width / 2 + Math.cos(angle) * radius, y: height / 2 + Math.sin(angle) * radius * .62, vx: 0, vy: 0, radius: Math.min(12, 5 + Math.sqrt(degree.get(item.id) || 0) * 1.15), fixed: false }; });
  const byId = new Map(nodes.map((item) => [item.id, item])); const edges = sourceEdges.map((item) => ({ ...item, sourceNode: byId.get(item.source), targetNode: byId.get(item.target) })).filter((item) => item.sourceNode && item.targetNode);
  const svg = svgNode('svg', { class: 'memory-network', viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': 'Force-directed graph of routes, facts, obligations, and claims' }); const viewport = svgNode('g', { class: 'memory-network-viewport' }); const links = svgNode('g', { class: 'memory-network-links' }); const nodeLayer = svgNode('g', { class: 'memory-network-nodes' }); viewport.append(links, nodeLayer); svg.append(viewport);
  for (const edge of edges) { edge.element = svgNode('line', { class: 'memory-network-edge', 'data-source': edge.source, 'data-target': edge.target }); edge.element.append(svgNode('title', {}, `${edge.source} — ${edge.kind} — ${edge.target}`)); links.append(edge.element); }
  const tooltip = node('div', 'memory-network-tooltip'); tooltip.hidden = true; const wrapper = node('div', 'memory-network-wrap', svg, tooltip); $('memory-map').replaceChildren(wrapper);
  const model = { cancelled: false, running: false, nodes, edges, svg, viewport, width, height, scale: 1, tx: 0, ty: 0, drag: null, pan: null, alpha: 0 }; state.main.graphModel = model;
  function showTooltip(event, item) { tooltip.replaceChildren(node('strong', '', item.id), node('span', '', `${label(item.type)} · ${number(degree.get(item.id) || 0)} links · updated ${date(item.updated_at || item.created_at)}`), node('p', '', item.abstract || 'No abstract')); tooltip.hidden = false; const bounds = wrapper.getBoundingClientRect(); tooltip.style.left = `${Math.min(bounds.width - 280, Math.max(10, event.clientX - bounds.left + 14))}px`; tooltip.style.top = `${Math.min(bounds.height - 105, Math.max(10, event.clientY - bounds.top + 14))}px`; }
  function focusNode(item, on) { const adjacent = new Set([item.id]); for (const edge of edges) if (edge.source === item.id || edge.target === item.id) { edge.element.classList.toggle('is-related', on); adjacent.add(edge.source); adjacent.add(edge.target); } for (const other of nodes) other.element.classList.toggle('is-muted', on && !adjacent.has(other.id)); }
  function graphPoint(event) { const point = svg.createSVGPoint(); point.x = event.clientX; point.y = event.clientY; return point.matrixTransform(viewport.getScreenCTM().inverse()); }
  for (const item of nodes) {
    const circle = svgNode('circle', { class: `memory-network-node ${item.type}${state.main.selected === item.id ? ' selected' : ''}`, cx: item.x, cy: item.y, r: item.radius, tabindex: '0', role: 'button', 'aria-label': `Open ${item.type} ${item.id}`, 'data-map-id': item.id }); circle.style.fill = memoryNodeColor(item.type, item.recency, item.active); item.element = circle; circle.append(svgNode('title', {}, `${item.id}\nUpdated ${date(item.updated_at || item.created_at)}\n${item.abstract || 'No abstract'}`)); nodeLayer.append(circle);
    circle.addEventListener('pointerenter', (event) => { if (!model.drag) { focusNode(item, true); showTooltip(event, item); } }); circle.addEventListener('pointermove', (event) => { if (!model.drag) showTooltip(event, item); }); circle.addEventListener('pointerleave', () => { if (!model.drag) { focusNode(item, false); tooltip.hidden = true; } });
    circle.addEventListener('pointerdown', (event) => { event.stopPropagation(); const point = graphPoint(event); model.drag = { item, pointerId: event.pointerId, startX: point.x, startY: point.y, moved: false }; item.fixed = true; circle.setPointerCapture(event.pointerId); tooltip.hidden = true; reheatMemoryGraph(model, .9); });
    circle.addEventListener('pointermove', (event) => { if (!model.drag || model.drag.pointerId !== event.pointerId || model.drag.item !== item) return; const point = graphPoint(event); if (Math.hypot(point.x - model.drag.startX, point.y - model.drag.startY) > 3) model.drag.moved = true; item.x = point.x; item.y = point.y; item.vx = 0; item.vy = 0; reheatMemoryGraph(model, .82); paintMemoryGraph(model); });
    circle.addEventListener('pointerup', async (event) => { if (!model.drag || model.drag.pointerId !== event.pointerId || model.drag.item !== item) return; const moved = model.drag.moved; model.drag = null; circle.releasePointerCapture(event.pointerId); focusNode(item, false); if (moved) reheatMemoryGraph(model, .72); else { item.fixed = false; reheatMemoryGraph(model, .25); await loadDetail(item.id); document.querySelector('.memory-layout')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); } });
    circle.addEventListener('pointercancel', (event) => { if (model.drag?.pointerId === event.pointerId && model.drag.item === item) { model.drag = null; item.fixed = false; focusNode(item, false); reheatMemoryGraph(model, .5); } });
    circle.addEventListener('keydown', async (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); await loadDetail(item.id); document.querySelector('.memory-layout')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); } });
  }
  svg.addEventListener('pointerdown', (event) => { if (event.target.closest('.memory-network-node')) return; model.pan = { pointerId: event.pointerId, clientX: event.clientX, clientY: event.clientY, tx: model.tx, ty: model.ty }; svg.setPointerCapture(event.pointerId); });
  svg.addEventListener('pointermove', (event) => { if (!model.pan || model.pan.pointerId !== event.pointerId) return; const bounds = svg.getBoundingClientRect(); model.tx = model.pan.tx + (event.clientX - model.pan.clientX) * width / bounds.width; model.ty = model.pan.ty + (event.clientY - model.pan.clientY) * height / bounds.height; viewport.setAttribute('transform', `translate(${model.tx} ${model.ty}) scale(${model.scale})`); });
  svg.addEventListener('pointerup', (event) => { if (model.pan?.pointerId === event.pointerId) { model.pan = null; svg.releasePointerCapture(event.pointerId); } });
  svg.addEventListener('wheel', (event) => { event.preventDefault(); const bounds = svg.getBoundingClientRect(); const px = (event.clientX - bounds.left) * width / bounds.width; const py = (event.clientY - bounds.top) * height / bounds.height; const next = Math.max(.35, Math.min(5, model.scale * Math.exp(-event.deltaY * .0012))); const ratio = next / model.scale; model.tx = px - (px - model.tx) * ratio; model.ty = py - (py - model.ty) * ratio; model.scale = next; viewport.setAttribute('transform', `translate(${model.tx} ${model.ty}) scale(${model.scale})`); }, { passive: false });
  paintMemoryGraph(model); reheatMemoryGraph(model, 1);
}
function paintMemoryGraph(model) { for (const edge of model.edges) { edge.element.setAttribute('x1', edge.sourceNode.x); edge.element.setAttribute('y1', edge.sourceNode.y); edge.element.setAttribute('x2', edge.targetNode.x); edge.element.setAttribute('y2', edge.targetNode.y); } for (const item of model.nodes) { item.element.setAttribute('cx', item.x); item.element.setAttribute('cy', item.y); } }
function reheatMemoryGraph(model, amount) { if (model.cancelled) return; model.alpha = Math.max(model.alpha, amount); if (!model.running) { model.running = true; requestAnimationFrame(() => simulateMemoryGraph(model)); } }
function simulateMemoryGraph(model) {
  if (model.cancelled || model.alpha < .012) { model.running = false; return; } const { nodes, edges, width, height } = model; const alpha = model.alpha;
  for (const edge of edges) { const a = edge.sourceNode; const b = edge.targetNode; const dx = b.x - a.x; const dy = b.y - a.y; const distance = Math.max(1, Math.hypot(dx, dy)); const pull = (distance - 62 - a.radius - b.radius) * .014 * alpha; const fx = dx / distance * pull; const fy = dy / distance * pull; if (!a.fixed) { a.vx += fx; a.vy += fy; } if (!b.fixed) { b.vx -= fx; b.vy -= fy; } }
  const fullPairs = nodes.length <= 420; const samples = fullPairs ? nodes.length : 30; for (let i = 0; i < nodes.length; i++) { const a = nodes[i]; for (let step = 1; step < samples; step++) { const j = fullPairs ? i + step : (i + step * 97) % nodes.length; if (j >= nodes.length || j === i) continue; const b = nodes[j]; let dx = b.x - a.x; let dy = b.y - a.y; const distance2 = Math.max(20, dx * dx + dy * dy); if (distance2 > 30000) continue; const distance = Math.sqrt(distance2); const collision = a.radius + b.radius + 7; const push = (520 / distance2 + Math.max(0, collision - distance) * .08) * alpha; dx /= distance; dy /= distance; if (!a.fixed) { a.vx -= dx * push; a.vy -= dy * push; } if (!b.fixed) { b.vx += dx * push; b.vy += dy * push; } } }
  for (const item of nodes) { if (item.fixed) continue; item.vx += (width / 2 - item.x) * .0018 * alpha; item.vy += (height / 2 - item.y) * .0018 * alpha; item.vx *= .84; item.vy *= .84; item.x = Math.max(18, Math.min(width - 18, item.x + item.vx)); item.y = Math.max(18, Math.min(height - 18, item.y + item.vy)); }
  model.alpha *= .982; paintMemoryGraph(model); requestAnimationFrame(() => simulateMemoryGraph(model));
}
async function loadMemoryMap() {
  const serial = ++state.main.mapSerial;
  try { const data = await api('/api/memory-graph'); if (serial !== state.main.mapSerial) return; renderMemoryGraph(data); }
  catch (error) { if (serial === state.main.mapSerial) { $('memory-map').replaceChildren(node('div', 'notice error', `Unable to load the dependency graph: ${error.message}`)); $('memory-map-meta').textContent = 'Graph unavailable'; } }
}
function relationGraph(item) {
  const relations = (item.relations || []).filter((relation) => relation.target_id); const section = node('section', 'graph-section', node('h3', '', 'Related records', node('span', 'quiet', `${relations.length} links · select a node to open`)));
  if (!relations.length) { section.append(node('p', 'empty', 'This record has no linked records yet.')); return section; }
  const visible = relations.slice(0, 12); const rows = Math.ceil(visible.length / 2); const height = Math.max(150, rows * 62 + 35); const svg = svgNode('svg', { class: 'relation-graph', viewBox: `0 0 600 ${height}`, role: 'img', 'aria-label': `Related records for ${item.id}` }); const center = { x: 300, y: height / 2 };
  const defs = svgNode('defs', {}); const marker = svgNode('marker', { id: 'relation-arrow', viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 5, markerHeight: 5, orient: 'auto-start-reverse' }); marker.append(svgNode('path', { d: 'M 0 0 L 8 4 L 0 8 z', fill: '#a1b8a7' })); defs.append(marker); svg.append(defs);
  const points = visible.map((relation, i) => ({ relation, x: i % 2 ? 493 : 107, y: (Math.floor(i / 2) + .5) * (height - 30) / rows + 15 }));
  for (const point of points) { const left = point.x < center.x; const start = { x: center.x + (left ? -68 : 68), y: center.y }; const end = { x: point.x + (left ? 75 : -75), y: point.y }; svg.append(svgNode('path', { class: 'graph-link', d: `M${start.x},${start.y} C${(start.x + end.x) / 2},${start.y} ${(start.x + end.x) / 2},${end.y} ${end.x},${end.y}`, 'marker-end': 'url(#relation-arrow)' })); const edgeLabel = String(point.relation.kind || 'related'); svg.append(svgNode('text', { class: 'graph-edge-label', x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 - 23, 'text-anchor': 'middle' }, edgeLabel.length > 21 ? `${edgeLabel.slice(0, 20)}…` : edgeLabel)); }
  function graphNode(id, x, y, isCenter) { const group = svgNode('g', { class: `graph-node${isCenter ? ' center' : ''}`, tabindex: '0', role: 'button', 'aria-label': `Open ${id}` }); group.append(svgNode('title', {}, id), svgNode('rect', { x: x - (isCenter ? 68 : 75), y: y - 16, width: isCenter ? 136 : 150, height: 32, rx: 4 }), svgNode('text', { x, y: y + 3, 'text-anchor': 'middle' }, id.length > 21 ? `${id.slice(0, 20)}…` : id)); const select = () => loadDetail(id); group.addEventListener('click', select); group.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(); } }); return group; }
  for (const point of points) svg.append(graphNode(point.relation.target_id, point.x, point.y, false)); svg.append(graphNode(item.id, center.x, center.y, true)); section.append(svg);
  const links = node('div', 'relations-list'); for (const relation of relations) { const link = node('a', 'relation-chip', node('span', '', relation.kind || 'related'), relation.target_id); link.href = `/main-memory?id=${encodeURIComponent(relation.target_id)}`; link.addEventListener('click', (event) => { if (!event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) { event.preventDefault(); loadDetail(relation.target_id); } }); links.append(link); } section.append(links); if (relations.length > visible.length) section.append(node('p', 'quiet', 'The graph shows the first 12 adjacent nodes; the list above contains every relation.')); return section;
}
async function loadDetail(id, scroll = false) {
  const serial = ++state.main.detailSerial; $('memory-detail').setAttribute('aria-busy', 'true');
  try { const item = await api(`/api/main-memory/${encodeURIComponent(id)}`); if (serial !== state.main.detailSerial) return; state.main.selected = item.id; history.replaceState(null, '', `/main-memory?id=${encodeURIComponent(item.id)}`); for (const row of $('main-records').querySelectorAll('.record-button')) row.classList.toggle('selected', row.dataset.id === item.id); for (const row of document.querySelectorAll('.memory-network-node')) row.classList.toggle('selected', row.dataset.mapId === item.id); const detail = $('memory-detail'); detail.replaceChildren(node('div', 'detail-heading', node('span', `type-label ${item.type}`, item.type), node('h2', '', item.id), badge(item.status))); if (item.abstract) detail.append(node('div', 'detail-abstract', markdown(item.abstract))); detail.append(markdown(item.content || 'No complete content is available.'), relationGraph(item)); if (scroll && innerWidth <= 800) detail.scrollIntoView({ behavior: 'auto', block: 'start' }); }
  catch (error) { if (serial === state.main.detailSerial) $('memory-detail').replaceChildren(node('div', 'notice error', `Unable to load ${id}: ${error.message}`)); } finally { if (serial === state.main.detailSerial) $('memory-detail').removeAttribute('aria-busy'); }
}
function journalItem(item) { const row = node('details', 'journal-entry'); const id = item.id; row.dataset.id = id; row.open = state.openJournal.has(id); const meta = [item.worker_session_id || item.session_id, item.attempt_no ? `Attempt ${item.attempt_no}` : '', item.turn_id ? `Turn ${item.turn_id}` : '', item.seq == null ? '' : `#${item.seq}`].filter(Boolean); const summary = node('summary', '', node('div', 'record-top', node('span', 'record-id', id), node('time', 'journal-date', date(item.created_at))), node('div', 'journal-meta', ...meta.map((value) => node('span', '', value))), node('div', 'record-abstract', item.abstract || text(item.content).slice(0, 260) || 'No abstract'), node('span', 'journal-expand', 'Expand full record ↓')); row.append(summary); const content = item.content || item.data || 'No complete content is available.'; const render = () => { if (!row.querySelector(':scope > .prose')) row.append(markdown(content)); }; if (row.open) render(); row.addEventListener('toggle', () => { if (row.open) { state.openJournal.add(id); render(); } else state.openJournal.delete(id); }); return row; }
async function loadExplorer(type) {
  const types = type ? [type] : ['scratch', 'summary']; const query = state.explorer.q;
  const results = await Promise.allSettled(types.map(async (kind) => { const config = state.explorer[kind]; const serial = (config.serial || 0) + 1; config.serial = serial; const params = new URLSearchParams({ type: kind, q: query, offset: config.offset, limit: config.limit }); const data = await api(`/api/explorer-memory?${params}`); if (config.serial !== serial) return; $(`${kind}-total`).textContent = number(data.total); const key = JSON.stringify(data.items); const target = $(`${kind}-records`); if (target.dataset.key !== key) { target.dataset.key = key; target.replaceChildren(...data.items.map(journalItem)); if (!data.items.length) target.append(node('p', 'empty', query ? 'No Explorer records match this search.' : `No ${kind} records.`)); } pagination(`${kind}-pagination`, data, (offset) => { config.offset = offset; loadExplorer(kind); }); }));
  const failed = results.find((result) => result.status === 'rejected'); errorAt('explorer-error', failed ? failed.reason : null);
}
function debounce(handler, delay = 300) { let timer; return (...args) => { clearTimeout(timer); timer = setTimeout(() => handler(...args), delay); }; }

$(`page-${page}`).hidden = false; document.querySelector(`[data-page="${page}"]`)?.classList.add('active'); document.querySelector(`[data-page="${page}"]`)?.setAttribute('aria-current', 'page');
$('guidance-form').addEventListener('submit', async (event) => { event.preventDefault(); const form = event.currentTarget; const submit = form.querySelector('button[type="submit"]'); if (submit.disabled) return; const value = $('guidance-text').value; if (!value.trim()) { feedback('guidance-feedback', 'Enter guidance before submitting.', true); return; } submit.disabled = true; feedback('guidance-feedback', 'Saving guidance…'); try { const result = await api('/api/guidance', { text: value }); if ($('guidance-text').value === value) $('guidance-text').value = ''; feedback('guidance-feedback', `Saved${result.guidance_id || result.id ? ` · ${result.guidance_id || result.id}` : ''} and waiting for delivery.`); await loadOverview(); } catch (error) { feedback('guidance-feedback', error.message, true); } finally { submit.disabled = false; } });
$('advisor-form').addEventListener('submit', async (event) => { event.preventDefault(); const submit = event.currentTarget.querySelector('button[type="submit"]'); if (submit.disabled) return; const choices = state.choices.map((id) => ({ kind: 'listed', obligation_id: id })); for (const id of ['custom-obligation', 'custom-obligation-2']) if ($(id).value.trim()) choices.push({ kind: 'custom', statement: $(id).value.trim() }); if (choices.length < 1 || choices.length > 2) { feedback('advisor-feedback', 'Choose or enter one or two obligations in total.', true); return; } const requestId = state.overview?.advisor?.status === 'waiting_for_human' ? state.overview?.advisor?.request_id : null; if (!requestId) { feedback('advisor-feedback', 'There is no Advisor request waiting for feedback.', true); return; } submit.disabled = true; for (const input of document.querySelectorAll('#advisor-form textarea,#advisor-choices input')) input.disabled = true; feedback('advisor-feedback', 'Submitting…'); try { await api('/api/advisor-feedback', { request_id: requestId, response: { choices, instructions: $('advisor-instructions').value.trim() } }); state.advisorSubmitted = requestId; if (state.overview?.advisor?.request_id === requestId) feedback('advisor-feedback', 'Submitted. Waiting for the research runtime to accept it.'); for (const input of document.querySelectorAll('#advisor-form input,#advisor-form textarea,#advisor-choices input')) input.disabled = true; await loadOverview(); } catch (error) { if (state.overview?.advisor?.request_id === requestId) { feedback('advisor-feedback', error.message, true); submit.disabled = false; for (const input of document.querySelectorAll('#advisor-form textarea,#advisor-choices input')) input.disabled = false; } } });
$('refresh-monitor').addEventListener('click', async () => { if (state.monitorBusy || state.monitorSubmitting) return; state.monitorBusy = true; state.monitorSubmitting = true; state.monitorEpoch++; monitorButton(); try { await api('/api/monitor/refresh', {}); state.monitorSubmitting = false; await loadMonitor(); } catch (error) { errorAt('monitor-error', error); state.monitorBusy = false; } finally { state.monitorSubmitting = false; monitorButton(); } });
for (const filter of $('kind-filters').querySelectorAll('button')) filter.addEventListener('click', () => { state.main.kind = filter.dataset.kind; state.main.offset = 0; for (const item of $('kind-filters').querySelectorAll('button')) item.classList.toggle('active', item === filter); loadMain(); });
$('main-search').addEventListener('input', debounce(() => { state.main.q = $('main-search').value.trim(); state.main.offset = 0; loadMain(); }));
$('explorer-search').addEventListener('input', debounce(() => { state.explorer.q = $('explorer-search').value.trim(); state.explorer.scratch.offset = 0; state.explorer.summary.offset = 0; loadExplorer(); }));
$('refresh-main').addEventListener('click', async () => { $('refresh-main').disabled = true; try { await Promise.all([loadMain(), loadMemoryMap()]); if (state.main.selected) await loadDetail(state.main.selected); } finally { $('refresh-main').disabled = false; } });
$('reset-graph').addEventListener('click', resetMemoryGraph);
$('refresh-explorer').addEventListener('click', async () => { $('refresh-explorer').disabled = true; try { await loadExplorer(); } finally { $('refresh-explorer').disabled = false; } });
loadOverview();
if (page === 'overview') { loadMonitor(); setInterval(updateClock, 1000); }
if (page === 'main') { loadMain(); loadMemoryMap(); const selected = new URLSearchParams(location.search).get('id'); if (selected) loadDetail(selected); }
if (page === 'explorer') { const query = new URLSearchParams(location.search).get('q') || ''; state.explorer.q = query; $('explorer-search').value = query; loadExplorer(); }
setInterval(() => { if (!document.hidden) { loadOverview(); if (page === 'overview') loadMonitor(); } }, 5000);
setInterval(() => { if (!document.hidden) { if (page === 'main') { loadMain(); loadMemoryMap(); } if (page === 'explorer') loadExplorer(); } }, 15000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) { loadOverview(); if (page === 'overview') loadMonitor(); else if (page === 'main') { loadMain(); loadMemoryMap(); } else loadExplorer(); } });
