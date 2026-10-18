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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="小红书点点视频菜谱 → openCook")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("login", "run", "pipeline", "collect", "classify", "extract", "export"):
        command = commands.add_parser(name)
        command.add_argument("--state-dir", type=Path, default=state_directory())
        if name == "login":
            command.add_argument("--worker", choices=("classify", "extract"), default="classify")
        if name != "login":
            command.add_argument(
                "--links" if name == "run" else "--profiles", type=Path, required=True
            )
            command.add_argument("--output", type=Path, required=True)
            if name in {"run", "pipeline", "extract"}:
                command.add_argument("--refresh", action="store_true")
            if name in {"pipeline", "classify"}:
                command.add_argument("--reclassify", action="store_true")
            if name in {"pipeline", "collect"}:
                command.add_argument("--full-collect", action="store_true",
                                     help="忽略增量，重滚博主全部历史并对账")
            if name == "pipeline":
                command.add_argument("--access-per-hour", type=positive_integer)
                command.add_argument("--send-per-hour", type=positive_integer)
                command.add_argument("--active-hours", type=positive,
                                     help="兼容旧任务；累计时长上限已由北京时间07:00–00:00窗口替代")
                command.add_argument(
                    "--execution", choices=("serial", "parallel"), default="serial"
                )
                command.add_argument("--skip-collect", action="store_true",
                                     help="从已采集清单继续分类、提取和导出")
            if name in {"run", "pipeline", "classify", "extract"}:
                command.add_argument("--chat-batch-size", type=positive_integer, default=5)
                command.add_argument("--max-attempts", type=positive_integer, default=2)
                command.add_argument("--task-engine", choices=("legacy", "v2"), default="legacy",
                                     help="显式选择新任务执行器；旧未决请求不自动迁移")
                command.add_argument("--retry-review", action="store_true",
                                     help="人工审核后，重新尝试输入范围内的待审核任务")
            command.add_argument("--timeout", type=positive, default=180)
            command.add_argument("--interval", type=positive, default=2)
            command.add_argument("--non-interactive", action="store_true")
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in {"supervise", "status", "pause", "resume", "stop"}:
        from .supervisor import command
        return command(argv, state_directory(), parser=parser)
    args = parser.parse_args(argv)
    from .runtime import start_worker
    runtime = start_worker()
    code, error = 1, None
    try:
        if args.command == "login":
            from .accounts import profile_path, save_binding
            with state_lock(args.state_dir), SiteBrowser(
                profile_path(args.state_dir, args.worker)
            ) as browser:
                browser.login()
                identity = browser.account_identity(browser.page)
                save_binding(args.state_dir, args.worker, identity)
            print(f"{args.worker} 登录状态已保存，用户ID：{identity}。")
            code = 0
            return code
        if args.command == "run":
            from .runner import run

            code = run(args)
            return code
        from .workflow import run_workflow

        code = run_workflow(args)
        return code
    except (KeyboardInterrupt, EOFError):
        print("\n已停止；已完成记录可在下次运行时复用。", file=sys.stderr)
        code = 130
        return code
    except (TaskError, PlaywrightError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        error = str(exc)
        return code
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if runtime:
            runtime.finish(code, error)


if __name__ == "__main__":
    sys.exit(main())
