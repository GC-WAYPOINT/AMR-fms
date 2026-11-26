'use strict';
// 주기 갱신은 입력 중인 제한값을 덮어쓰지 않는다.
window.trafficView = (() => {
  let current = '', dirty = false, busy = false, snapshot = null, generation = 0, fetchedAt = 0;
  const input = $('#trafficLimit'), pauseBox = $('#trafficConflictPause');
  input.addEventListener('input', () => { dirty = true; });
  pauseBox.addEventListener('change', () => { dirty = true; });
  async function refresh() {
    const token = ++generation;
    const key = app.site && app.map ? mapURL() : '';
    if (key !== current) { current = key; dirty = false; snapshot = null; input.value = ''; }
    $('#trafficForm').querySelector('button').disabled = !key || busy;
    input.disabled = !key || busy;
    $('#trafficMapName').textContent = key ? `${app.site} / ${app.map}` : '상단에서 현장과 맵을 선택하세요.';
    if (!key) { $('#trafficStatus').replaceChildren(); return; }
    const result = await request(`${key}/traffic`);
    if (token !== generation || key !== current || key !== mapURL()) return;
    snapshot = result; fetchedAt = Date.now();
    if (!dirty) { input.value = result.max_moving_robots ?? ''; pauseBox.checked = !!result.conflict_pause; }
    const box = $('#trafficStatus'); box.replaceChildren();
    // 교착(진짜 순환)과 정체(안 움직임)를 구분해 보인다. 자동 해소는 탑승 전 도착 층 예약 회수뿐이다.
    for (const d of result.deadlocks || []) box.append(el('p', `교착: ${d.robots.join(' ↔ ')} · ${d.resources.map(r => r.split(':').at(-1)).join(', ')} · 운영자 확인 필요`, 'bad'));
    for (const s of result.stalls || []) if (!s.in_deadlock) box.append(el('p', `정체: ${s.robot_id} · ${s.kind === 'moving' ? '이동 중인데 제자리' : '통행 대기 장기화'} ${Math.round(s.seconds)}초${s.reason ? ' · ' + s.reason : ''}`, 'warn'));
    for (const c of result.conflicts || []) box.append(el('p', `경로 교차 예고: ${c.robots.join(' · ')} (${c.distance} m)${c.paused ? ' · ' + c.paused + ' 일시정지' : ''}`, 'warn'));
    const names = {free:'비어 있음',reserved:'진입 예약',occupied:'점유 중',blocked:'확인 필요 · 신규 진입 차단'};
    for (const zone of result.zones) box.append(el('p',`${zone.name || zone.zone_id} · ${names[zone.status]}${zone.owner ? ' · '+zone.owner : ''}`));
    for (const lease of result.leases) {
      const label = lease.kind === 'floor' ? '이동 허가' : lease.kind === 'wait' ? '대기 node 예약' : '점유 존 예약';
      box.append(el('p', `${lease.robot} · ${label}${lease.future ? ' (도착 층)' : ''} · ${lease.resource.split(':').at(-1)}`));
    }
    // 맵 전역 장애물: 로봇이 보고한 정지 장애물을 일정 시간 기억해 경로 선택에 반영한다.
    if ((result.obstacles || []).length) {
      const soonest = Math.min(...result.obstacles.map(o => o.expires_in)), sec = Math.max(0, Math.round(soonest));
      box.append(el('p', `전역 장애물 ${result.obstacles.length}칸 · 가장 빠른 해제 ${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')} 후 (다른 로봇이 지나가며 안 보이면 즉시 해제)`, 'warn'));
    }
    for (const waiting of result.waiting) box.append(el('p', `${waiting.robot_id} · ${waiting.reason}`));
    if (!result.leases.length && !result.waiting.length) box.append(el('p','예약·대기 중인 로봇이 없습니다.','muted'));
    draw();
  }
  $('#trafficForm').addEventListener('submit', e => {
    e.preventDefault();
    run(async () => {
      if (!current || busy) return;
      const key = current, value = input.value === '' ? null : Number(input.value);
      if (!app.site || !app.map || key !== mapURL()) {
        await refresh();
        throw new Error('선택 맵이 바뀌었습니다. 설정값을 다시 확인하세요.');
      }
      if (value !== null && (!Number.isInteger(value) || value < 1)) throw new Error('1 이상의 정수 또는 빈칸을 입력하세요.');
      busy = true; ++generation;
      try {
        await request(`${key}/traffic`, 'PUT', {max_moving_robots:value, conflict_pause:pauseBox.checked});
        if (current === key) dirty = false;
        notify('층별 동시 이동 대수 제한을 저장했습니다.');
      } finally { busy = false; }
      await refresh();
    });
  });
  setInterval(() => { if (!busy) run(refresh); }, 1500);
  // 남은 시간이 1초마다 줄어 보이도록 장애물이 있을 때만 지도를 다시 그린다.
  setInterval(() => { if (snapshot && (snapshot.obstacles || []).length) draw(); }, 1000);
  return {refresh, get fetchedAt(){return fetchedAt;}, get snapshot(){return app.site && app.map && current===mapURL() ? snapshot : null;}};
})();
