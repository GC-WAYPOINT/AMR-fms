"""점유 존 대기열: 줄 서기(선입선출)·먼저 올 로봇 우선·다른 방향 줄의 도착 순서."""
from fms_server.tests.test_traffic import BASE, dispatch, env, latest, leases, pose, zone_body  # noqa: F401

# 서쪽 gate 의 대기열: 대기 node (5,3) 뒤로 (4,3), (3,3). 오더 node 순서는 [줄 끝, 2번째, 대기, 출구, 목적지].
TAIL, SECOND, WAIT, EXIT, GOAL = range(5)


def queue_zone(e, queue=((4, 3), (3, 3))):
    body = zone_body(e['version'])
    body['gates'][0]['queue'] = [pose(x, y) for x, y in queue]
    r = e['client'].post(BASE + '/zones', json=body)
    assert r.status_code == 200, r.text
    e['registry'].register_robot('hq', 'R4', 'f1')
    # A(R1) 가 줄 끝에 가장 가깝고, C(R4) 가 B(R3) 보다 가깝다.
    for rid, x, y in [('R1', 2, 2), ('R2', 13, 7), ('R3', 0.5, 5), ('R4', 1, 1)]:
        e['report'](rid, x, y)
    return r.json()


def released(job):
    return [n.released for n in latest(job).nodes]


def at(e, job, node, x=None, y=None):
    """로봇이 오더의 node 에 도착해 서 있다고 보고한다."""
    order = latest(job)
    p = order.nodes[node].position
    e['report'](job.robot_id, p.x if x is None else x, p.y if y is None else y, order, node=node)


def kinds(e, robot):
    return sorted((v['kind'], v.get('seq')) for v in e['coord'].traffic.data['leases'].values()
                  if v['robot'] == robot and v['kind'] != 'floor')


def test_zone_api_stores_queue_and_rejects_bad_queue(env):
    zone = queue_zone(env)
    stored = env['registry'].map_store('hq').load_annotations('f1').zone_by_id(zone['zone_id'])
    assert [(q.x, q.y) for q in stored.gates[0].queue] == [(4, 3), (3, 3)]
    body = zone_body(zone['version'])
    body.update(x=1, width=0.5)
    body['gates'] = [dict(gate_id='g', wait=pose(0.5, 6), exit=pose(2.5, 6), queue=[pose(0.5, 6.3)])]
    r = env['client'].post(BASE + '/zones', json=body)
    assert r.status_code == 422 and '떨어뜨리' in r.text      # 대기 node 와 너무 가까운 자리
    body['gates'][0]['queue'] = [pose(1.2, 4)]               # 존 안
    assert env['client'].post(BASE + '/zones', json=body).status_code == 422


def test_robots_queue_in_arrival_order_and_move_up_when_zone_frees(env):
    queue_zone(env)
    a, oa = dispatch(env, 'R1', 'east')
    assert [n.position.x for n in oa.nodes[:EXIT + 1]] == [3, 4, 5, 11]
    # 존도 줄도 비었다: A 가 맨 앞(대기 node)을 잡고, 지나가는 줄 자리도 예약한다.
    assert released(a) == [True, True, True, False, False]
    assert kinds(env, 'R1') == [('queue', 0), ('queue', 1), ('wait', WAIT)]
    b, _ = dispatch(env, 'R3', 'east')
    c, _ = dispatch(env, 'R4', 'east')
    assert not any(released(b)) and not any(released(c))
    assert b.traffic['waiting'] == c.traffic['waiting'] == '대기열 가득 참'   # A 가 아직 줄 끝을 지나는 중

    # A 가 대기 node 근처까지 오면 서지 않고 존에 들어간다. 지나온 줄 자리는 풀린다.
    at(env, a, SECOND, x=4.2)
    assert released(a)[EXIT] and len(leases(env, 'zone')) == 1
    assert ('queue', 0) not in kinds(env, 'R1')
    at(env, a, WAIT)
    assert kinds(env, 'R1') == [('wait', WAIT), ('zone', None)]
    env['coord'].tick_traffic()
    # B 보다 줄 끝에 가까운 C 가 먼저 선다. 앞(대기 node)은 A 가 존을 나갈 때까지 가지고 있으므로 2번째 자리다.
    assert released(c) == [True, True, False, False, False]
    assert c.traffic['waiting'] == '대기열 2번째 자리 대기'
    assert b.traffic['waiting'] == '대기열 가득 참'

    at(env, c, TAIL)
    at(env, c, SECOND)
    env['coord'].tick_traffic()
    assert released(b) == [True, False, False, False, False]
    assert b.traffic['waiting'] == '대기열 3번째 자리 대기'
    at(env, b, TAIL)

    # A 가 출구를 지나 존을 비우면 C 가 맨 앞으로 당겨 (대기 node 1 m 앞) 바로 들어가고, B 가 2번째로 당긴다.
    at(env, a, EXIT, x=12.6)                             # 출구를 지나 출구 자리도 비웠다
    env['coord'].tick_traffic()
    assert released(c)[EXIT] and leases(env, 'zone')[0]['robot'] == 'R4'
    assert released(b) == [True, False, False, False, False]   # C 가 아직 2번째 자리에 있다
    at(env, c, WAIT)
    env['coord'].tick_traffic()
    assert released(b) == [True, True, False, False, False]
    assert b.traffic['waiting'] == '대기열 2번째 자리 대기'


