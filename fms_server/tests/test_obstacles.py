"""맵 전역 장애물 레지스트리와 막힌 통로 재경로."""
import time

from common.schemas import InstantActionType, NodeProgress, ObstacleInfo, Order
from fms_server.tests.test_traffic import BASE, env, dispatch, latest, pose  # noqa: F401  (fixture 재사용)

UID = 'hq/f1'


def two_gate_zone(e):
    """같은 방향 gate 가 둘인 존: y=3 / y=7 통로. 한쪽이 막히면 다른 쪽으로 간다."""
    body = dict(base_version=e['version'], name='칸막이', type='occupancy', x=7, y=0, width=2, height=10,
                gates=[dict(gate_id='lower', wait=pose(5, 3), exit=pose(11, 3)),
                       dict(gate_id='upper', wait=pose(5, 7), exit=pose(11, 7))])
    r = e['client'].post(BASE + '/zones', json=body)
    assert r.status_code == 200, r.text
    for rid, x, y in [('R1', 3, 3), ('R2', 13, 7), ('R3', 3, 1)]:
        e['report'](rid, x, y)


def seen(x, y, size=0.1):
    return ObstacleInfo(x=x, y=y, width=size, height=size)


def see(e, rid, x, y, *obstacles):
    e['report'](rid, x, y, detected_obstacles=list(obstacles))


def registry_of(e):
    return e['coord'].traffic.obstacles


def test_obstacle_needs_confirmation_then_expires(env):
    reg, t0 = registry_of(env), time.time()
    see(env, 'R1', 3, 3, seen(3.6, 3))
    reg.update(t0)
    assert reg.rects('hq', 'f1') == []          # 처음 본 순간에는 올리지 않는다
    see(env, 'R1', 3, 3, seen(3.6, 3))
    reg.update(t0 + 30.1)
    assert reg.rects('hq', 'f1') == [(3.6, 3, 0.1, 0.1)]
    see(env, 'R1', 3, 3, seen(3.6, 3))
    reg.update(t0 + 100)                         # 계속 보이면 유지 기간을 다시 센다
    # R1 이 멀어져 더 보고하지 않아도 (판단 거리 밖) 5분 동안은 남는다.
    see(env, 'R1', 3, 8)
    reg.update(t0 + 100 + 299)
    assert len(reg.rects('hq', 'f1')) == 1
    reg.update(t0 + 100 + 301)
    assert reg.rects('hq', 'f1') == []


def test_moving_obstacle_and_other_robot_body_are_not_registered(env):
    reg, t0 = registry_of(env), time.time()
    for i in range(6):                           # 움직이는 장애물: 칸이 계속 바뀐다
        see(env, 'R1', 3, 3, seen(3.5 + 0.2 * i, 3))
        reg.update(t0 + i)
    # R3 의 몸체가 장애물로 보고된 경우 (로봇 간 인식)
    for i in range(6):
        see(env, 'R1', 3, 3, seen(3, 1))
        reg.update(t0 + 10 + i)
    assert reg.rects('hq', 'f1') == []


def test_other_robot_passing_without_seeing_it_clears(env):
    reg, t0 = registry_of(env), time.time()
    for dt in (0, 30.1):
        see(env, 'R1', 3, 3, seen(3.6, 3))
        reg.update(t0 + dt)
    assert reg.rects('hq', 'f1')
    see(env, 'R3', 3.0, 1.0)                      # 멀다 (2 m): 판단하지 않는다
    see(env, 'R1', 3, 3, seen(3.6, 3))
    reg.update(t0 + 4)
    assert reg.rects('hq', 'f1')
    see(env, 'R3', 3.4, 3.2)                      # 0.4 m 안인데 장애물이 안 보인다 -> 치워진 것
    reg.update(t0 + 5)
    assert reg.rects('hq', 'f1') == []


