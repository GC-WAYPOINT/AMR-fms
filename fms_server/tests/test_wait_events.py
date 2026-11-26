"""존 대기 기록(학습 라벨 원본)과 배정 스냅샷 검증.

설계서 3.2·3.3: 존 대기 시간 = 대기 node 에 멈춘 시각 -> 진입 허가 시각. 주행 구간(approach)과 구분하고,
대기 이유·존·gate 가 바뀔 때마다 구간을 나눠 남긴다. job.assigned 에는 배정 순간의 교통 상태를 남긴다.
"""
import time

from fms_server.tests.test_traffic import add_zone, dispatch, env, latest  # noqa: F401  (env fixture 재사용)


class Recorder:
    def __init__(self):
        self.rows = []

    def record(self, kind, **fields):
        fields.setdefault("ts", time.time())
        self.rows.append(dict(fields, kind=kind))

    def of(self, kind, job=None):
        return [r for r in self.rows if r["kind"] == kind and (job is None or r.get("job_id") == job)]


def attach(e):
    rec = Recorder()
    e["coord"]._events = rec
    e["coord"].traffic.events = rec
    return rec


def test_first_wait_is_recorded_and_approach_is_separated(env):
    zone = add_zone(env)
    rec = attach(env)
    a, oa = dispatch(env, "R1", "east")
    first = rec.of("traffic.wait", a.job_id)
    # 예전에는 prepare 가 대기 이유를 이벤트 없이 넣어서 첫 대기 시작이 남지 않았다.
    assert first and first[0]["phase"] == "approach"
    assert first[0]["zone_id"] == zone["zone_id"] and first[0]["gate_id"] == "west"

    env["report"]("R1", 5, 3, oa, node=0)          # 대기 node 에 멈춤, 존 비어 있음 -> 바로 허가
    arrived = rec.of("traffic.at_wait", a.job_id)
    assert len(arrived) == 1 and arrived[0]["zone_busy"] is False
    grant = [r for r in rec.of("traffic.reserve", a.job_id) if r["lease_kind"] == "zone"]
    assert len(grant) == 1 and grant[0]["queued_s"] is not None and grant[0]["queued_s"] < 0.5
    ends = rec.of("traffic.wait_end", a.job_id)
    assert [e["phase"] for e in ends] == ["approach"] and ends[0]["outcome"] == "clear"


def test_queue_wait_measures_only_time_stopped_at_wait_node(env):
    add_zone(env)
    rec = attach(env)
    a, oa = dispatch(env, "R1", "east")
    b, ob = dispatch(env, "R2", "west")
    env["report"]("R1", 5, 3, oa, node=0)          # R1 이 존 진입 허가
    env["report"]("R2", 11, 7, ob, node=0)         # R2 는 반대편 대기 node 에서 기다림
    waits = rec.of("traffic.wait", b.job_id)
    assert [w["phase"] for w in waits] == ["approach", "at_wait"]
    assert rec.of("traffic.at_wait", b.job_id)[0]["zone_busy"] is True
    time.sleep(0.3)
    env["report"]("R1", 11, 3, latest(a), node=1)  # R1 출구 통과 -> 존 해제 -> R2 허가
    assert latest(b).nodes[1].released
    release = [r for r in rec.of("traffic.release", a.job_id) if r["lease_kind"] == "zone"]
    assert release and release[0]["held_s"] >= 0.3
    grant = [r for r in rec.of("traffic.reserve", b.job_id) if r["lease_kind"] == "zone"][0]
    end = [e for e in rec.of("traffic.wait_end", b.job_id) if e["phase"] == "at_wait"][0]
    assert grant["queued_s"] >= 0.3
    assert abs(end["seconds"] - grant["queued_s"]) < 0.1   # 두 값은 같은 구간을 잰다 (대조용)
    assert end["outcome"] == "clear"


def test_finish_closes_open_wait_segment(env):
    add_zone(env)
    rec = attach(env)
    a, oa = dispatch(env, "R1", "east")
    b, ob = dispatch(env, "R2", "west")
    env["report"]("R1", 5, 3, oa, node=0)
    env["report"]("R2", 11, 7, ob, node=0)
    env["coord"].cancel(b.job_id)
    env["report"]("R2", 11, 7)                     # 취소 확인 (IDLE·정지)
    assert b.status.value == "canceled"
    end = [e for e in rec.of("traffic.wait_end", b.job_id) if e["phase"] == "at_wait"]
    assert end and end[0]["outcome"] == "canceled"
    assert "wait_seg" not in b.traffic


def test_assign_snapshot_counts_other_jobs_before_own_reservation(env):
    zone = add_zone(env)
    rec = attach(env)
    a, oa = dispatch(env, "R1", "east")
    snap_a = rec.of("job.assigned", a.job_id)[0]["snapshot"]
    assert snap_a["zones"][0]["reserved_ahead"] == 0          # 자기 자신은 세지 않는다
    assert snap_a["zones"][0]["occupied"] is False
    assert snap_a["soc"] == 90.0 and snap_a["robots"] == 3

    env["report"]("R1", 5, 3, oa, node=0)                     # R1 존 점유
    b, ob = dispatch(env, "R2", "west")
    zb = rec.of("job.assigned", b.job_id)[0]["snapshot"]["zones"][0]
    assert zb["zone_id"] == zone["zone_id"] and zb["gate_id"] == "east"
    assert zb["occupied"] is True and zb["occ_elapsed_s"] >= 0
    assert zb["reserved_ahead"] == 1 and zb["queue_len"] == 0
    assert zb["entries_1m"] == 1

    env["report"]("R2", 11, 7, ob, node=0)                    # R2 반대편 대기 node 에서 대기
    c = env["jobs"].create("hq", [dict(type="move", target="east")], "R3")
    env["coord"].dispatch(c.job_id)
    zc = rec.of("job.assigned", c.job_id)[0]["snapshot"]["zones"][0]
    assert zc["gate_id"] == "west"
    assert zc["queue_len"] == 1 and zc["queue_len_dir"] == 0  # 대기 중인 R2 는 반대 방향
    assert zc["reserved_ahead"] == 2
