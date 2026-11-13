"""Entry point. The jobapp.cmd (Windows) and jobapp.sh (macOS, Linux) scripts call these.

    python -m app                 start Job App and open the dashboard
    python -m app --demo          the same, with sample data kept apart from yours
    python -m app check           test this computer
    python -m app signin          sign in to Claude with your own account or API key
    python -m app where           show where your data is kept
    python -m app import FOLDER   copy your data from an older Job App folder
    python -m app forget          delete everything Job App keeps about you
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import sys
import urllib.error
import urllib.request
from logging.handlers import RotatingFileHandler

from . import __version__
from .paths import Paths, default_paths

DEFAULT_PORT = 8765
DEMO_PORT = 8775


def setup_logging(paths: Paths, verbose: bool = False) -> None:
    paths.logs.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    to_file = RotatingFileHandler(paths.logs / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    to_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(to_file)
    to_console = logging.StreamHandler()
    to_console.setLevel(logging.WARNING)
    to_console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(to_console)


def _job_app_at(port: int, demo: bool) -> bool:
    """Is a Job App instance of the same kind already answering on this port?"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as response:
            info = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return info.get("app") == "job-app" and bool(info.get("demo")) == demo


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def choose_port(preferred: int, demo: bool) -> tuple[int, bool]:
    """Return (port, already_running)."""
    if _job_app_at(preferred, demo):
        return preferred, True
    for port in range(preferred, preferred + 20):
        if port != preferred and _job_app_at(port, demo):
            return port, True
        if _port_free(port):
            return port, False
    raise SystemExit(f"No free port found between {preferred} and {preferred + 19}.")


async def serve(paths: Paths, port: int, open_window: bool) -> None:
    import uvicorn

    from .server.app import create_app, load_or_create_token
    from .window import open_dashboard

    log = logging.getLogger("jobapp")
    token = load_or_create_token(paths.session_token)
    agents = {}
    if paths.demo:
        # Sample-data mode never uses Claude: the agents are stand-ins.
        from .profile.demo import demo_ask

        from .apply.agent import rule_based_agent
        from .search.demo import demo_web_agent, sample_web

        agents = {"ask": demo_ask, "check_sign_in": lambda: True,
                  "search": {"web": sample_web(), "web_agent": demo_web_agent, "headless_factory": None},
                  "apply_agent": rule_based_agent}
    app = create_app(paths, token=token, allowed_hosts={f"127.0.0.1:{port}", f"localhost:{port}"}, **agents)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_config=None, access_log=False, timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started and not task.done():
        await asyncio.sleep(0.05)
    if server.started:
        url = f"http://127.0.0.1:{port}/"
        log.info("Job App %s listening on %s (demo=%s)", __version__, url, paths.demo)
        print(f"Job App is running at {url}")
        print(f"Your data is in {paths.root}")
        print("Keep this window open while you use it. Close it, or press Ctrl+C, to stop.")
        if open_window:
            how = open_dashboard(f"{url}?t={token}")
            log.info("Dashboard opened: %s", how)
            if how == "not opened":
                print(f"Could not open a browser. Open this link yourself: {url}?t={token}")
    await task


def _tidy_transcripts(paths: Paths) -> None:
    """Remove the Claude runtime's transcripts of earlier agent runs, unless you chose to keep them."""
    try:
        from .agents.auth import forget_transcripts
        from .core.settings import load_settings

        if not load_settings(paths.settings).keep_agent_transcripts:
            forget_transcripts(paths.agent_cwd)
    except Exception as exc:  # noqa: BLE001 - never stops the app from starting
        logging.getLogger("jobapp").warning("Could not tidy agent transcripts: %s", exc)