def test_registered_obstacle_steers_route_to_other_gate(env):
    two_gate_zone(env)
    base = dispatch(env, 'R1', 'east')[0]
    assert base.traffic['crossings'][0]['gate'] == 'lower'
    env['coord'].cancel(base.job_id)
    reg, t0 = registry_of(env), time.time()
    for dt in (0, 30.1):                           # 아래 통로 대기 node(5,3) 를 막는다
        see(env, 'R3', 4.4, 3, seen(5, 3, 0.4))
        reg.update(t0 + dt)
    assert reg.rects('hq', 'f1')
    env['report']('R1', 3, 3)
    job2 = env['jobs'].create('hq', [dict(type='move', target='east')], 'R3')
    env['report']('R3', 3, 1)
    env['coord'].dispatch(job2.job_id)
    assert job2.traffic['crossings'][0]['gate'] == 'upper'
    snap = env['coord'].traffic.snapshot('hq', 'f1')
    assert snap['obstacles'] and snap['obstacles'][0]['expires_in'] > 0


def remaining(order, released=None):
    """로봇이 보고하는 "아직 도달하지 않은 노드" (오더의 모든 노드)."""
    return [NodeProgress(node_id=n.node_id, sequence_id=n.sequence_id,
                         released=n.released if released is None else released) for n in order.nodes]


def blocked_at_lower_wait(e):
    """R1 이 아래 통로 대기 node 로 가다가 앞이 막혀 서 있다 (허가된 노드가 남아 있는데 WAITING)."""
    two_gate_zone(e)
    job = e['jobs'].create('hq', [dict(type='move', target='east')], 'R1')
    e['coord'].dispatch(job.job_id)
    first = latest(job)
    assert job.traffic['crossings'][0]['gate'] == 'lower'
    e['report']('R1', 3, 3, first, node_states=remaining(first))
    assert e['jobs'].get(job.job_id).status.value == 'working'
    return job, first


def test_blocked_robot_is_rerouted_through_other_gate(env):
    job, first = blocked_at_lower_wait(env)
    traffic, reg, t0 = env['coord'].traffic, registry_of(env), time.time()
    for dt in (0, 30.1):                           # 아래 통로 대기 node(5,3) 를 막는다
        see(env, 'R3', 3, 1, seen(5, 3, 0.4))
        reg.update(t0 + dt)
    # R1 도 앞의 장애물을 보고 있다 (재경로 조건: 자기 센서로 보는 등록 장애물이 남은 경로를 막음)
    env['report']('R1', 3, 3, first, node_states=remaining(first), detected_obstacles=[seen(5, 3, 0.4)])
    traffic.analyze(t0)
    env['coord'].tick_reroute(t0 + 5)
    assert 'rerouting' not in job.traffic          # 15 초 전에는 기다린다
    env['coord'].tick_reroute(t0 + 20)
    assert [g[1] for g in job.traffic['rerouting']['gates']] == ['upper']
    assert env['mqtt'].instants[-1].action_type == InstantActionType.CANCEL_ORDER
    # 취소가 확인되기 전에는 낡은 오더에 허가를 더 내보내지 않는다
    published = len(env['mqtt'].orders)
    env['coord'].tick_traffic()
    assert len(env['mqtt'].orders) == published
    # 로봇이 취소를 받고 IDLE 이 되면 같은 작업을 새 order_id 로 다시 보낸다
    job.traffic['rerouting']['at'] = time.time() - 1
    env['report']('R1', 3, 3)
    assert job.order_id == f'{job.job_id}-r1'
    assert job.status.value == 'assigned'
    assert [c['gate'] for c in job.traffic['crossings']] == ['upper']
    assert Order.model_validate(job.order_snapshot).order_id == job.order_id
    assert job.reroutes == 1 and job.resume_from == 0 and 'rerouting' not in job.traffic


def test_no_reroute_when_no_other_route(env):
    job, first = blocked_at_lower_wait(env)
    traffic, t0 = env['coord'].traffic, time.time()
    traffic.analyze(t0)
    env['coord'].tick_reroute(t0 + 20)             # 장애물이 없으니 경로가 같다
    assert 'rerouting' not in job.traffic and job.reroutes == 0


def test_robot_held_by_fms_is_not_rerouted(env):
    job, first = blocked_at_lower_wait(env)
    traffic, reg, t0 = env['coord'].traffic, registry_of(env), time.time()
    for dt in (0, 30.1):
        see(env, 'R3', 3, 1, seen(5, 3, 0.4))
        reg.update(t0 + dt)
    env['report']('R1', 3, 3, first, node_states=remaining(first, released=False))
    traffic.analyze(t0)
    env['coord'].tick_reroute(t0 + 20)             # 허가가 없어서 서 있는 것: 장애물 탓이 아니다
    assert 'rerouting' not in job.traffic


