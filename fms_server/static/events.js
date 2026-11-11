'use strict';
// 이력 페이지: 이벤트 로그를 필터로 조회한다. 로봇 팝업·작업 행에서 eventsView.show({robot, job}) 로 넘어온다.
window.eventsView = (() => {
  const kinds = {
    'job.assigned':'작업 배정', 'job.working':'작업 시작', 'job.success':'작업 완료', 'job.failed':'작업 실패',
    'job.canceled':'작업 취소', 'job.cancel_requested':'취소 요청', 'job.retry':'작업 재시도', 'job.deadline_missed':'마감 초과',
    'job.priority':'우선순위 변경', 'job.preempted':'시스템 작업 선점',
    'robot.state':'로봇 상태 변화', 'robot.liveness':'연결 상태', 'robot.paused':'일시정지', 'robot.resumed':'재개',
    'robot.mode':'운전 모드', 'robot.localization':'위치 추정', 'robot.error':'오류 발생', 'robot.error_cleared':'오류 해제',
    'robot.battery':'배터리', 'robot.charging_started':'충전 시작', 'robot.charging_stopped':'충전 종료', 'robot.usage':'사용량',
    'robot.command':'로봇 명령', 'robot.service':'점검 모드',
    'traffic.wait':'교통 대기', 'traffic.wait_end':'교통 대기 해제', 'traffic.reserve':'교통 예약', 'traffic.release':'교통 예약 해제',
    'traffic.deadlock':'교착', 'traffic.stall':'정체', 'traffic.conflict':'경로 교차 예고', 'traffic.revoke':'예약 회수',
    'elevator.lease':'승강기 임대', 'elevator.release':'승강기 반납', 'elevator.fault':'승강기 장애', 'elevator.reroute':'승강기 예비 전환',
    'elevator.recover':'승강기 칸 내 복구', 'delivery.phase':'배송 단계', 'site.paused':'현장 정지', 'site.resumed':'현장 재개',
    'site.evacuation':'대피', 'map.activated':'맵 활성화', 'zone.created':'구역 생성', 'zone.updated':'구역 수정', 'zone.deleted':'구역 삭제',
    'charge.requested':'자동 충전', 'charge.no_charger':'빈 충전소 없음', 'staging.requested':'대기 위치 복귀',
    'schedule.run':'예약 실행', 'alert.opened':'알림 발생', 'alert.resolved':'알림 해소', 'alert.acked':'알림 확인',
    'server.start':'서버 시작', 'server.stop':'서버 종료'
  };
  const groups = [['','전체'],['job.','작업'],['robot.state,robot.liveness,robot.paused,robot.resumed,robot.mode,robot.localization,robot.error,robot.error_cleared,robot.battery,robot.charging_started,robot.charging_stopped,robot.command,robot.service','로봇'],
    ['traffic.','교통'],['elevator.','승강기'],['delivery.','배송'],['site.,map.,zone.,schedule.,server.','운영 조작'],['charge.,staging.','충전·대기'],['alert.','알림'],['robot.usage','사용량(원본)']];
  const severities = {info:'정보', warning:'주의', error:'오류'};
  const form = $('#eventFilter'), tb = $('#eventRows');
  let busy = false;
  options(form.kind, groups);
  function detail(data) {
    return Object.entries(data || {}).filter(([, v]) => v !== null && v !== '' && v !== undefined)
      .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · ');
  }
  async function refresh() {
    if (busy || app.page !== 'events') return;
    busy = true;
    try {
      const q = new URLSearchParams({limit: form.limit.value || '200'});
      if (app.site && !form.allSites.checked) q.set('site', app.site);
      for (const name of ['robot', 'job', 'kind', 'severity']) if (form[name].value.trim()) q.set(name, form[name].value.trim());
      // 사용량 원본은 따로 고를 때만 보인다 (1분마다 쌓여 목록을 가린다).
      const result = await request(`/api/v1/events?${q}`);
      const rows = result.events.filter(e => form.kind.value || e.kind !== 'robot.usage');
      tb.replaceChildren();
      for (const e of rows) {
        const tr = row(tb, [new Date(e.ts * 1000).toLocaleString(), kinds[e.kind] || e.kind, severities[e.severity] || e.severity,
          e.robot_id, e.job_id, e.actor, detail(e.data)]);
        if (e.severity !== 'info') tr.classList.add(e.severity === 'error' ? 'bad' : 'warn');
      }
      empty(tb, 7, '조건에 맞는 이력이 없습니다.');
      $('#eventCount').textContent = `${rows.length}건`;
    } finally { busy = false; }
  }
  form.addEventListener('submit', e => { e.preventDefault(); run(refresh); });
  form.addEventListener('change', () => run(refresh));
  $('#eventReset').onclick = () => { form.reset(); run(refresh); };
  setInterval(() => run(refresh), 3000);
  function show(filter = {}) {
    form.reset();
    form.robot.value = filter.robot || '';
    form.job.value = filter.job || '';
    showPage('events');
    run(refresh);
  }
  return {refresh, show, kinds};
})();
