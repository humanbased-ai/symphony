from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping, TextIO


INIT_TUTORIAL_ID = "init-orientation"
INIT_TUTORIAL_VERSION = "2"
STARTER_MISSION_ID = "hello-world-mission"
JAZZBAND_OPENAI_BLOG_URL = "https://openai.com/index/open-source-codex-orchestration-symphony/"
DEFAULT_CONFIG_DIR = ".config/jazzband"
DEFAULT_TUTORIAL_HISTORY_FILE = "tutorials.json"

InputFunc = Callable[[str], str]
OutputFunc = Callable[[str], None]


@dataclass(frozen=True)
class TutorialPage:
    question: str
    answer: tuple[str, ...]


def default_tutorial_history_path(environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    configured_home = _non_empty(env.get("XDG_CONFIG_HOME"))
    if configured_home is not None:
        return Path(configured_home).expanduser() / "jazzband" / DEFAULT_TUTORIAL_HISTORY_FILE
    return Path.home() / DEFAULT_CONFIG_DIR / DEFAULT_TUTORIAL_HISTORY_FILE


def run_init_tutorial_once(
    *,
    force: bool = False,
    history_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    input_func: InputFunc | None = None,
    output_func: OutputFunc | None = None,
    input_stream: TextIO | None = None,
) -> bool:
    if input_func is None:
        stream = input_stream if input_stream is not None else sys.stdin
        if not stream.isatty():
            return False

    if not force and not should_show_tutorial(INIT_TUTORIAL_ID, INIT_TUTORIAL_VERSION, path=history_path, environ=environ):
        return False

    language = prompt_tutorial_language(
        version=INIT_TUTORIAL_VERSION,
        input_func=input_func,
        output_func=output_func,
    )
    completed = print_init_tutorial(language, input_func=input_func, output_func=output_func)
    if completed:
        record_tutorial_seen(
            INIT_TUTORIAL_ID,
            INIT_TUTORIAL_VERSION,
            language=language,
            path=history_path,
            environ=environ,
        )
    return True


def should_show_tutorial(
    tutorial_id: str,
    version: str,
    *,
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    record = _load_tutorial_record(tutorial_id, path=path, environ=environ)
    return record.get("version") != version


def record_tutorial_seen(
    tutorial_id: str,
    version: str,
    *,
    language: str,
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    history_path = _resolve_history_path(path, environ)
    payload = _load_history(history_path)
    tutorials = payload.setdefault("tutorials", {})
    if not isinstance(tutorials, dict):
        tutorials = {}
        payload["tutorials"] = tutorials

    tutorials[tutorial_id] = {
        "version": version,
        "language": language,
        "seen_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    _write_history(history_path, payload)
    return history_path


def should_show_starter_mission(*, path: str | Path | None = None, environ: Mapping[str, str] | None = None) -> bool:
    record = _load_tutorial_record(STARTER_MISSION_ID, path=path, environ=environ)
    return not record.get("done")


def record_starter_mission_done(*, path: str | Path | None = None, environ: Mapping[str, str] | None = None) -> None:
    history_path = _resolve_history_path(path, environ)
    payload = _load_history(history_path)
    tutorials = payload.setdefault("tutorials", {})
    if not isinstance(tutorials, dict):
        tutorials = {}
        payload["tutorials"] = tutorials
    tutorials[STARTER_MISSION_ID] = {
        "done": True,
        "seen_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    _write_history(history_path, payload)


def prompt_tutorial_language(
    *,
    version: str,
    input_func: InputFunc | None = None,
    output_func: OutputFunc | None = None,
) -> str:
    read = input_func if input_func is not None else input
    write = output_func if output_func is not None else print

    write(f"Choose orientation language / 请选择教程语言 (tutorial v{version}):")
    write("  1. English")
    write("  2. 简体中文")
    choice = read("Language [1]: ").strip()
    if choice in {"2", "zh", "zh-cn", "zh_CN", "chinese", "中文", "简体中文"}:
        return "zh-cn"
    if choice not in {"", "1", "en", "english"}:
        write("Unrecognized input — defaulting to English.")
    return "en"


def print_init_tutorial(
    language: str = "en",
    *,
    input_func: InputFunc | None = None,
    output_func: OutputFunc | None = None,
) -> bool:
    write = output_func if output_func is not None else print
    read = input_func if input_func is not None else input
    pages = tutorial_pages(language)
    write(_welcome_line(language))
    for index, page in enumerate(pages, start=1):
        write("")
        write(f"[{index}/{len(pages)}] {page.question}")
        for line in page.answer:
            write(f"    {line}" if line else "")
        if index == len(pages):
            write("")
            continue

        choice = read(_continue_prompt(language)).strip().lower()
        if choice in {"s", "skip", "q", "quit", "跳过"}:
            write(_skipped_line(language))
            return False
    return True


def tutorial_pages(language: str = "en") -> tuple[TutorialPage, ...]:
    return _simplified_chinese_pages() if language == "zh-cn" else _english_pages()


def _welcome_line(language: str) -> str:
    if language == "zh-cn":
        return "欢迎使用 Jazzband。"
    return "Welcome to Jazzband."


def _continue_prompt(language: str) -> str:
    if language == "zh-cn":
        return "按 Enter 继续，或输入 s 跳过教程: "
    return "Press Enter for next, or type s to skip: "


def _skipped_line(language: str) -> str:
    if language == "zh-cn":
        return "已跳过教程。你可以继续完成 setup。"
    return "Orientation skipped. Setup will continue."


def _english_pages() -> tuple[TutorialPage, ...]:
    return (
        TutorialPage(
            "What is Jazzband?",
            (
                "Jazzband turns Linear into the control plane for coding agents.",
                "Instead of babysitting a handful of agent sessions, you write a clear",
                "ticket, move it into an active state, and let Jazzband dispatch the work.",
            ),
        ),
        TutorialPage(
            "What will init create?",
            (
                "This init flow creates a repo-owned WORKFLOW.md, connects the tracker,",
                "points agents at the GitHub repo, and prepares local workspace/log paths.",
            ),
        ),
        TutorialPage(
            "Why does issue-driven orchestration matter?",
            (
                "Issues become the shared contract for scope, status, review feedback,",
                "and handoff. That makes agent work easier to inspect, retry, and review.",
            ),
        ),
        TutorialPage(
            "What productivity shift did OpenAI report?",
            (
                "OpenAI described the old ceiling as engineers comfortably managing about",
                "3-5 Codex sessions before context switching got painful. With Jazzband,",
                "some teams saw landed PRs increase by 500% in the first three weeks.",
                f"Source: {JAZZBAND_OPENAI_BLOG_URL}",
            ),
        ),
        TutorialPage(
            "What should I expect next?",
            (
                "I will ask for the Linear project, workflow states, workspace location,",
                "GitHub repo, and local auth. After init, run `jazzband doctor WORKFLOW.md`",
                "to verify the setup, then try one disposable Linear ticket with",
                "`jazzband run WORKFLOW.md --once`.",
            ),
        ),
        TutorialPage(
            "One project per WORKFLOW.md",
            (
                "Each WORKFLOW.md targets one Linear project. To run multiple projects",
                "in parallel, start one process per project with a unique --port:",
                "  jazzband run project-a/WORKFLOW.md --port 7337",
                "  jazzband run project-b/WORKFLOW.md --port 7338",
                "To stop a specific process: kill <PID>  (find it with: ps aux | grep 'jazzband run')",
                "To stop all at once: pkill -f 'jazzband run'",
            ),
        ),
    )


def _simplified_chinese_pages() -> tuple[TutorialPage, ...]:
    return (
        TutorialPage(
            "Jazzband 是什么?",
            (
                "Jazzband 把 Linear 变成编码代理的控制台。你不需要同时盯着一堆",
                "agent session，只要写清楚 ticket，把它移动到活跃状态，Jazzband",
                "就会为这项工作启动 agent。",
            ),
        ),
        TutorialPage(
            "init 会创建什么?",
            (
                "init 会创建一个跟随仓库版本管理的 WORKFLOW.md，连接任务系统，",
                "指向 GitHub 仓库，并准备本地 workspace 和日志路径。",
            ),
        ),
        TutorialPage(
            "为什么要用 issue 驱动编排?",
            (
                "Issue 会成为 scope、状态、review feedback 和 handoff 的共同契约。",
                "这样 agent 工作更容易检查、重试和 review。",
            ),
        ),
        TutorialPage(
            "OpenAI 报告了什么生产力变化?",
            (
                "OpenAI 在 Jazzband 文章里提到，以前工程师通常能舒服管理大约",
                "3-5 个 Codex session，再多就会被上下文切换拖慢。使用 Jazzband",
                "后，一些团队在前三周落地的 PR 数量提升了 500%。",
                f"来源: {JAZZBAND_OPENAI_BLOG_URL}",
            ),
        ),
        TutorialPage(
            "接下来会发生什么?",
            (
                "我会依次询问 Linear project、工作流状态、workspace 位置、GitHub",
                "仓库和本地认证。完成 init 后，先运行 `jazzband doctor WORKFLOW.md`",
                "检查配置，再用一个临时 Linear ticket 跑一次:",
                "`jazzband run WORKFLOW.md --once`。",
            ),
        ),
        TutorialPage(
            "一个 WORKFLOW.md 对应一个项目",
            (
                "每个 WORKFLOW.md 只监听一个 Linear project。如果需要同时运行多个",
                "项目，为每个项目单独启动一个进程，并用 --port 分配不同端口：",
                "  jazzband run project-a/WORKFLOW.md --port 7337",
                "  jazzband run project-b/WORKFLOW.md --port 7338",
                "停止某个进程: kill <PID>  (用 ps aux | grep 'jazzband run' 查找)",
                "停止全部进程: pkill -f 'jazzband run'",
            ),
        ),
    )


def _load_tutorial_record(
    tutorial_id: str,
    *,
    path: str | Path | None,
    environ: Mapping[str, str] | None,
) -> dict[str, object]:
    payload = _load_history(_resolve_history_path(path, environ))
    tutorials = payload.get("tutorials")
    if not isinstance(tutorials, dict):
        return {}
    record = tutorials.get(tutorial_id)
    return record if isinstance(record, dict) else {}


def _load_history(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_history(path: Path, payload: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass

        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError:
        pass


def _resolve_history_path(path: str | Path | None, environ: Mapping[str, str] | None) -> Path:
    return Path(path).expanduser() if path is not None else default_tutorial_history_path(environ)


def _non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None