def test_robot_ahead_still_on_next_slot_keeps_follower_back(env):
    queue_zone(env)
    a, _ = dispatch(env, 'R1', 'east')
    at(env, a, TAIL)
    env['coord'].tick_traffic()
    b, _ = dispatch(env, 'R3', 'east')
    assert not any(released(b))                          # A 가 줄 끝을 아직 벗어나지 않았다
    at(env, a, SECOND, x=4.0)
    env['coord'].tick_traffic()
    assert released(b) == [True, False, False, False, False]


def test_other_gate_head_that_arrived_first_enters_first(env):
    r = env['client'].post(BASE + '/zones', json=zone_body(env['version']))
    assert r.status_code == 200, r.text
    env['report']('R1', 3, 3)                            # 새 맵 버전으로 다시 보고
    env['report']('R2', 13, 7)
    env['report']('R3', 8, 5)                            # 작업 없는 로봇이 존 안에 서 있어 둘 다 기다린다
    a, _ = dispatch(env, 'R1', 'east')                   # 서쪽 gate: 대기 (5,3) -> 출구 (11,3)
    b, _ = dispatch(env, 'R2', 'west')                   # 동쪽 gate: 대기 (11,7) -> 출구 (5,7)
    at(env, b, 0)                                        # R2 가 줄 맨 앞(대기 node)에 먼저 도착
    at(env, a, 0)                                        # R1 은 나중에 도착 (R1 작업이 먼저 배정됐다)
    assert not latest(a).nodes[1].released and not latest(b).nodes[1].released
    env['report']('R3', 14, 9)                           # 존이 비었다
    env['coord'].tick_traffic()
    assert latest(b).nodes[1].released                   # 먼저 도착한 R2 가 먼저 들어간다
    assert not latest(a).nodes[1].released
    assert a.traffic['waiting'] == '점유 존 진입 허가 대기' and leases(env, 'zone')[0]['robot'] == 'R2'


