"""hermes-plugin-gitlab — GitLab (self-managed or GitLab.com) for Hermes Agent.

Registers six read tools (search / repo / issues / merge_requests / pipelines / api), four write tools
(issue_write / mr_write / pipeline_write / commit), a ``/gitlab`` slash command, a ``hermes gitlab`` CLI
subcommand, and the bundled ``gitlab:workflow`` skill. Registration performs no network I/O; the
GitLab client is created lazily inside each tool call.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import commands, handlers, schemas
from .settings import Settings, set_settings
from .version import __version__

__all__ = ["__version__", "register"]

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).parent
_TOOLSET = "gitlab"
_EMOJI = {
    "gitlab_search": "🔎",
    "gitlab_repo": "📁",
    "gitlab_issues": "🐞",
    "gitlab_merge_requests": "🔀",
    "gitlab_pipelines": "🏗️",
    "gitlab_api": "🧰",
    "gitlab_issue_write": "📝",
    "gitlab_mr_write": "✅",
    "gitlab_pipeline_write": "▶️",
    "gitlab_commit": "💾",
}
_REQUIRES_ENV = ["GITLAB_URL", "GITLAB_TOKEN"]


def register(ctx) -> None:
    """Called once by Hermes' plugin loader."""
    set_settings(Settings.from_ctx(ctx))
    from . import audit

    audit.set_audit_log(None)  # re-resolve path/enabled from the fresh settings
    from . import sinks

    sinks.set_worker(None)  # sinks are built lazily from the fresh settings on first event

    for schema in schemas.ALL:
        name = schema["name"]
        is_write = name in handlers.WRITE_TOOLS
        ctx.register_tool(
            name=name,
            toolset=_TOOLSET,
            schema=schema,
            handler=handlers.HANDLERS[name],
            check_fn=handlers.check_write_requirements if is_write else handlers.check_requirements,
            requires_env=_REQUIRES_ENV,
            emoji=_EMOJI.get(name, ""),
            description=schema["description"].split(". ")[0],
        )

    ctx.register_command(
        "gitlab",
        handler=commands.slash_handler,
        args_hint="<status|mr|pending|show|run|drop|prune|audit> [args]",
        description="GitLab: status, merge request summary, staged writes, audit trail",
    )

    try:
        ctx.register_cli_command(
            name="gitlab",
            help="GitLab: status, mr, pending, show, run, drop, prune, audit",
            setup_fn=commands.setup_cli,
            handler_fn=commands.cli_handler,
            description="Inspect GitLab and run staged writes from the shell (no model involved).",
        )
    except Exception as exc:  # older Hermes without CLI registration keeps working without it
        logger.debug("gitlab plugin: CLI command not registered: %s", exc)

    skill = _PLUGIN_DIR / "SKILL.md"
    if skill.exists():
        try:
            ctx.register_skill(
                "workflow", skill, description="Read, review, triage and change GitLab safely with the gitlab_* tools"
            )
        except Exception as exc:
            logger.debug("gitlab plugin: skill not registered: %s", exc)
