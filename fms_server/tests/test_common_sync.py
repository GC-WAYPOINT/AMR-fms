"""이 리포 `common/` (원본) 과 AMR-simulator `viewer/common/` (사본) 이 같은지 검사.

뷰어는 AMR-simulator 리포에 있고 사본을 갖는다. 자동 동기화가 없으므로 원본을 고치고
사본 복사를 잊으면 여기서 걸린다. AMR-simulator 위치는 `AMR_SIM_DIR` 로 바꿀 수 있고
(기본: 이 리포 옆 `../AMR-simulator`), 없으면 건너뛴다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SIM = Path(os.environ.get("AMR_SIM_DIR", REPO.parent / "AMR-simulator"))
FILES = ["schemas.py", "map_model.py", "map_annotations.py"]


@pytest.mark.parametrize("name", FILES)
def test_viewer_copy_matches(name):
    dst_path = SIM / "viewer" / "common" / name
    if not dst_path.exists():
        pytest.skip(f"AMR-simulator 사본 없음: {dst_path} (AMR_SIM_DIR 로 지정)")
    src = (REPO / "common" / name).read_bytes()
    assert src == dst_path.read_bytes(), (
        f"{dst_path} 가 common/{name} 와 다르다 — cp common/{name} {dst_path}"
    )
