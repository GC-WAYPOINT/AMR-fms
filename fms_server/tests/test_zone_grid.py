"""zone_grid: 존 판정용 거친 격자의 연결성·최단 경로."""
import math

import numpy as np

from fms_server import zone_grid


def room_with_wall(door_px):
    """40x20 px 방 두 개 사이 세로 벽(두께 13 px = 0.65 m, 팽창 포함). door_px > 0 이면 가운데 문."""
    free = np.ones((40, 81), dtype=bool)
    free[:, 34:47] = False
    if door_px:
        top = 20 - door_px // 2
        free[top:top + door_px, 34:47] = True
    return free


def length(cells):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(cells, cells[1:]))


def test_narrow_door_stays_open():
    """정밀 격자에서 1 px 폭(팽창 후 남은 좁은 문)만 열려 있어도 거친 격자에서 이어지고 경로가 문을 지난다."""
    coarse = zone_grid.coarsen(room_with_wall(1), 5)
    cells = zone_grid.path(coarse, (1, 1), (15, 1))
    assert cells[0] == (1, 1) and cells[-1] == (15, 1)
    assert (8, 4) in cells     # 문 칸 (y=20 px -> 4)


def test_wall_does_not_leak():
    """벽 + 팽창이 칸 2개보다 두꺼우면 벽 너머로 새지 않는다."""
    coarse = zone_grid.coarsen(room_with_wall(0), 5)
    assert zone_grid.path(coarse, (1, 4), (15, 4)) == []


def test_shortest_and_blocked_goal():
    free = np.ones((10, 10), dtype=bool)
    free[5, 5] = False
    assert length(zone_grid.path(free, (0, 0), (3, 0))) == 3
    assert abs(length(zone_grid.path(free, (0, 0), (3, 3))) - 3 * math.sqrt(2)) < 1e-9
    assert zone_grid.path(free, (0, 0), (5, 5)) == []
    assert zone_grid.path(free, (0, 0), (0, 0)) == [(0, 0)]


def test_path_goes_around_obstacle():
    free = np.ones((9, 9), dtype=bool)
    free[1:9, 4] = False       # 아래가 막힌 세로 벽. 위(y=0)로 돌아가야 한다
    cells = zone_grid.path(free, (0, 8), (8, 8))
    assert (4, 0) in cells and all(free[y, x] for x, y in cells)


def test_no_corner_cutting():
    """대각선은 양 옆 칸이 모두 열려 있어야 한다."""
    free = np.array([[1, 0], [0, 1]], dtype=bool)
    assert zone_grid.path(free, (0, 0), (1, 1)) == []