def test_reroute_resumes_after_finished_commands(env):
    two_gate_zone(env)
    job = env['jobs'].create('hq', [dict(type='move', target='west'), dict(type='move', target='east')], 'R1')
    job.resume_from = 1
    env['coord'].dispatch(job.job_id)
    order = latest(job)
    assert [n.node_id for n in order.nodes if n.node_id.startswith(job.job_id + '-n')] == [f'{job.job_id}-n1']
    assert job.done_commands == 1


def test_stale_last_node_from_previous_order_is_not_progress(env):
    two_gate_zone(env)
    job = env['jobs'].create('hq', [dict(type='move', target='west')], 'R1')
    env['coord'].dispatch(job.job_id)
    order = latest(job)
    # 새 오더의 첫 노드에 닿기 전: last_node_* 는 이전 오더의 값이다
    env['report']('R1', 3, 3, order, last_node_id='old-order-n0', last_node_sequence_id=5,
                  node_states=remaining(order))
    assert env['jobs'].get(job.job_id).status.value == 'working'
    assert job.done_commands == 0


def assert_route_would_change(e, job):
    """예전 조건(등록 장애물 + 경로 선택이 바뀜)이었다면 재경로했을 상황인지 확인한다."""
    from common.schemas import Pose
    gates = e['coord'].traffic.preview_gates(job, 'f1', e['fleet'].get('R1').state, [Pose(x=13, y=3, theta=0)],
                                             with_obstacles=True)
    assert [g for _, g in gates] == ['upper']


def register(e, *obstacles):
    """R3 이 30 초 넘게 보고해 장애물을 등록한다."""
    reg, t0 = registry_of(e), time.time()
    for dt in (0, 30.1):
        see(e, 'R3', 3, 1, *obstacles)
        reg.update(t0 + dt)
    return t0


def test_robot_not_seeing_registered_obstacle_is_not_rerouted(env):
    """로봇끼리 막힘·허가를 받고도 선 로봇(이상 주입 stop_after_release): 자기 앞 장애물을 보지 않는다."""
    job, first = blocked_at_lower_wait(env)
    t0 = register(env, seen(5, 3, 0.4))            # 다른 로봇이 등록한 장애물이 아래 통로에 있다
    traffic = env['coord'].traffic
    assert_route_would_change(env, job)
    traffic.analyze(t0)
    env['coord'].tick_reroute(t0 + 20)
    assert 'rerouting' not in job.traffic and job.reroutes == 0


def test_seen_obstacle_off_path_does_not_trigger_reroute(env):
    """경로 선택을 바꾸는 장애물이 있어도, 로봇이 보는 장애물이 자기 길 밖이면 재경로하지 않는다."""
    job, first = blocked_at_lower_wait(env)
    t0 = register(env, seen(5, 3, 0.4), seen(3, 5, 0.4))
    # R1 은 옆(3,5)의 장애물만 본다. 길(y=3)에서 1.8 m 떨어져 차체에 닿지 않는다.
    env['report']('R1', 3, 3, first, node_states=remaining(first), detected_obstacles=[seen(3, 5, 0.4)])
    traffic = env['coord'].traffic
    assert_route_would_change(env, job)
    traffic.analyze(t0)
    env['coord'].tick_reroute(t0 + 20)
    assert 'rerouting' not in job.traffic and job.reroutes == 0


def test_seen_obstacle_beyond_reach_does_not_trigger_reroute(env):
    """길 위에 있어도 로봇 앞 3 m 밖이면 이 로봇이 그것 때문에 선 것이 아니다."""
    job, first = blocked_at_lower_wait(env)
    t0 = register(env, seen(5, 3, 0.4))
    # R1 이 대기 node 5 m 앞(0,3)에 서 있다 — 경로 위 장애물이지만 3 m 밖
    env['report']('R1', 0, 3, first, node_states=remaining(first), detected_obstacles=[seen(5, 3, 0.4)])
    traffic = env['coord'].traffic
    traffic.analyze(t0)
    env['coord'].tick_reroute(t0 + 20)
    assert 'rerouting' not in job.traffic and job.reroutes == 0
