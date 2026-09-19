"""一键跑全套测试。

    python run_tests.py

三个测试套件都是**离线可跑**的（不需要 API Key、不访问外网）：

    smoke_test.py     核心链路（17 项）——Loop/上下文/权限/工具/技能/配置
    test_streaming.py 流式解析（11 项）——SSE 分片、跨分片工具调用装配
    test_rpc.py       端到端（10 项）——真子进程 + 真 HTTP + 真 SSE

test_rpc 会真的 spawn 子进程并开本地端口，比前两个慢一个量级，属于正常。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

# 项目目录：三个套件脚本都挂在它下面的 tests/ 里。
# 注意 maodingcode 包的导入根是它的**上一级**（4-code/），各套件自己会处理 sys.path。
ROOT = Path(__file__).resolve().parent

SUITES = [
    ("核心链路", "tests/smoke_test.py"),
    ("流式解析", "tests/test_streaming.py"),
    ("RPC 端到端", "tests/test_rpc.py"),
]


def main() -> int:
    print("MaoDingCode 测试套件")
    print("=" * 56)
    failures: list[str] = []
    total_seconds = 0.0

    for label, script in SUITES:
        path = ROOT / script
        if not path.is_file():
            print(f"[跳过] {label}：找不到 {script}")
            failures.append(label)
            continue

        started = time.time()
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.time() - started
        total_seconds += elapsed

        ok = proc.returncode == 0
        mark = "通过" if ok else "失败"
        print(f"[{mark}] {label:<12} {script:<26} {elapsed:5.2f}s")
        if not ok:
            failures.append(label)
            # 失败时把尾部输出打出来，省得再单独跑一次
            tail = (proc.stdout or "").strip().splitlines()[-20:]
            print("-" * 56)
            for line in tail:
                print(f"    {line}")
            stderr_tail = (proc.stderr or "").strip().splitlines()[-10:]
            for line in stderr_tail:
                print(f"    [stderr] {line}")
            print("-" * 56)

    print("=" * 56)
    if failures:
        print(f"有 {len(failures)} 个套件失败：{', '.join(failures)}")
        return 1
    print(f"全部套件通过 ✅  总耗时 {total_seconds:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
