"""Worker 运维命令行：启动 Worker、执行恢复、单次推进某个 run。

用法：
    python -m app.worker.cli worker --processes 2 --threads 4
    python -m app.worker.cli recover
    python -m app.worker.cli run <run_id>
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time

from app.agent.orchestrator import execute_run
from app.agent.providers import build_provider_set
from app.agent.recovery import recover_runs
from app.config import get_settings
from app.db import session_factory_for
from app.observability import configure_logging

logger = logging.getLogger("app.worker.cli")


def _recovery_loop(interval_seconds: int) -> None:
    factory = session_factory_for(get_settings())
    while True:
        try:
            recover_runs(factory, get_settings())
        except Exception as exc:  # noqa: BLE001 - 恢复线程不能因单次失败退出
            logger.warning("recovery_loop_error", extra={"error_type": type(exc).__name__})
        time.sleep(interval_seconds)


def command_worker(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.recover_on_start:
        factory = session_factory_for(settings)
        result = recover_runs(factory, settings)
        print(json.dumps({"startup_recovery": result}, ensure_ascii=False))

    thread = threading.Thread(
        target=_recovery_loop,
        args=(settings.worker_recovery_interval_seconds,),
        name="recovery-loop",
        daemon=True,
    )
    thread.start()

    # dramatiq 1.17 的 main() 接受已解析的 Namespace（不是原始 argv），
    # 因此这里先用它自己的 parser 解析参数，避免 AttributeError: 'list' object has no attribute 'path'。
    from dramatiq.cli import main as dramatiq_main
    from dramatiq.cli import make_argument_parser

    argv = ["app.worker.actors", "--processes", str(args.processes), "--threads", str(args.threads)]
    if args.verbose:
        argv.append("--verbose")
    parsed = make_argument_parser().parse_args(argv)
    return int(dramatiq_main(parsed) or 0)


def command_recover(_: argparse.Namespace) -> int:
    settings = get_settings()
    factory = session_factory_for(settings)
    result = recover_runs(factory, settings)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def command_run(args: argparse.Namespace) -> int:
    settings = get_settings()
    factory = session_factory_for(settings)
    providers = build_provider_set(settings)
    status = execute_run(factory, args.run_id, providers=providers, settings=settings)
    print(json.dumps({"run_id": args.run_id, "status": status, "provider_mode": providers.mode}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.worker.cli", description="AI-Guide Worker 运维命令")
    sub = parser.add_subparsers(dest="command", required=True)

    worker = sub.add_parser("worker", help="启动 Dramatiq Worker（含周期恢复线程）")
    worker.add_argument("--processes", type=int, default=1)
    worker.add_argument("--threads", type=int, default=4)
    worker.add_argument("--verbose", action="store_true")
    worker.add_argument("--no-recover-on-start", dest="recover_on_start", action="store_false", default=True)
    worker.set_defaults(func=command_worker)

    recover = sub.add_parser("recover", help="重新投递 QUEUED 与租约过期的 RUNNING")
    recover.set_defaults(func=command_recover)

    run = sub.add_parser("run", help="在本进程内单次推进指定 run（排障用）")
    run.add_argument("run_id")
    run.set_defaults(func=command_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
