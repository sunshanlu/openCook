import argparse
import math
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from .browser import SiteBrowser
from .errors import TaskError
from .storage import state_lock


def state_directory() -> Path:
    return Path.home() / "Library" / "Application Support" / "openCook XHS"


def positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description="小红书点点视频菜谱 → openCook")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("login", "run", "pipeline", "collect", "classify", "extract", "export"):
        command = commands.add_parser(name)
        command.add_argument("--state-dir", type=Path, default=state_directory())
        if name != "login":
            command.add_argument(
                "--links" if name == "run" else "--profiles", type=Path, required=True
            )
            command.add_argument("--output", type=Path, required=True)
            if name in {"run", "pipeline", "extract"}:
                command.add_argument("--refresh", action="store_true")
            if name in {"pipeline", "classify"}:
                command.add_argument("--reclassify", action="store_true")
            if name in {"run", "pipeline", "classify", "extract"}:
                command.add_argument("--chat-batch-size", type=positive_integer, default=5)
                command.add_argument("--max-attempts", type=positive_integer, default=2)
                command.add_argument("--retry-review", action="store_true",
                                     help="人工审核后，重新尝试输入范围内的待审核任务")
            command.add_argument("--timeout", type=positive, default=180)
            command.add_argument("--interval", type=positive, default=2)
            command.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "login":
            with state_lock(args.state_dir), SiteBrowser(args.state_dir / "chrome") as browser:
                browser.login()
            print("登录状态已保存。")
            return 0
        if args.command == "run":
            from .runner import run

            return run(args)
        from .workflow import run_workflow

        return run_workflow(args)
    except (KeyboardInterrupt, EOFError):
        print("\n已停止；已完成记录可在下次运行时复用。", file=sys.stderr)
        return 130
    except (TaskError, PlaywrightError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
