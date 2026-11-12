#!/usr/bin/env python3
"""部署前自检：编译、静态检查（F/E9）、测试函数重名、pytest。

用法：
    python scripts/preflight.py            # 编译 + 静态检查 + 重名检查
    python scripts/preflight.py --with-tests
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILED: list[str] = []


def _run(cmd: list[str], title: str) -> None:
    print(f"== {title}")
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        FAILED.append(title)
        print(output[:4000])
    else:
        print(output[-1500:] if output else "ok")


def check_compile() -> None:
    _run([sys.executable, "-m", "compileall", "-q", "app", "tests", "scripts"], "编译检查")


def check_lint() -> None:
    probe = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"], cwd=ROOT, capture_output=True, text=True
    )
    if probe.returncode != 0:
        print("== 静态检查\n跳过：未安装 ruff（pip install ruff）")
        return
    _run(
        [sys.executable, "-m", "ruff", "check", "app", "tests", "scripts", "--select", "F,E9",
         "--output-format", "concise"],
        "静态检查 F/E9",
    )


def check_duplicate_tests() -> None:
    print("== 测试函数重名检查")
    path = ROOT / "tests" / "test_core.py"
    if not path.exists():
        print("跳过：未找到 tests/test_core.py")
        return
    tree = ast.parse(path.read_text(encoding="utf-8"))
    seen: dict[str, int] = {}
    dups: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            seen[node.name] = seen.get(node.name, 0) + 1
    dups = [name for name, count in seen.items() if count > 1]
    if dups:
        FAILED.append("测试函数重名")
        print("重名测试：" + ", ".join(dups))
    else:
        print(f"ok（{len(seen)} 个测试函数，无重名）")


def run_tests() -> None:
    _run([sys.executable, "-m", "pytest", "-q", "tests/test_core.py"], "pytest")


def main() -> int:
    check_compile()
    check_lint()
    check_duplicate_tests()
    if "--with-tests" in sys.argv:
        run_tests()
    if FAILED:
        print("\n自检失败：" + "、".join(FAILED))
        return 1
    print("\n自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
