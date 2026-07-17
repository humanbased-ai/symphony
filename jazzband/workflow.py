from __future__ import annotations

import logging
import re
from collections.abc import AsyncGenerator
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .config import ConfigError, WorkflowConfig


LOGGER = logging.getLogger(__name__)


class WorkflowError(ValueError):
    """Raised when WORKFLOW.md cannot be parsed into the Jazzband contract."""


@dataclass(frozen=True)
class WorkflowDefinition:
    config: dict[str, Any]
    prompt_template: str

    def typed_config(
        self,
        *,
        workflow_path: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> WorkflowConfig:
        return WorkflowConfig.from_mapping(self.config, workflow_path=workflow_path, environ=environ)


@dataclass(frozen=True)
class EffectiveWorkflow:
    definition: WorkflowDefinition
    config: WorkflowConfig


def load_workflow(path: str | Path) -> WorkflowDefinition:
    workflow_path = Path(path)

    try:
        content = workflow_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise WorkflowError("missing_workflow_file") from exc

    return parse_workflow(content)


def parse_workflow(content: str) -> WorkflowDefinition:
    if content.startswith("---"):
        front_matter, body = _split_front_matter(content)
        try:
            parsed = yaml.safe_load(front_matter) if front_matter.strip() else {}
        except yaml.YAMLError as exc:
            raise WorkflowError("workflow_parse_error") from exc

        if parsed is None:
            parsed = {}
        if not isinstance(parsed, dict):
            raise WorkflowError("workflow_front_matter_must_be_map")

        return WorkflowDefinition(config=parsed, prompt_template=body.strip())

    return WorkflowDefinition(config={}, prompt_template=content.strip())


def render_prompt(prompt_template: str, *, issue: Any, attempt: int | None = None) -> str:
    template = prompt_template.strip() or "You are working on an issue from Linear."
    context = {"issue": _template_value(issue), "attempt": attempt}

    try:
        from jinja2 import Environment, StrictUndefined, TemplateError
    except ModuleNotFoundError:
        return _render_prompt_fallback(template, context)

    try:
        environment = Environment(undefined=StrictUndefined, autoescape=False)
        return environment.from_string(template).render(context).strip()
    except TemplateError as exc:
        raise WorkflowError("template_render_error") from exc


@dataclass
class WorkflowReloader:
    path: Path
    last_good: WorkflowDefinition | None = None
    last_good_effective: EffectiveWorkflow | None = None
    last_error: Exception | None = None

    @classmethod
    def for_path(cls, path: str | Path) -> "WorkflowReloader":
        return cls(Path(path))

    def load_initial(self) -> WorkflowDefinition:
        workflow = load_workflow(self.path)
        self.last_good = workflow
        self.last_error = None
        return workflow

    def load_initial_effective(self, *, environ: Mapping[str, str] | None = None) -> EffectiveWorkflow:
        workflow = load_workflow(self.path)
        effective = EffectiveWorkflow(
            definition=workflow,
            config=workflow.typed_config(workflow_path=self.path, environ=environ),
        )
        self.last_good = workflow
        self.last_good_effective = effective
        self.last_error = None
        return effective

    # Not thread-safe; callers must serialize reload calls.
    def reload(self) -> WorkflowDefinition:
        try:
            workflow = load_workflow(self.path)
        except WorkflowError as exc:
            self._reject_reload(exc)
            if self.last_good is None:
                raise
            return self.last_good

        self.last_good = workflow
        self.last_error = None
        return workflow

    # Not thread-safe; callers must serialize reload calls.
    def reload_effective(self, *, environ: Mapping[str, str] | None = None) -> EffectiveWorkflow:
        try:
            workflow = load_workflow(self.path)
            effective = EffectiveWorkflow(
                definition=workflow,
                config=workflow.typed_config(workflow_path=self.path, environ=environ),
            )
        except (WorkflowError, ConfigError) as exc:
            self._reject_reload(exc)
            if self.last_good_effective is None:
                raise
            return self.last_good_effective

        self.last_good = workflow
        self.last_good_effective = effective
        self.last_error = None
        return effective

    def _reject_reload(self, exc: Exception) -> None:
        self.last_error = exc
        LOGGER.error("Rejected WORKFLOW.md reload for %s: %s", self.path, exc)


async def watch_workflow(path: str | Path) -> AsyncGenerator[Any, None]:
    try:
        from watchfiles import awatch
    except ModuleNotFoundError as exc:
        raise WorkflowError("workflow_watcher_unavailable") from exc

    workflow_path = Path(path).resolve()
    async for changes in awatch(workflow_path.parent, debounce=100, step=50):
        if any(Path(changed_path).resolve() == workflow_path for _, changed_path in changes):
            yield changes


def _split_front_matter(content: str) -> tuple[str, str]:
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", content

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :])

    raise WorkflowError("unterminated_workflow_front_matter")


def _template_value(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {key: _template_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_template_value(item) for item in value]
    return value


_INTERPOLATION_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")


def _render_prompt_fallback(template: str, context: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        expression = match.group(1).strip()
        if "|" in expression:
            raise WorkflowError("template_render_error")

        value: Any = context
        for part in expression.split("."):
            part = part.strip()
            if not part:
                raise WorkflowError("template_render_error")
            if isinstance(value, dict) and part in value:
                value = value[part]
                continue
            raise WorkflowError("template_render_error")

        return "" if value is None else str(value)

    return _INTERPOLATION_RE.sub(replace, template).strip()
