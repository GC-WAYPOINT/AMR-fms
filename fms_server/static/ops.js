'use strict';
// 운영 제어: 현장 정지·대피, 자동 충전·대기 정책, 예약 실행, 작업 우선순위·재시도.
window.opsUI = (() => {
  const statusNames = {working:'수행 중', idle:'대기', charging:'충전', manual:'수동', service:'점검', error:'오류', offline:'미접속'};
  const days = ['월','화','수','목','금','토','일'];
  let ops = null, busy = false, policyDirty = false, schedules = [];
  const policyForm = $('#chargingForm'), scheduleForm = $('#scheduleForm');
  policyForm.addEventListener('input', () => { policyDirty = true; });

  // -- 현장 정지·대피 ---------------------------------------------------
  function renderOps() {
    const banner = $('#opsBanner'), e = ops?.evacuation;
    const state = e?.active ? `대피 중 (${e.mode === 'pullover' ? '대기 위치로 이동' : '제자리 정지'})${e.reason ? ' · ' + e.reason : ''}`
      : ops?.paused ? `현장 정지 중${ops.map_ids ? ' (' + ops.map_ids.join(', ') + ')' : ''}${ops.reason ? ' · ' + ops.reason : ''}` : '';
    banner.hidden = !state; banner.textContent = state; banner.className = e?.active ? 'ops-banner danger' : 'ops-banner';
    $('#opsState').textContent = !app.site ? '먼저 현장을 선택하세요.' : state || '정상 운영 중';
    $('#sitePause').disabled = !app.site || !!e?.active;
    $('#siteResume').disabled = !app.site || !ops?.paused || !!e?.active;
    $('#evacStart').disabled = !app.site || !!e?.active;
    $('#evacEnd').disabled = !app.site || !e?.active;
    const list = $('#evacCanceled'); list.replaceChildren();
    if (e?.canceled_jobs?.length) {
      list.append(el('p', `대피로 취소된 작업 ${e.canceled_jobs.length}건 (자동으로 다시 게시하지 않습니다. 작업 이력에서 재시도하세요.)`, 'muted'));
      list.append(el('p', e.canceled_jobs.join(', ')));
    }
  }
  async function post(path, body, done) {
    const result = await request(`${siteURL()}/${path}`, 'POST', body);
    ops = result.ops; renderOps(); notify(done); return result;
  }
  $('#sitePause').onclick = () => run(async () => {
    const scope = $('#pauseScope').value, reason = $('#opsReason').value.trim();
    if (!confirm(scope === 'map' ? `맵 ${app.map}의 로봇을 모두 일시정지할까요?` : '현장의 로봇을 모두 일시정지할까요?')) return;
    const r = await post('pause', scope === 'map' ? {map_id: app.map, reason} : {reason}, '현장을 정지했습니다.');
    notify(`일시정지 명령 ${r.paused_robots.length}대`);
  });
  $('#siteResume').onclick = () => run(async () => {
    const r = await post('resume', undefined, '현장을 재개했습니다.');
    notify(`재개 명령 ${r.resumed_robots.length}대. 로봇에서 건 일시정지는 로봇에서 해제하세요.`);
  });
  $('#evacStart').onclick = () => run(async () => {
    const mode = $('#evacMode').value;
    if (!confirm(mode === 'pullover' ? '대피를 시작할까요? 진행 중인 작업을 취소하고 로봇을 대기 위치로 보냅니다.' : '대피를 시작할까요? 모든 로봇을 제자리에 세웁니다.')) return;
    await post('evacuation', {active: true, mode, reason: $('#opsReason').value.trim()}, '대피를 시작했습니다.');
  });
  $('#evacEnd').onclick = () => run(async () => {
    if (!confirm('대피를 해제할까요? 취소된 작업은 자동으로 다시 게시하지 않습니다.')) return;
    await post('evacuation', {active: false}, '대피를 해제했습니다.');
  });

  // -- 충전·대기 정책 ---------------------------------------------------
  function fillPolicy(p) {
    for (const k of ['low', 'critical', 'release', 'min_job_charge', 'idle_after']) policyForm[k].value = p[k];
    policyForm.enabled.checked = p.enabled; policyForm.opportunity.checked = p.opportunity;
    policyForm.stagingEnabled.checked = p.staging.enabled; policyForm.stagingIdle.value = p.staging.idle_after;
  }
  policyForm.addEventListener('submit', e => {
    e.preventDefault();
    run(async () => {
      const n = k => Number(policyForm[k].value);
      const p = await request(`${siteURL()}/charging`, 'PUT', {
        enabled: policyForm.enabled.checked, opportunity: policyForm.opportunity.checked, low: n('low'), critical: n('critical'),
        release: n('release'), min_job_charge: n('min_job_charge'), idle_after: n('idle_after'),
        staging: {enabled: policyForm.stagingEnabled.checked, idle_after: n('stagingIdle')}});
      policyDirty = false; fillPolicy(p); notify('충전·대기 정책을 저장했습니다.');
    });
  });
  function renderSpots(spots) {
    const tb = $('#spotRows'); tb.replaceChildren();
    const add = (kind, s) => row(tb, [s.name, kind, s.map_id, s.occupied ? '로봇 있음' : s.targeted ? '이동 중인 로봇 있음' : '비어 있음',
      kind === '충전소' && !s.approach_free ? '진입 지점 막힘' : kind === '대기 위치' && !s.entry ? '단순 이동(진입 공간 없음)' : s.entry || '—',
      [el('span', (s.groups || []).join(', ') || '모든 로봇'), button('그룹', async () => {
        const value = prompt(`${s.name}을(를) 쓸 로봇 그룹 (쉼표로 구분, 빈칸 = 모든 로봇)`, (s.groups || []).join(', '));
        if (value === null) return;
        await request(`${siteURL()}/locations/${enc(s.name)}`, 'PUT', {groups: value.split(',').map(x => x.trim()).filter(Boolean)});
        await refresh(true);
      })]]);
    for (const s of spots.chargers) add('충전소', s);
    for (const s of spots.parkings) add('대기 위치', s);
    empty(tb, 6, '충전소·주차 지점 목적지가 없습니다. 목적지·도킹에서 등록하세요.');
  }

  // -- 예약 실행 --------------------------------------------------------
  function renderSchedules() {
    const tb = $('#scheduleRows'); tb.replaceChildren();
    const presets = new Map((window.jobUI?.presets || []).map(p => [p.preset_id, p.name]));
    for (const s of schedules) {
      const when = s.time ? `${(s.days.length === 7 ? '매일' : s.days.map(d => days[d]).join('·'))} ${s.time}` : `${Math.round(s.interval_s)}초마다`;
      row(tb, [s.name || '—', presets.get(s.preset_id) || '(삭제된 프리셋)', when, s.priority, s.robot_group, s.enabled ? '사용' : '중지',
        s.last_error ? `실패: ${s.last_error}` : s.last_run ? new Date(s.last_run * 1000).toLocaleString() : '—',
        [button(s.enabled ? '중지' : '사용', async () => {
          await request(`${siteURL()}/schedules/${enc(s.id)}`, 'PUT', {name: s.name, preset_id: s.preset_id, days: s.days, time: s.time,
            interval_s: s.interval_s, robot_group: s.robot_group, priority: s.priority, enabled: !s.enabled});
          await refresh(true);
        }), button('삭제', async () => {
          if (!confirm('이 예약을 삭제할까요?')) return;
          await request(`${siteURL()}/schedules/${enc(s.id)}`, 'DELETE'); await refresh(true);
        }, 'danger')]]);
    }
    empty(tb, 8, '등록된 예약이 없습니다.');
    options(scheduleForm.preset_id, [...presets], '프리셋 선택');
  }
  scheduleForm.addEventListener('submit', e => {
    e.preventDefault();
    run(async () => {
      const interval = scheduleForm.kind.value === 'interval';
      const picked = [...scheduleForm.querySelectorAll('[name=day]:checked')].map(i => Number(i.value));
      await request(`${siteURL()}/schedules`, 'POST', {
        name: scheduleForm.name.value.trim(), preset_id: scheduleForm.preset_id.value,
        days: picked.length ? picked : null, time: interval ? null : scheduleForm.time.value,
        interval_s: interval ? Number(scheduleForm.interval.value) * 60 : null,
        robot_group: scheduleForm.robot_group.value.trim() || null, priority: Number(scheduleForm.priority.value) || 0});
      scheduleForm.reset(); kindChanged(); await refresh(true); notify('예약을 등록했습니다.');
    });
  });
  function kindChanged() {
    const interval = scheduleForm.kind.value === 'interval';
    $('#scheduleTimeLabel').hidden = interval; $('#scheduleIntervalLabel').hidden = !interval;
  }
  scheduleForm.kind.onchange = kindChanged;
  const dayBox = $('#scheduleDays');
  days.forEach((d, i) => { const l = el('label', undefined, 'check'), c = el('input'); c.type = 'checkbox'; c.name = 'day'; c.value = i; l.append(c, d); dayBox.append(l); });
  kindChanged();

  // -- 작업 행 조작 (fms.js renderJobs 가 부른다) ------------------------
  function jobActions(j, actions) {
    if (j.status === 'pending' && !j.system) actions.push(button(`우선순위 ${j.priority}`, async () => {
      const value = prompt('우선순위 (-100 ~ 100, 클수록 먼저 배정)', j.priority);
      if (value === null || value.trim() === '') return;
      await request(`${siteURL()}/jobs/${enc(j.job_id)}`, 'PATCH', {priority: Number(value)}); await refreshSite();
    }));
    if (['failed', 'canceled', 'success'].includes(j.status) && !j.system) actions.push(button('재시도', async () => {
      const r = await request(`${siteURL()}/jobs/${enc(j.job_id)}/retry`, 'POST', {}); await refreshSite();
      notify(`${r.job_id} 작업을 새로 만들었습니다.`);
    }));
  }
  function jobNote(j) {
    const parts = [];
    if (j.priority) parts.push(`우선순위 ${j.priority}`);
    if (j.robot_group) parts.push(`그룹 ${j.robot_group}`);
    if (j.not_before && j.status === 'pending') parts.push(`${new Date(j.not_before).toLocaleString()} 이후`);
    if (j.deadline) parts.push(`${j.deadline_missed ? '마감 초과' : '마감'} ${new Date(j.deadline).toLocaleString()}`);
    if (j.retry_of) parts.push(`${j.retry_of} 재시도`);
    return parts.join(' · ');
  }

  // -- 로봇 행 조작 ------------------------------------------------------
  function robotActions(assigned, actions) {
    actions.push(button(assigned.in_service ? '점검 전환' : '서비스 복귀', async () => {
      let note = assigned.note || '';
      if (assigned.in_service) { note = prompt(`${assigned.robot_id}을(를) 점검(서비스 제외)으로 바꿉니다. 사유:`, note); if (note === null) return; }
      await request(`${siteURL()}/robots/${enc(assigned.robot_id)}`, 'PUT', {in_service: !assigned.in_service, note});
      await refreshSite(); notify(assigned.in_service ? '점검 모드로 바꿨습니다. 진행 중인 작업은 끝까지 수행합니다.' : '서비스에 복귀했습니다.');
    }), button('그룹', async () => {
      const value = prompt('로봇 그룹 (쉼표로 구분)', (assigned.groups || []).join(', ')); if (value === null) return;
      await request(`${siteURL()}/robots/${enc(assigned.robot_id)}`, 'PUT', {groups: value.split(',').map(x => x.trim()).filter(Boolean)});
      await refreshSite();
    }));
  }
  const robotStatus = (r, assigned) => !assigned.in_service ? `점검${assigned.note ? ' · ' + assigned.note : ''}` : statusNames[r?.display_status] || '상태 수신 전';

  async function refresh(force = false) {
    if (busy && !force) return;
    if (!app.site) { ops = null; renderOps(); return; }
    busy = true;
    try {
      const site = app.site;
      ops = await request(`${siteURL()}/ops`);
      if (site !== app.site) return;
      renderOps();
      if (app.page === 'ops' || force) {
        const [policy, spots] = await Promise.all([request(`${siteURL()}/charging`), request(`${siteURL()}/charging/spots`)]);
        if (site !== app.site) return;
        if (!policyDirty) fillPolicy(policy);
        renderSpots(spots);
      }
      if (app.page === 'presets' || force) { schedules = await request(`${siteURL()}/schedules`); if (site === app.site) renderSchedules(); }
    } finally { busy = false; }
  }
  setInterval(() => run(() => refresh()), 3000);
  return {refresh, jobActions, jobNote, robotActions, robotStatus, statusNames, resetSite() { policyDirty = false; ops = null; schedules = []; }};
})();
