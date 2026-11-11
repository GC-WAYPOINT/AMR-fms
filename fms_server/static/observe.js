'use strict';
// 통계(KPI), 알림, webhook·API 키 설정. 차트는 Canvas 로 직접 그린다 (CDN·빌드 없음).
window.observeUI = (() => {
  const fmt = (v, unit = '', digits = 1) => v === null || v === undefined ? '—' : `${Number(v).toFixed(digits)}${unit}`;
  const duration = s => s === null || s === undefined ? '—' : s < 90 ? `${Math.round(s)}초` : s < 5400 ? `${(s / 60).toFixed(1)}분` : `${(s / 3600).toFixed(1)}시간`;
  const statusColors = {working:'#4aa8ff', idle:'#7d8da3', charging:'#59c8be', manual:'#f0b45a', service:'#ab9aee', error:'#f2848c'};
  const statusNames = {working:'수행', idle:'대기', charging:'충전', manual:'수동', service:'점검', error:'오류'};
  let kpiBusy = false, alertBusy = false, lastAlertId = 0;

  // -- 통계 -------------------------------------------------------------
  function chart(canvas, labels, series, {stacked = false, yLabel = ''} = {}) {
    const ratio = window.devicePixelRatio || 1, w = canvas.clientWidth || 600, h = canvas.clientHeight || 200;
    canvas.width = w * ratio; canvas.height = h * ratio;
    const g = canvas.getContext('2d'); g.setTransform(ratio, 0, 0, ratio, 0, 0); g.clearRect(0, 0, w, h);
    const pad = {l: 44, r: 10, t: 12, b: 24}, pw = w - pad.l - pad.r, ph = h - pad.t - pad.b, n = labels.length;
    const totals = labels.map((_, i) => stacked ? series.reduce((a, s) => a + (s.values[i] || 0), 0) : Math.max(...series.map(s => s.values[i] || 0)));
    const max = Math.max(1e-9, ...totals), top = max <= 1 ? 1 : Math.ceil(max * 1.1);
    g.font = '11px system-ui, sans-serif'; g.fillStyle = '#8fa1b8'; g.strokeStyle = '#26364a'; g.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = pad.t + ph * (1 - i / 4);
      g.beginPath(); g.moveTo(pad.l, y); g.lineTo(w - pad.r, y); g.stroke();
      g.textAlign = 'right'; g.fillText((top * i / 4).toFixed(top <= 4 ? 1 : 0), pad.l - 6, y + 4);
    }
    g.textAlign = 'center';
    const every = Math.max(1, Math.ceil(n / 12));
    labels.forEach((text, i) => { if (i % every === 0) g.fillText(text, pad.l + pw * (i + .5) / n, h - 7); });
    if (yLabel) { g.textAlign = 'left'; g.fillText(yLabel, pad.l + 4, pad.t + 10); }
    const bw = pw / n * .7;
    labels.forEach((_, i) => {
      let base = 0;
      series.forEach((s, k) => {
        const v = s.values[i] || 0; if (!v) return;
        const x = pad.l + pw * (i + .5) / n - bw / 2 + (stacked ? 0 : k * bw / series.length);
        const bh = ph * v / top, y = pad.t + ph - bh - (stacked ? ph * base / top : 0);
        g.fillStyle = s.color; g.fillRect(x, y, stacked ? bw : bw / series.length - 1, bh);
        if (stacked) base += v;
      });
    });
  }
  function legend(box, series) {
    box.replaceChildren(...series.map(s => { const item = el('span', s.name, 'chart-key'); item.style.setProperty('--key', s.color); return item; }));
  }
  function tile(box, title, value, note) {
    const t = el('div', undefined, 'kpi-tile'); t.append(el('small', title), el('strong', value));
    if (note) t.append(el('span', note, 'muted')); box.append(t);
  }
  async function refreshKpi() {
    if (kpiBusy || app.page !== 'stats') return;
    if (!app.site) { $('#kpiTiles').replaceChildren(el('p', '먼저 현장을 선택하세요.', 'muted')); return; }
    kpiBusy = true;
    try {
      const range = $('#kpiRange').value, day = range !== '24h', now = Date.now() / 1000;
      const span = {'24h': 86400, '7d': 7 * 86400, '30d': 30 * 86400}[range];
      const k = await request(`${siteURL()}/kpi?bucket=${day ? 'day' : 'hour'}&from=${now - span}&to=${now}`);
      const t = k.totals, tiles = $('#kpiTiles'); tiles.replaceChildren();
      tile(tiles, '완료 작업', String(t.completed), `실패 ${t.failed} · 취소 ${t.canceled}`);
      tile(tiles, '성공률', t.success_rate === null ? '—' : `${(t.success_rate * 100).toFixed(1)}%`, `시스템 작업 ${t.system_jobs}건 제외`);
      tile(tiles, '배정 대기', duration(t.assign_wait_avg), '게시 → 배정 평균');
      tile(tiles, '수행 시간', duration(t.duration_avg), '배정 → 완료 평균');
      tile(tiles, '가동률', t.utilization === null ? '—' : `${(t.utilization * 100).toFixed(1)}%`, '접속 시간 중 수행 비율');
      tile(tiles, '주행 거리', `${fmt(t.distance / 1000, ' km', 2)}`, `충전 ${t.charges}회 · ${duration(t.charge_seconds)}`);
      tile(tiles, '교통 대기', duration(t.traffic_wait_avg), `${t.traffic_waits}회 · 합계 ${duration(t.traffic_wait_total)}`);
      tile(tiles, '승강기 대기', duration(t.elevator_wait_avg), `교착 ${t.deadlocks} · 정체 ${t.stalls}`);
      tile(tiles, '배송 리드타임', duration(t.delivery_lead_avg), `배송 ${t.deliveries}건 · 마감 초과 ${t.deadline_missed}`);
      const labels = k.buckets.map(b => { const d = new Date(b.start * 1000); return day ? `${d.getMonth() + 1}/${d.getDate()}` : `${d.getHours()}시`; });
      const jobs = [{name:'완료', color:'#59c8be', values:k.buckets.map(b => b.completed)}, {name:'실패', color:'#f2848c', values:k.buckets.map(b => b.failed)},
        {name:'취소', color:'#7d8da3', values:k.buckets.map(b => b.canceled)}];
      chart($('#kpiJobs'), labels, jobs, {stacked: true, yLabel: '작업 수'}); legend($('#kpiJobsKey'), jobs);
      const time = Object.keys(statusNames).map(s => ({name: statusNames[s], color: statusColors[s], values: k.buckets.map(b => b.seconds[s] / 3600)}));
      chart($('#kpiStates'), labels, time, {stacked: true, yLabel: '로봇·시간'}); legend($('#kpiStatesKey'), time);
      const waits = [{name:'배정 대기', color:'#f0b45a', values:k.buckets.map(b => (b.assign_wait_avg || 0) / 60)},
        {name:'교통 대기', color:'#4aa8ff', values:k.buckets.map(b => (b.traffic_wait_avg || 0) / 60)},
        {name:'승강기 대기', color:'#ab9aee', values:k.buckets.map(b => (b.elevator_wait_avg || 0) / 60)}];
      chart($('#kpiWaits'), labels, waits, {yLabel: '평균 대기 (분)'}); legend($('#kpiWaitsKey'), waits);
      const tb = $('#kpiRobots'); tb.replaceChildren();
      for (const r of k.robots) row(tb, [r.robot_id, r.utilization === null ? '—' : `${(r.utilization * 100).toFixed(1)}%`, duration(r.seconds.working),
        duration(r.seconds.idle), duration(r.seconds.charging), duration(r.seconds.error), fmt(r.distance, ' m', 0), `${r.charges}회`, `${r.completed} / ${r.failed}`]);
      empty(tb, 9, '이 기간에 기록된 로봇 사용량이 없습니다.');
    } finally { kpiBusy = false; }
  }
  $('#kpiRange').onchange = () => run(refreshKpi);
  window.addEventListener('resize', () => run(refreshKpi));

  // -- 알림 -------------------------------------------------------------
  async function refreshAlerts() {
    if (alertBusy) return;
    alertBusy = true;
    try {
      const all = $('#alertHistory').checked && app.page === 'alerts';
      const q = new URLSearchParams({open: all ? 'false' : 'true'});
      if (app.site) q.set('site', app.site);
      const data = await request(`/api/v1/alerts?${q}`);
      const open = data.alerts.filter(a => a.open), unacked = open.filter(a => !a.acked_by);
      const badge = $('#alertBadge'); badge.hidden = !open.length; badge.textContent = open.length;
      badge.classList.toggle('quiet', !unacked.length);
      const newest = Math.max(0, ...open.map(a => a.id));
      if (lastAlertId && newest > lastAlertId) for (const a of open.filter(x => x.id > lastAlertId)) notify(`알림: ${a.message}`, a.severity === 'error');
      lastAlertId = Math.max(lastAlertId, newest) || lastAlertId || -1;
      if (app.page !== 'alerts') return;
      const tb = $('#alertRows'); tb.replaceChildren();
      for (const a of data.alerts) {
        const actions = [];
        if (!a.acked_by) actions.push(button('확인', async () => { await request(`/api/v1/alerts/${a.id}/ack`, 'POST', {}); await refreshAlerts(); }));
        if (a.data.robot_id) actions.push(button('로봇 이력', () => window.eventsView?.show({robot: a.data.robot_id})));
        if (a.data.job_id) actions.push(button('작업 이력', () => window.eventsView?.show({job: a.data.job_id})));
        const tr = row(tb, [new Date(a.opened_at * 1000).toLocaleString(), a.message, a.open ? '진행 중' : `해소 ${new Date(a.resolved_at * 1000).toLocaleTimeString()}`,
          a.acked_by ? `${a.acked_by} · ${new Date(a.acked_at * 1000).toLocaleTimeString()}` : '미확인', actions]);
        if (a.open) tr.classList.add(a.severity === 'error' ? 'bad' : 'warn');
      }
      empty(tb, 5, all ? '알림 이력이 없습니다.' : '열린 알림이 없습니다.');
    } finally { alertBusy = false; }
  }
  $('#alertHistory').onchange = () => run(refreshAlerts);

  // -- webhook · API 키 (현장 설정) --------------------------------------
  async function refreshIntegrations() {
    if (app.page !== 'settings') return;
    const me = await request('/api/v1/auth/me');
    $('#authState').textContent = me.enabled ? `인증 사용 중 · ${me.name || '키 없음'} (${me.role || '권한 없음'})` : '인증 꺼짐 (FMS_AUTH=1 로 켭니다). 지금은 누구나 모든 조작을 할 수 있습니다.';
    const admin = !me.enabled || me.role === 'admin';
    $('#integrationPanels').hidden = !admin;
    if (!admin) return;
    const hooks = await request('/api/v1/webhooks'), tb = $('#webhookRows'); tb.replaceChildren();
    for (const h of hooks.webhooks) row(tb, [h.name || '—', h.url, (h.kinds.length ? h.kinds : ['job.', 'alert.']).join(', '), h.has_secret ? '서명 사용' : '서명 없음', h.enabled ? '사용' : '중지',
      [button(h.enabled ? '중지' : '사용', async () => { await request(`/api/v1/webhooks/${enc(h.id)}`, 'PUT', {name: h.name, url: h.url, kinds: h.kinds, enabled: !h.enabled}); await refreshIntegrations(); }),
       button('삭제', async () => { if (!confirm('이 webhook 을 삭제할까요?')) return; await request(`/api/v1/webhooks/${enc(h.id)}`, 'DELETE'); await refreshIntegrations(); }, 'danger')]]);
    empty(tb, 6, '등록된 webhook 이 없습니다.');
    $('#webhookPending').textContent = hooks.pending ? `전송 대기 ${hooks.pending}건` : '';
    const keys = await request('/api/v1/auth/keys'), kb = $('#apiKeyRows'); kb.replaceChildren();
    for (const k of keys.keys) row(kb, [k.name, k.role, k.created_at ? new Date(k.created_at * 1000).toLocaleString() : '—',
      button('삭제', async () => { if (!confirm(`API 키 '${k.name}'을(를) 삭제할까요?`)) return; await request(`/api/v1/auth/keys/${enc(k.name)}`, 'DELETE'); await refreshIntegrations(); }, 'danger')]);
    empty(kb, 4, '등록된 API 키가 없습니다.');
  }
  $('#webhookForm').addEventListener('submit', e => {
    e.preventDefault();
    run(async () => {
      const f = e.target;
      await request('/api/v1/webhooks', 'POST', {name: f.name.value.trim(), url: f.url.value.trim(), secret: f.secret.value,
        kinds: f.kinds.value.split(',').map(x => x.trim()).filter(Boolean)});
      f.reset(); await refreshIntegrations(); notify('webhook 을 등록했습니다.');
    });
  });
  $('#apiKeyForm').addEventListener('submit', e => {
    e.preventDefault();
    run(async () => {
      const f = e.target, r = await request('/api/v1/auth/keys', 'POST', {name: f.name.value.trim(), role: f.role.value});
      f.reset(); await refreshIntegrations();
      prompt(`API 키 '${r.name}' (${r.role}). 지금만 볼 수 있습니다. 복사해 두세요.`, r.key);
    });
  });
  $('#apiKeyChange').onclick = () => { const key = prompt('이 브라우저에서 쓸 API 키 (빈칸 = 지우기)'); if (key === null) return; window.fmsAuth.set(key.trim()); location.reload(); };

  setInterval(() => { run(refreshAlerts); run(refreshIntegrations); }, 4000);
  setInterval(() => run(refreshKpi), 30000);
  return {refreshKpi, refreshAlerts, refreshIntegrations,
    pageShown(page) { if (page === 'stats') run(refreshKpi); if (page === 'alerts') run(refreshAlerts); if (page === 'settings') run(refreshIntegrations); }};
})();
