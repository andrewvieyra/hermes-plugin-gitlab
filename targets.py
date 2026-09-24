"""What the model is pointing at: project references, GitLab web URLs, iids, labels, the write
allow-list, and the API path builders every handler shares."""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlsplit

from .client import GitLabError, encode, project_id

_KINDS = {
    "merge_requests": "merge_request",
    "issues": "issue",
    "pipelines": "pipeline",
    "jobs": "job",
    "commit": "commit",
    "commits": "commits",
    "tree": "tree",
    "blob": "blob",
    "compare": "compare",
    "tags": "tag",
    "branches": "branch",
}
_PROJECT_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.\-]*(?:/[A-Za-z0-9_.][A-Za-z0-9_.\-]*)+$")
# Top-level web paths that are never a namespace: a URL under them names a group, an admin page or a
# listing, not a project.
_RESERVED_ROOTS = {
    "-",
    "admin",
    "api",
    "dashboard",
    "explore",
    "groups",
    "help",
    "oauth",
    "profile",
    "uploads",
    "users",
}


def parse_url(url: str, base_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """``https://gitlab.example.com/group/proj/-/merge_requests/12`` ->
    ``{"project": "group/proj", "kind": "merge_request", "iid": 12}``. ``None`` when *url* is not a
    GitLab web URL. Raises when the host differs from the configured instance."""
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    if base_url:
        expected = urlsplit(base_url).netloc.lower()
        if expected and parts.netloc.lower() != expected:
            raise GitLabError(f"URL host {parts.netloc!r} is not the configured GitLab host {expected!r}")
    path = unquote(parts.path).strip("/")
    if base_url:  # GitLab served under a relative URL root: https://host/gitlab/group/project
        prefix = urlsplit(base_url).path.strip("/")
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            path = path[len(prefix) :].strip("/")
    if not path or path.startswith("api/") or path.split("/")[0].lower() in _RESERVED_ROOTS:
        return None
    if "/-/" in path:
        project, rest = path.split("/-/", 1)
    else:
        segments = path.split("/")
        index = next((i for i, s in enumerate(segments) if i >= 2 and s in _KINDS), None)
        if index is None:
            project, rest = path, ""
        else:
            project, rest = "/".join(segments[:index]), "/".join(segments[index:])
    project = project.strip("/")
    if not _PROJECT_RE.match(project):
        return None
    result: Dict[str, Any] = {"project": project, "kind": None}
    if not rest:
        return result
    segments = rest.strip("/").split("/")
    kind = _KINDS.get(segments[0])
    result["kind"] = kind
    tail = segments[1:]
    if kind in {"merge_request", "issue"} and tail and tail[0].isdigit():
        result["iid"] = int(tail[0])
    elif kind in {"pipeline", "job"} and tail and tail[0].isdigit():
        result["id"] = int(tail[0])
    elif kind == "commit" and tail:
        result["sha"] = tail[0]
    elif kind in {"tree", "blob"} and tail:
        result["ref"] = tail[0]
        result["path"] = "/".join(tail[1:])
    elif kind == "compare" and tail:
        result["range"] = tail[0]
    return result


def project_arg(value: Any, base_url: Optional[str] = None) -> str:
    """Normalise the ``project`` argument: a numeric id, a ``group/project`` path, or a GitLab web URL.
    Returns the id or the *unencoded* path; :func:`client.project_id` encodes it for the URL."""
    if value is None or isinstance(value, bool):
        raise GitLabError("project is required: a numeric id, a 'group/project' path, or a GitLab URL")
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    if not text:
        raise GitLabError("project is required: a numeric id, a 'group/project' path, or a GitLab URL")
    if text.lower().startswith(("http://", "https://")):
        parsed = parse_url(text, base_url)
        if not parsed:
            raise GitLabError(f"could not find a project in URL {text!r}")
        return parsed["project"]
    text = text.strip("/")
    if text.isdigit():
        return text
    if not _PROJECT_RE.match(text):
        raise GitLabError(f"invalid project {value!r}: expected a numeric id or a 'group/project' path")
    return text


def positive_int(value: Any, name: str) -> int:
    """``value`` as a positive int (``"7"`` counts, ``True`` does not)."""
    if value is None or isinstance(value, bool):
        raise GitLabError(f"{name} is required")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise GitLabError(f"{name} must be a positive integer (got {value!r})") from None
    if number <= 0:
        raise GitLabError(f"{name} must be a positive integer (got {value!r})")
    return number


def to_bool(value: Any, name: str, default: Optional[bool] = None) -> Optional[bool]:
    """Boolean argument from the model. ``None`` and ``""`` mean "not given" (*default*); true/false,
    yes/no, on/off and 1/0 are accepted in any case; anything else is an error rather than a silent
    ``True`` (``confidential: "nope"`` must not create a confidential issue)."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise GitLabError(f"{name} must be true or false (got {value!r})")


def optional_int(value: Any, name: str) -> Optional[int]:
    """A positive int, or ``None`` for ``None`` / ``""``."""
    if value is None or value == "":
        return None
    return positive_int(value, name)


def str_list(value: Any) -> List[str]:
    """A list of non-empty strings from a list or a comma-separated string."""
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()] if str(value).strip() else []


def labels_arg(value: Any) -> Optional[str]:
    """GitLab takes labels as a comma-separated string; accept a list too."""
    items = str_list(value)
    return ",".join(items) if items else None


def matches_allowlist(path_with_namespace: str, patterns: List[str]) -> bool:
    """Case-insensitive glob match of ``group/project`` against the ``write_projects`` setting.
    An empty list allows every project. ``*`` matches across ``/``, so ``platform/*`` covers subgroups."""
    if not patterns:
        return True
    path = (path_with_namespace or "").strip("/").lower()
    return any(fnmatch.fnmatchcase(path, pattern.strip("/").lower()) for pattern in patterns if pattern.strip())


# -- API paths ------------------------------------------------------------------------------------


def p_project(project: Any) -> str:
    """``projects/:id`` with the id or path encoded."""
    return f"projects/{project_id(project)}"


def p_issue(project: Any, iid: int) -> str:
    """``projects/:id/issues/:iid``."""
    return f"{p_project(project)}/issues/{int(iid)}"


def p_mr(project: Any, iid: int) -> str:
    """``projects/:id/merge_requests/:iid``."""
    return f"{p_project(project)}/merge_requests/{int(iid)}"


def p_pipeline(project: Any, pipeline_id: int) -> str:
    """``projects/:id/pipelines/:pipeline_id``."""
    return f"{p_project(project)}/pipelines/{int(pipeline_id)}"


def p_job(project: Any, job_id: int) -> str:
    """``projects/:id/jobs/:job_id``."""
    return f"{p_project(project)}/jobs/{int(job_id)}"


def p_branch(project: Any, name: str) -> str:
    """``projects/:id/repository/branches/:name`` with the name encoded."""
    return f"{p_project(project)}/repository/branches/{encode(name)}"


def p_file(project: Any, path: str) -> str:
    """``projects/:id/repository/files/:path`` with the path encoded."""
    return f"{p_project(project)}/repository/files/{encode(path.strip('/'))}"


def p_commit(project: Any, sha: str) -> str:
    """``projects/:id/repository/commits/:sha`` with the sha encoded."""
    return f"{p_project(project)}/repository/commits/{encode(sha)}"


def project_from_path(path: str) -> Optional[str]:
    """``projects/group%2Fproj/issues`` -> ``group/proj``; ``projects/12/...`` -> ``12``; else ``None``."""
    segments = path.strip("/").split("/")
    if len(segments) >= 2 and segments[0] == "projects":
        return unquote(segments[1])
    return None


def validate_path_safe(path: Any) -> str:
    """:func:`client.validate_path` re-exported here so builders have one import for path handling."""
    from .client import validate_path

    return validate_path(path)
