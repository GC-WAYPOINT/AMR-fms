"""점유 존 판정용 거친 격자 최단 경로 (`traffic.ZonePlanner`).

FMS 는 흰 픽셀(주행 가능) 위 최단 경로를 빠르게 구해 그 경로가 지나는 존만 본다. 실제 주행 경로는
로봇이 계획하므로 경로 모양은 대략이면 되지만 연결성(좁은 문이 열려 있는지, 벽 너머로 새지 않는지)은
정밀 격자와 같아야 한다.

- 축소: 로봇 반폭으로 팽창한 정밀 FREE 격자를 factor x factor 블록으로 묶고, 블록 안에 FREE 가 하나라도
  있으면 통과 가능으로 본다. 정밀 격자에서 갈 수 있는 점은 거친 격자에서도 갈 수 있다 (좁은 문 보존).
  벽 두께 + 양쪽 팽창(0.15 + 2 x 0.235 = 0.62 m)이 칸 크기의 2배보다 두꺼우면 블록이 통째로 막힌 줄이
  남아 벽 너머로 새지 않는다. 그래서 칸은 0.25 m 로 둔다.
- 탐색: A* 한 번. 8방향, 대각선은 양 옆 칸이 모두 열려 있어야 한다 (모서리 끼어들기 금지).
"""
from __future__ import annotations

import heapq
import math

import numpy as np

COARSE_CELL = 0.25  # m. 벽 두께 + 팽창의 절반보다 작아야 한다 (모듈 설명)
_SQRT2 = math.sqrt(2.0)


def coarse_factor(resolution: float) -> int:
    return max(1, int(round(COARSE_CELL / resolution)))


def coarsen(fine: np.ndarray, factor: int) -> np.ndarray:
    """블록 안에 True 가 하나라도 있으면 True. 가장자리는 False 로 채워 블록 크기를 맞춘다."""
    if factor == 1:
        return fine.copy()
    h, w = fine.shape
    H, W = -(-h // factor), -(-w // factor)
    padded = np.zeros((H * factor, W * factor), dtype=bool)
    padded[:h, :w] = fine
    return padded.reshape(H, factor, W, factor).any(axis=(1, 3))


def path(free: np.ndarray, start: tuple[int, int], goal: tuple[int, int]) -> list[tuple[int, int]]:
    """free 격자에서 start(칸 x, y)부터 goal 까지 최단 경로 칸 목록 (양 끝 포함). 못 가면 []."""
    h, w = free.shape
    def inside(p):
        return 0 <= p[0] < w and 0 <= p[1] < h and free[p[1], p[0]]
    if not (inside(start) and inside(goal)):
        return []
    # 테두리를 막힌 칸으로 둘러 인덱스 경계 검사를 없앤다.
    W = w + 2
    flat = np.zeros((h + 2, W), dtype=bool)
    flat[1:-1, 1:-1] = free
    flat = flat.ravel().tolist()
    s, g = (start[1] + 1) * W + start[0] + 1, (goal[1] + 1) * W + goal[0] + 1
    gx, gy = g % W, g // W

    def heuristic(i):   # 8방향 격자의 정확한 하한 (octile)
        dx, dy = abs(i % W - gx), abs(i // W - gy)
        return max(dx, dy) + (_SQRT2 - 1) * min(dx, dy)

    cost, prev = {s: 0.0}, {}
    heap = [(heuristic(s), 0.0, s)]
    straight = (1, -1, W, -W)
    diagonal = ((1 + W, 1, W), (1 - W, 1, -W), (-1 + W, -1, W), (-1 - W, -1, -W))
    while heap:
        _, d, i = heapq.heappop(heap)
        if d > cost[i]:
            continue
        if i == g:
            cells = [i]
            while i != s:
                i = prev[i]
                cells.append(i)
            return [(c % W - 1, c // W - 1) for c in reversed(cells)]
        steps = [(i + o, 1.0) for o in straight if flat[i + o]]
        steps += [(i + o, _SQRT2) for o, a, b in diagonal if flat[i + o] and flat[i + a] and flat[i + b]]
        for n, step in steps:
            nd = d + step
            if nd < cost.get(n, math.inf):
                cost[n], prev[n] = nd, i
                heapq.heappush(heap, (nd + heuristic(n), nd, n))
    return []