def test_ten_robots_pass_one_at_a_time_in_arrival_order(env):
    """10대가 한 존으로 몰린다: 첫 로봇은 바로 지나가고 나머지는 대기열에서 순서대로 당겨 들어간다.

    로봇은 허가된 마지막 node 까지 한 번에 이동한다고 흉내 낸다. 매 단계 존 안 로봇은 1대 이하,
    한 자리에 두 로봇이 서지 않아야 하고, 존 진입 순서는 (첫 배정 로봇, 그 뒤 줄 끝에 가까운 순)이다."""
    queue = [(4, 3), (3, 3), (2, 3), (1, 3)]
    body = zone_body(env['version'])
    body['gates'][0]['queue'] = [pose(x, y) for x, y in queue]
    r = env['client'].post(BASE + '/zones', json=body)
    assert r.status_code == 200, r.text
    starts = {f'Q{i}': (0.6 + 0.9 * (i % 5), 5.5 + 1.5 * (i // 5)) for i in range(10)}
    for rid, (x, y) in starts.items():
        env['registry'].register_robot('hq', rid, 'f1')
        env['report'](rid, x, y)
    for rid, x, y in [('R1', 14, 9), ('R2', 14, 1), ('R3', 15, 5)]:
        env['report'](rid, x, y)
    jobs = {rid: dispatch(env, rid, 'east')[0] for rid in starts}
    tail = queue[-1]
    first = 'Q0'
    rest = sorted((rid for rid in starts if rid != first),
                  key=lambda rid: ((starts[rid][0] - tail[0]) ** 2 + (starts[rid][1] - tail[1]) ** 2, jobs[rid].job_id))
    traffic = env['coord'].traffic
    entered, grant = [], traffic._zone_entered
    def record(zone_key, now):                          # 존 진입 허가 순서
        entered.append(traffic.data['leases'][zone_key]['robot'])
        grant(zone_key, now)
    traffic._zone_entered = record
    reached = {rid: -1 for rid in starts}
    for _ in range(200):
        for rid, job in jobs.items():
            order = latest(job)
            frontier = max((n.sequence_id for n in order.nodes if n.released), default=-1)
            if frontier > reached[rid]:
                if any(n.allowed_zone_ids for n in order.nodes[reached[rid] + 1:frontier + 1]):
                    env['report'](rid, 8, 3, order, node=max(reached[rid], 0))   # 존 한가운데를 지나는 중
                    assert [q for q in starts if 7 <= env['fleet'].get(q).state.pose.x <= 9] == [rid]
                reached[rid] = frontier
                p = order.nodes[frontier].position
                env['report'](rid, p.x, p.y, order, node=frontier)
                inside = [q for q in starts if 7 <= env['fleet'].get(q).state.pose.x <= 9]
                assert len(inside) <= 1                 # 존 안에는 한 대만
                spots = [(env['fleet'].get(q).state.pose.x, env['fleet'].get(q).state.pose.y) for q in starts
                         if reached[q] < len(latest(jobs[q]).nodes) - 1]
                assert len(spots) == len(set(spots))    # 같은 자리에 두 대가 서지 않는다
        env['coord'].tick_traffic()
        if all(reached[rid] == len(latest(jobs[rid]).nodes) - 1 for rid in starts):
            break
    assert entered == [first, *rest]


def test_autodoor_queue_lines_up_behind_entry_node(env):
    body = dict(base_version=env['version'], name='문', type='autodoor', x=7, y=4, width=2, height=2,
                door_queues=[[dict(x=5.2, y=5), dict(x=4.2, y=5)], []])   # 진입점 A (6.2,5) 뒤로 두 자리
    r = env['client'].post(BASE + '/zones', json=body)
    assert r.status_code == 200, r.text
    door = env['registry'].map_store('hq').load_annotations('f1').zone_by_id(r.json()['zone_id']).door
    assert [(q.x, q.y) for q in door.queue_a] == [(5.2, 5), (4.2, 5)] and door.queue_b == []
    env['registry'].location_store('hq').create('door-east', 'f1', 13, 5, 0)
    for rid, x, y in [('R1', 3, 4), ('R2', 13, 9), ('R3', 2.5, 6)]:
        env['report'](rid, x, y)
    a, oa = dispatch(env, 'R1', 'door-east')
    assert [n.position.x for n in oa.nodes[:4]] == [4.2, 5.2, 6.2, 9.8]
    assert released(a) == [True, True, True, False, False]      # 자동문은 진입점에서 서서 문을 연다
    b, _ = dispatch(env, 'R3', 'door-east')
    assert not any(released(b))
    at(env, a, TAIL)
    at(env, a, WAIT)
    env['coord'].tick_traffic()
    assert released(b) == [True, True, False, False, False]
    assert b.traffic['waiting'] == '대기열 2번째 자리 대기'


def test_route_failure_is_not_retried_every_second(env):
    """경로를 못 만드는 게시물을 매초 모든 로봇으로 다시 계산하면 락을 수 초 잡아 실시간 맵이 끊긴다."""
    from fms_server.task import ROUTE_RETRY_SECONDS, RouteBlocked
    coord, calls = env['coord'], []

    def fail(job_id, robot_id):
        calls.append(robot_id)
        raise RouteBlocked('경로 없음', robot_specific=False)

    coord.dispatch = fail
    job = env['jobs'].create('hq', [dict(type='move', target='east')], None, auto_assign=True)
    t0 = 1_000_000.0
    coord.allocate_pending(t0)
    assert len(calls) == 1 and env['jobs'].get(job.job_id).error == '경로 없음'   # 다른 로봇은 시도하지 않는다
    coord.allocate_pending(t0 + 5)
    assert len(calls) == 1
    coord.allocate_pending(t0 + ROUTE_RETRY_SECONDS + 1)
    assert len(calls) == 2
    r = env['client'].post(BASE + '/zones', json=zone_body(env['version']))   # 맵이 바뀌면 바로 다시 시도
    assert r.status_code == 200, r.text
    coord.allocate_pending(t0 + ROUTE_RETRY_SECONDS + 2)
    assert len(calls) == 3