def run(args: argparse.Namespace) -> int:
    paths = default_paths(demo=args.demo)
    paths.ensure()
    setup_logging(paths, args.verbose)
    if not args.demo:
        _tidy_transcripts(paths)
    preferred = args.port or (DEMO_PORT if args.demo else DEFAULT_PORT)
    port, already_running = choose_port(preferred, args.demo)

    if already_running:
        from .server.app import load_or_create_token
        from .window import open_dashboard

        print(f"Job App is already running at http://127.0.0.1:{port}/ . Opening its dashboard.")
        if not args.no_window:
            open_dashboard(f"http://127.0.0.1:{port}/?t={load_or_create_token(paths.session_token)}")
        return 0

    if args.demo:
        from .core.demo import sample_jobs
        from .core.store import TrackerStore

        from .profile.demo import seed_demo_profile

        import shutil

        # Sample-data mode starts fresh each time: no applications or answers from an earlier sample session.
        shutil.rmtree(paths.applications, ignore_errors=True)
        for leftover in (paths.answers, paths.data / "schedule.json"):
            leftover.unlink(missing_ok=True)
        TrackerStore(paths.tracker).replace_all(sample_jobs(practice_base=f"http://127.0.0.1:{port}/practice"))
        seed_demo_profile(paths, with_resume=True)
        print("Sample data mode: everything shown is made up, your real files are not touched,")
        print("and the agents are stand-ins that do not use Claude.")

    try:
        asyncio.run(serve(paths, port, open_window=not args.no_window))
    except KeyboardInterrupt:
        pass
    if not args.demo:
        _tidy_transcripts(paths)
    print("Job App stopped.")
    return 0


def main(argv: list[str] | None = None) -> int:
    # A character the Windows console cannot show should never stop the app.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog="python -m app", description="Job App")
    parser.add_argument("--version", action="version", version=f"Job App {__version__}")
    parser.add_argument("--demo", action="store_true", help="use sample data kept apart from your tracker")
    parser.add_argument("--port", type=int, default=0, help=f"port to listen on (default {DEFAULT_PORT})")
    parser.add_argument("--no-window", action="store_true", help="do not open the dashboard window")
    parser.add_argument("--verbose", action="store_true", help="write more detail to logs/app.log in your data folder")
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", help="test that this computer can run every part of Job App")
    check.add_argument("--quick", action="store_true", help="only look at what is installed; start nothing")
    check.add_argument("--skip-browser", action="store_true", help="do not test the dedicated browser")
    check.add_argument("--skip-agent", action="store_true", help="do not test the Claude agents")
    check.add_argument("--skip-autofill", action="store_true",
                       help="do not let the application agent fill the practice form (saves some plan usage)")
    check.add_argument("--headless", action="store_true", help="run the browser test without a visible window")

    signin = sub.add_parser("signin", help="sign in to Claude with your own Claude account or API key")
    how = signin.add_mutually_exclusive_group()
    how.add_argument("--plan", action="store_const", const="plan", dest="choice",
                     help="use your Claude account (Pro or Max plan)")
    how.add_argument("--api-key", action="store_const", const="key", dest="choice",
                     help="use your own Anthropic API key")
    how.add_argument("--sign-out", action="store_true", help="remove the stored API key and the Claude sign-in")
    how.add_argument("--status", action="store_true", help="only show how the agents sign in now")

    sub.add_parser("where", help="show where your data is kept")
    importer = sub.add_parser("import", help="copy profile, data and settings from an older Job App folder")
    forget = sub.add_parser("forget", help="delete everything Job App keeps about you on this computer")
    forget.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    importer.add_argument("folder", help="the old Job App folder (the one that has profile, data and config in it)")

    args = parser.parse_args(argv)
    if args.command == "check":
        from .checks.runner import run_checks

        return run_checks(args)
    if args.command == "signin":
        from .checks.signin import run_signin

        return run_signin(choice=args.choice or "", sign_out=args.sign_out, status_only=args.status)
    if args.command == "where":
        paths = default_paths()
        print(f"Program: {paths.program}")
        print(f"Your data: {paths.root}")
        print("Set JOBAPP_HOME to keep your data somewhere else.")
        return 0
    if args.command == "import":
        from .core.migrate import import_old_folder

        return import_old_folder(args.folder)
    if args.command == "forget":
        from .core.migrate import forget_everything

        if _job_app_at(DEFAULT_PORT, False):
            print("Job App is running. Close it first (Ctrl+C in its terminal window), then run this again.")
            return 1
        return forget_everything(assume_yes=args.yes)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
