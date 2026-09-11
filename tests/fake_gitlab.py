"""In-memory stand-in for a GitLab instance, speaking the subset of the REST API the plugin uses.

It behaves like GitLab where it matters for these tests: ``PRIVATE-TOKEN`` / ``Bearer`` auth with
401 otherwise; page-based pagination with ``x-total`` / ``x-next-page`` headers (and no ``x-total``
for commits, like the real thing); URL-encoded project paths; GitLab's error shapes (``{"message":
"404 Project Not Found"}``, ``{"message": {"title": [...]}}``, ``{"error": "..."}``); 409 on sha
mismatch for approve and merge; 405/422 for unmergeable MRs; base64 file contents; an optional
per-request failure hook and a one-shot 429 for the retry path.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlsplit


class _Response:
    def __init__(
        self, status: int, body: Any = None, headers: Optional[Dict[str, str]] = None, text: Optional[str] = None
    ):
        self.status_code = status
        self._body = body
        self.headers = dict(headers or {})
        if text is not None:
            self.text = text
        else:
            self.text = "" if body is None else json.dumps(body)

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _sha(seed: str) -> str:
    return hashlib.sha1(seed.encode()).hexdigest()


class FakeGitLab:
    """``session``-compatible object: ``request(method, url, params=, json=, headers=, ...)``."""

    def __init__(self, base_url: str = "https://gitlab.test", token: str = "glpat-fakeTOKEN1234567890abcdef"):
        self.base_url = base_url
        self.token = token
        self.calls: List[Dict[str, Any]] = []
        self.fail_on: Optional[Callable[[str, str, Any, Any], Optional[_Response]]] = None
        self.rate_limit_once = False
        self.advanced_search = False
        self.mergeable = True
        self.version = {"version": "17.4.1", "revision": "abc1234", "enterprise": False}
        self.token_info: Optional[Dict[str, Any]] = None
        self.users: Dict[int, Dict[str, Any]] = {}
        self.current_user_id = 1
        self.projects: Dict[int, Dict[str, Any]] = {}
        self.branches: Dict[int, Dict[str, Dict[str, Any]]] = {}
        self.files: Dict[Tuple[int, str], Dict[str, str]] = {}
        self.commits: Dict[int, List[Dict[str, Any]]] = {}
        self.commit_diffs: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
        self.issues: Dict[int, Dict[int, Dict[str, Any]]] = {}
        self.mrs: Dict[int, Dict[int, Dict[str, Any]]] = {}
        self.discussions: Dict[Tuple[int, str, int], List[Dict[str, Any]]] = {}
        self.mr_diffs: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        self.mr_commits: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        self.mr_pipelines: Dict[Tuple[int, int], List[int]] = {}
        self.approvals: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self.pipelines: Dict[int, Dict[int, Dict[str, Any]]] = {}
        self.jobs: Dict[int, Dict[int, Dict[str, Any]]] = {}
        self.traces: Dict[int, str] = {}
        self.milestones: Dict[int, List[Dict[str, Any]]] = {}
        self.labels: Dict[int, List[Dict[str, Any]]] = {}
        self.releases: Dict[int, List[Dict[str, Any]]] = {}
        self.variables: Dict[int, List[Dict[str, Any]]] = {}
        self._next = {"note": 500, "discussion": 0, "pipeline": 200, "job": 2000, "label": 30, "global": 9000}
        self._clock = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)

    # -- helpers ------------------------------------------------------------------------------
    def _tick(self) -> str:
        self._clock += timedelta(seconds=1)
        return self._clock.isoformat().replace("+00:00", "Z")

    def _id(self, kind: str) -> int:
        self._next[kind] += 1
        return self._next[kind]

    def writes(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c["method"] != "GET"]

    def user(self, user_id: int) -> Dict[str, Any]:
        u = self.users[user_id]
        return {k: u.get(k) for k in ("id", "username", "name", "state", "avatar_url", "web_url")}

    def project_by_ref(self, ref: str) -> Optional[Dict[str, Any]]:
        ref = unquote(ref)
        if ref.isdigit():
            return self.projects.get(int(ref))
        return next((p for p in self.projects.values() if p["path_with_namespace"] == ref), None)

    def add_project(self, pid: int, path: str, default_branch: str = "main", **extra: Any) -> Dict[str, Any]:
        group, _, name = path.rpartition("/")
        project = {
            "id": pid,
            "name": name,
            "path": name,
            "path_with_namespace": path,
            "namespace": {"id": 100 + pid, "full_path": group, "kind": "group"},
            "default_branch": default_branch,
            "web_url": f"{self.base_url}/{path}",
            "description": extra.pop("description", f"{name} service"),
            "visibility": "private",
            "archived": False,
            "last_activity_at": self._tick(),
            "created_at": "2025-01-01T00:00:00Z",
            "star_count": 1,
            "forks_count": 0,
            "open_issues_count": 0,
            "merge_method": "merge",
            "only_allow_merge_if_pipeline_succeeds": False,
            "squash_option": "default_off",
            "topics": [],
        }
        project.update(extra)
        self.projects[pid] = project
        self.branches.setdefault(pid, {})
        self.commits.setdefault(pid, [])
        self.issues.setdefault(pid, {})
        self.mrs.setdefault(pid, {})
        self.pipelines.setdefault(pid, {})
        self.jobs.setdefault(pid, {})
        return project

    def _commit_obj(
        self,
        pid: int,
        sha: str,
        title: str,
        parents: List[str],
        author: str = "Dana Dev",
        stats: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        stamp = self._tick()
        return {
            "id": sha,
            "short_id": sha[:8],
            "title": title,
            "message": title + "\n",
            "author_name": author,
            "author_email": "dana@example.com",
            "authored_date": stamp,
            "committer_name": author,
            "committer_email": "dana@example.com",
            "committed_date": stamp,
            "created_at": stamp,
            "parent_ids": parents,
            "web_url": f"{self.base_url}/{self.projects[pid]['path_with_namespace']}/-/commit/{sha}",
            "stats": stats or {"additions": 1, "deletions": 0, "total": 1},
        }

    def add_commit(
        self,
        pid: int,
        branch: str,
        title: str,
        files: Optional[Dict[str, Optional[str]]] = None,
        diffs: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        head = self.branches[pid].get(branch)
        parents = [head["commit"]["id"]] if head else []
        sha = _sha(f"{pid}:{branch}:{title}:{len(self.commits[pid])}")
        commit = self._commit_obj(pid, sha, title, parents)
        commit["_branch"] = branch
        self.commits[pid].insert(0, commit)
        tree = dict(self.files.get((pid, branch), {}))
        for path, content in (files or {}).items():
            if content is None:
                tree.pop(path, None)
            else:
                tree[path] = content
        self.files[(pid, branch)] = tree
        self.files[(pid, sha)] = tree
        self.branches[pid][branch] = self._branch_obj(pid, branch, commit)
        self.commit_diffs[(pid, sha)] = diffs or []
        return commit

    def _branch_obj(self, pid: int, name: str, commit: Dict[str, Any]) -> Dict[str, Any]:
        project = self.projects[pid]
        return {
            "name": name,
            "merged": False,
            "protected": name == project["default_branch"],
            "default": name == project["default_branch"],
            "developers_can_push": False,
            "developers_can_merge": False,
            "can_push": True,
            "web_url": f"{self.base_url}/{project['path_with_namespace']}/-/tree/{name}",
            "commit": {
                k: commit[k]
                for k in (
                    "id",
                    "short_id",
                    "title",
                    "author_name",
                    "authored_date",
                    "committed_date",
                    "message",
                    "parent_ids",
                )
            },
        }

    def head(self, pid: int, branch: str) -> str:
        return self.branches[pid][branch]["commit"]["id"]

    def add_issue(self, pid: int, iid: int, title: str, **extra: Any) -> Dict[str, Any]:
        project = self.projects[pid]
        stamp = self._tick()
        issue = {
            "id": self._id("global"),
            "iid": iid,
            "project_id": pid,
            "title": title,
            "description": extra.pop("description", f"Description of {title}"),
            "state": "opened",
            "issue_type": "issue",
            "author": self.user(2),
            "assignees": [],
            "labels": [],
            "milestone": None,
            "due_date": None,
            "confidential": False,
            "discussion_locked": None,
            "user_notes_count": 0,
            "merge_requests_count": 0,
            "created_at": stamp,
            "updated_at": stamp,
            "closed_at": None,
            "closed_by": None,
            "web_url": f"{self.base_url}/{project['path_with_namespace']}/-/issues/{iid}",
            "references": {
                "short": f"#{iid}",
                "relative": f"#{iid}",
                "full": f"{project['path_with_namespace']}#{iid}",
            },
            "time_stats": {"time_estimate": 0, "total_time_spent": 0},
            "weight": None,
        }
        issue.update(extra)
        self.issues[pid][iid] = issue
        self.discussions.setdefault((pid, "issues", iid), [])
        return issue

    def add_mr(self, pid: int, iid: int, title: str, source: str, target: str = "main", **extra: Any) -> Dict[str, Any]:
        project = self.projects[pid]
        stamp = self._tick()
        sha = self.head(pid, source) if source in self.branches[pid] else _sha(f"gone:{iid}")
        base = self.head(pid, target)
        mr = {
            "id": self._id("global"),
            "iid": iid,
            "project_id": pid,
            "title": title,
            "description": extra.pop("description", f"Description of {title}"),
            "state": "opened",
            "draft": title.lower().startswith("draft:"),
            "work_in_progress": title.lower().startswith("draft:"),
            "author": self.user(3),
            "assignees": [],
            "reviewers": [],
            "labels": [],
            "source_branch": source,
            "target_branch": target,
            "source_project_id": pid,
            "target_project_id": pid,
            "sha": sha,
            "merge_commit_sha": None,
            "squash_commit_sha": None,
            "merge_status": "can_be_merged",
            "detailed_merge_status": "mergeable",
            "has_conflicts": False,
            "changes_count": "2",
            "user_notes_count": 0,
            "head_pipeline": None,
            "milestone": None,
            "created_at": stamp,
            "updated_at": stamp,
            "merged_at": None,
            "merged_by": None,
            "closed_at": None,
            "web_url": f"{self.base_url}/{project['path_with_namespace']}/-/merge_requests/{iid}",
            "references": {
                "short": f"!{iid}",
                "relative": f"!{iid}",
                "full": f"{project['path_with_namespace']}!{iid}",
            },
            "diff_refs": {"base_sha": base, "head_sha": sha, "start_sha": base},
            "squash": False,
            "should_remove_source_branch": None,
            "force_remove_source_branch": False,
            "merge_when_pipeline_succeeds": False,
            "blocking_discussions_resolved": True,
            "diverged_commits_count": 0,
            "rebase_in_progress": False,
            "discussion_locked": None,
        }
        mr.update(extra)
        self.mrs[pid][iid] = mr
        self.discussions.setdefault((pid, "merge_requests", iid), [])
        self.mr_diffs.setdefault((pid, iid), [])
        self.mr_commits.setdefault((pid, iid), [])
        self.mr_pipelines.setdefault((pid, iid), [])
        self.approvals.setdefault((pid, iid), {"required": 1, "approved_by": []})
        return mr

    def _note(
        self,
        author_id: int,
        body: str,
        *,
        system: bool = False,
        resolvable: bool = False,
        position: Optional[Dict[str, Any]] = None,
        internal: bool = False,
    ) -> Dict[str, Any]:
        stamp = self._tick()
        note = {
            "id": self._id("note"),
            "type": "DiffNote" if position else ("DiscussionNote" if resolvable else None),
            "body": body,
            "author": self.user(author_id),
            "created_at": stamp,
            "updated_at": stamp,
            "system": system,
            "noteable_type": "MergeRequest",
            "resolvable": resolvable,
            "resolved": False if resolvable else None,
            "resolved_by": None,
            "internal": internal,
            "confidential": internal,
        }
        if position:
            note["position"] = position
        return note

    def add_discussion(
        self, key: Tuple[int, str, int], notes: List[Dict[str, Any]], individual: bool = False
    ) -> Dict[str, Any]:
        self._next["discussion"] += 1
        discussion = {"id": _sha(f"disc:{self._next['discussion']}"), "individual_note": individual, "notes": notes}
        self.discussions.setdefault(key, []).append(discussion)
        return discussion

    def add_pipeline(self, pid: int, ref: str, status: str, source: str = "push", **extra: Any) -> Dict[str, Any]:
        project = self.projects[pid]
        pipeline_id = self._id("pipeline")
        stamp = self._tick()
        pipeline = {
            "id": pipeline_id,
            "iid": len(self.pipelines[pid]) + 1,
            "project_id": pid,
            "status": status,
            "source": source,
            "ref": ref,
            "sha": self.head(pid, ref) if ref in self.branches[pid] else _sha(ref),
            "before_sha": "0" * 40,
            "tag": False,
            "yaml_errors": None,
            "user": self.user(3),
            "name": None,
            "created_at": stamp,
            "updated_at": stamp,
            "started_at": stamp,
            "finished_at": stamp if status in {"success", "failed", "canceled"} else None,
            "duration": 61,
            "queued_duration": 2,
            "coverage": None,
            "detailed_status": {"text": status, "label": status},
            "web_url": f"{self.base_url}/{project['path_with_namespace']}/-/pipelines/{pipeline_id}",
        }
        pipeline.update(extra)
        self.pipelines[pid][pipeline_id] = pipeline
        return pipeline

    def add_job(
        self, pid: int, pipeline_id: int, name: str, stage: str, status: str, trace: str = "", **extra: Any
    ) -> Dict[str, Any]:
        project = self.projects[pid]
        pipeline = self.pipelines[pid][pipeline_id]
        job_id = self._id("job")
        stamp = self._tick()
        job = {
            "id": job_id,
            "name": name,
            "stage": stage,
            "status": status,
            "ref": pipeline["ref"],
            "tag": False,
            "allow_failure": False,
            "failure_reason": "script_failure" if status == "failed" else None,
            "duration": 12.5,
            "queued_duration": 0.5,
            "created_at": stamp,
            "started_at": stamp,
            "finished_at": stamp if status in {"success", "failed"} else None,
            "coverage": None,
            "artifacts": [{"file_type": "trace", "size": len(trace), "filename": "job.log", "file_format": None}],
            "runner": {"id": 1, "description": "shared-runner-1"},
            "pipeline": {
                "id": pipeline_id,
                "project_id": pid,
                "ref": pipeline["ref"],
                "sha": pipeline["sha"],
                "status": pipeline["status"],
            },
            "user": self.user(3),
            "web_url": f"{self.base_url}/{project['path_with_namespace']}/-/jobs/{job_id}",
            "retried": False,
        }
        job.update(extra)
        self.jobs[pid][job_id] = job
        self.traces[job_id] = trace
        return job

    # -- session interface --------------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        params: Any = None,
        json: Any = None,
        headers: Any = None,
        timeout: Any = None,
        verify: Any = None,
    ) -> _Response:
        parts = urlsplit(url)
        assert url.startswith(self.base_url + "/api/v4/"), url
        path = parts.path[len("/api/v4/") :].strip("/")
        query: Dict[str, Any] = dict(parse_qsl(parts.query))
        for key, value in (params or {}).items():
            query[key] = value
        headers = dict(headers or {})
        self.calls.append({"method": method, "path": path, "params": dict(query), "json": json, "headers": headers})
        if self.fail_on:
            forced = self.fail_on(method, path, query, json)
            if forced is not None:
                return forced
        if self.rate_limit_once:
            self.rate_limit_once = False
            return _Response(429, {"message": "429 Retry later"}, {"Retry-After": "0"})
        auth = headers.get("PRIVATE-TOKEN") or (
            headers.get("Authorization", "")[7:] if headers.get("Authorization", "").startswith("Bearer ") else ""
        )
        if auth != self.token:
            return _Response(401, {"message": "401 Unauthorized"})
        for verb, regex, handler in self._routes():
            if verb != method:
                continue
            match = regex.fullmatch(path)
            if match:
                return handler(match, query, json)
        return _Response(404, {"error": "404 Not Found"})

    # -- pagination ---------------------------------------------------------------------------
    def _page(self, items: List[Any], query: Dict[str, Any], *, total_header: bool = True) -> _Response:
        per_page = max(1, min(100, int(query.get("per_page", 20))))
        page = max(1, int(query.get("page", 1)))
        total = len(items)
        pages = max(1, (total + per_page - 1) // per_page)
        chunk = items[(page - 1) * per_page : page * per_page]
        headers = {"x-page": str(page), "x-per-page": str(per_page)}
        if page < pages:
            headers["x-next-page"] = str(page + 1)
        if page > 1:
            headers["x-prev-page"] = str(page - 1)
        if total_header:
            headers["x-total"] = str(total)
            headers["x-total-pages"] = str(pages)
        return _Response(200, chunk, headers)

    # -- routing ------------------------------------------------------------------------------
    def _routes(self):
        P = r"projects/(?P<project>[^/]+)"
        return [
            ("GET", re.compile(r"version"), lambda m, q, j: _Response(200, dict(self.version))),
            ("GET", re.compile(r"user"), lambda m, q, j: _Response(200, dict(self.users[self.current_user_id]))),
            ("GET", re.compile(r"users"), self._users),
            (
                "GET",
                re.compile(r"personal_access_tokens/self"),
                lambda m, q, j: (
                    _Response(200, dict(self.token_info))
                    if self.token_info
                    else _Response(404, {"message": "404 Not Found"})
                ),
            ),
            ("GET", re.compile(r"projects"), self._projects),
            ("GET", re.compile(r"groups/(?P<group>[^/]+)/projects"), self._group_projects),
            ("GET", re.compile(r"search"), lambda m, q, j: self._search(None, q)),
            (
                "GET",
                re.compile(r"groups/(?P<group>[^/]+)/search"),
                lambda m, q, j: self._search(None, q, group=unquote(m.group("group"))),
            ),
            ("GET", re.compile(P), self._project),
            ("PUT", re.compile(P), lambda m, q, j: _Response(200, dict(self._p(m), **(j or {})))),
            ("GET", re.compile(P + r"/search"), lambda m, q, j: self._search(self._p(m), q)),
            (
                "GET",
                re.compile(P + r"/variables"),
                lambda m, q, j: _Response(200, self.variables.get(self._p(m)["id"], [])),
            ),
            (
                "GET",
                re.compile(P + r"/releases"),
                lambda m, q, j: self._page(self.releases.get(self._p(m)["id"], []), q),
            ),
            ("GET", re.compile(P + r"/labels"), lambda m, q, j: self._page(self.labels.get(self._p(m)["id"], []), q)),
            ("POST", re.compile(P + r"/labels"), self._create_label),
            ("DELETE", re.compile(P + r"/labels/(?P<label>[^/]+)"), lambda m, q, j: _Response(204)),
            (
                "GET",
                re.compile(P + r"/milestones"),
                lambda m, q, j: self._page(
                    [
                        x
                        for x in self.milestones.get(self._p(m)["id"], [])
                        if not q.get("title") or x["title"] == q["title"]
                    ],
                    q,
                ),
            ),
            ("GET", re.compile(P + r"/repository/tree"), self._tree),
            ("GET", re.compile(P + r"/repository/files/(?P<file>[^/]+)"), self._file),
            ("GET", re.compile(P + r"/repository/commits"), self._commits),
            ("POST", re.compile(P + r"/repository/commits"), self._create_commit),
            ("GET", re.compile(P + r"/repository/commits/(?P<sha>[^/]+)"), self._commit),
            (
                "GET",
                re.compile(P + r"/repository/commits/(?P<sha>[^/]+)/diff"),
                lambda m, q, j: self._page(self.commit_diffs.get((self._p(m)["id"], unquote(m.group("sha"))), []), q),
            ),
            ("GET", re.compile(P + r"/repository/compare"), self._compare),
            (
                "GET",
                re.compile(P + r"/repository/branches"),
                lambda m, q, j: self._page(
                    [
                        b
                        for b in self.branches[self._p(m)["id"]].values()
                        if not q.get("search") or q["search"] in b["name"]
                    ],
                    q,
                ),
            ),
            ("GET", re.compile(P + r"/repository/branches/(?P<branch>[^/]+)"), self._branch),
            ("GET", re.compile(P + r"/repository/tags"), lambda m, q, j: self._page(self._tags(self._p(m)["id"]), q)),
            ("GET", re.compile(r"issues"), lambda m, q, j: self._issue_list(None, q)),
            ("GET", re.compile(P + r"/issues"), lambda m, q, j: self._issue_list(self._p(m), q)),
            ("POST", re.compile(P + r"/issues"), self._issue_create),
            (
                "GET",
                re.compile(P + r"/issues/(?P<iid>\d+)"),
                lambda m, q, j: self._one(self._p(m), "issues", int(m.group("iid"))),
            ),
            ("PUT", re.compile(P + r"/issues/(?P<iid>\d+)"), self._issue_update),
            (
                "GET",
                re.compile(P + r"/issues/(?P<iid>\d+)/discussions"),
                lambda m, q, j: self._discussions(self._p(m), "issues", int(m.group("iid")), q),
            ),
            (
                "POST",
                re.compile(P + r"/issues/(?P<iid>\d+)/discussions"),
                lambda m, q, j: self._new_discussion(self._p(m), "issues", int(m.group("iid")), j),
            ),
            (
                "POST",
                re.compile(P + r"/issues/(?P<iid>\d+)/discussions/(?P<did>[^/]+)/notes"),
                lambda m, q, j: self._reply(self._p(m), "issues", int(m.group("iid")), m.group("did"), j),
            ),
            (
                "POST",
                re.compile(P + r"/issues/(?P<iid>\d+)/notes"),
                lambda m, q, j: self._new_note(self._p(m), "issues", int(m.group("iid")), j),
            ),
            (
                "GET",
                re.compile(P + r"/issues/(?P<iid>\d+)/related_merge_requests"),
                lambda m, q, j: self._page(
                    [
                        mr
                        for mr in self.mrs[self._p(m)["id"]].values()
                        if f"#{m.group('iid')}" in (mr.get("description") or "")
                    ],
                    q,
                ),
            ),
            ("GET", re.compile(r"merge_requests"), lambda m, q, j: self._mr_list(None, q)),
            ("GET", re.compile(P + r"/merge_requests"), lambda m, q, j: self._mr_list(self._p(m), q)),
            ("POST", re.compile(P + r"/merge_requests"), self._mr_create),
            (
                "GET",
                re.compile(P + r"/merge_requests/(?P<iid>\d+)"),
                lambda m, q, j: self._one(self._p(m), "merge_requests", int(m.group("iid"))),
            ),
            ("PUT", re.compile(P + r"/merge_requests/(?P<iid>\d+)"), self._mr_update),
            (
                "GET",
                re.compile(P + r"/merge_requests/(?P<iid>\d+)/diffs"),
                lambda m, q, j: self._page(self.mr_diffs.get((self._p(m)["id"], int(m.group("iid"))), []), q),
            ),
            (
                "GET",
                re.compile(P + r"/merge_requests/(?P<iid>\d+)/discussions"),
                lambda m, q, j: self._discussions(self._p(m), "merge_requests", int(m.group("iid")), q),
            ),
            (
                "POST",
                re.compile(P + r"/merge_requests/(?P<iid>\d+)/discussions"),
                lambda m, q, j: self._new_discussion(self._p(m), "merge_requests", int(m.group("iid")), j),
            ),
            (
                "POST",
                re.compile(P + r"/merge_requests/(?P<iid>\d+)/discussions/(?P<did>[^/]+)/notes"),
                lambda m, q, j: self._reply(self._p(m), "merge_requests", int(m.group("iid")), m.group("did"), j),
            ),
            ("PUT", re.compile(P + r"/merge_requests/(?P<iid>\d+)/discussions/(?P<did>[^/]+)"), self._resolve),
            (
                "POST",
                re.compile(P + r"/merge_requests/(?P<iid>\d+)/notes"),
                lambda m, q, j: self._new_note(self._p(m), "merge_requests", int(m.group("iid")), j),
            ),
            (
                "GET",
                re.compile(P + r"/merge_requests/(?P<iid>\d+)/commits"),
                lambda m, q, j: self._page(self.mr_commits.get((self._p(m)["id"], int(m.group("iid"))), []), q),
            ),
            (
                "GET",
                re.compile(P + r"/merge_requests/(?P<iid>\d+)/pipelines"),
                lambda m, q, j: self._page(
                    [
                        self.pipelines[self._p(m)["id"]][i]
                        for i in self.mr_pipelines.get((self._p(m)["id"], int(m.group("iid"))), [])
                    ],
                    q,
                ),
            ),
            (
                "GET",
                re.compile(P + r"/merge_requests/(?P<iid>\d+)/approvals"),
                lambda m, q, j: self._approvals(self._p(m), int(m.group("iid"))),
            ),
            ("POST", re.compile(P + r"/merge_requests/(?P<iid>\d+)/approve"), self._approve),
            ("POST", re.compile(P + r"/merge_requests/(?P<iid>\d+)/unapprove"), self._unapprove),
            ("PUT", re.compile(P + r"/merge_requests/(?P<iid>\d+)/merge"), self._merge),
            (
                "PUT",
                re.compile(P + r"/merge_requests/(?P<iid>\d+)/rebase"),
                lambda m, q, j: _Response(202, {"rebase_in_progress": True}),
            ),
            ("GET", re.compile(P + r"/pipelines"), self._pipeline_list),
            ("GET", re.compile(P + r"/pipelines/latest"), self._pipeline_latest),
            ("POST", re.compile(P + r"/pipeline"), self._pipeline_create),
            (
                "GET",
                re.compile(P + r"/pipelines/(?P<pid>\d+)"),
                lambda m, q, j: self._pipeline(self._p(m), int(m.group("pid"))),
            ),
            ("GET", re.compile(P + r"/pipelines/(?P<pid>\d+)/jobs"), self._pipeline_jobs),
            ("POST", re.compile(P + r"/pipelines/(?P<pid>\d+)/(?P<op>retry|cancel)"), self._pipeline_op),
            (
                "GET",
                re.compile(P + r"/jobs"),
                lambda m, q, j: self._page(self._filter_jobs(list(self.jobs[self._p(m)["id"]].values()), q), q),
            ),
            ("GET", re.compile(P + r"/jobs/(?P<jid>\d+)"), lambda m, q, j: self._job(self._p(m), int(m.group("jid")))),
            ("GET", re.compile(P + r"/jobs/(?P<jid>\d+)/trace"), self._trace),
            ("POST", re.compile(P + r"/jobs/(?P<jid>\d+)/(?P<op>retry|cancel|play)"), self._job_op),
        ]

    # -- handlers -----------------------------------------------------------------------------
    def _p(self, match: re.Match[str]) -> Dict[str, Any]:
        project = self.project_by_ref(match.group("project"))
        if project is None:
            raise _NotFound("404 Project Not Found")
        return project

    def _users(self, m, q, j) -> _Response:
        rows = list(self.users.values())
        if q.get("username"):
            rows = [u for u in rows if u["username"].lower() == str(q["username"]).lower()]
        return self._page([self.user(u["id"]) for u in rows], q)

    def _projects(self, m, q, j) -> _Response:
        rows = list(self.projects.values())
        if q.get("search"):
            needle = str(q["search"]).lower()
            rows = [
                p
                for p in rows
                if needle in p["name"].lower()
                or (q.get("search_namespaces") and needle in p["path_with_namespace"].lower())
            ]
        if q.get("archived") is not None:
            want = str(q["archived"]).lower() in {"true", "1"}
            rows = [p for p in rows if bool(p.get("archived")) == want]
        return self._page(rows, q)

    def _group_projects(self, m, q, j) -> _Response:
        group = unquote(m.group("group")).lower()
        rows = [p for p in self.projects.values() if p["path_with_namespace"].lower().startswith(group + "/")]
        if q.get("search"):
            rows = [p for p in rows if str(q["search"]).lower() in p["name"].lower()]
        return self._page(rows, q)

    def _project(self, m, q, j) -> _Response:
        return _Response(200, dict(self._p(m)))

    def _search(self, project: Optional[Dict[str, Any]], q: Dict[str, Any], group: Optional[str] = None) -> _Response:
        scope, needle = str(q.get("scope")), str(q.get("search", "")).lower()
        pids = (
            [project["id"]]
            if project
            else [
                p["id"]
                for p in self.projects.values()
                if not group or p["path_with_namespace"].lower().startswith(group.lower() + "/")
            ]
        )
        if scope in {"blobs", "commits", "notes", "wiki_blobs"} and not self.advanced_search:
            return _Response(400, {"error": "scope does not have a valid value"})
        rows: List[Any] = []
        if scope == "projects":
            rows = [p for p in self.projects.values() if needle in p["name"].lower()]
        elif scope == "issues":
            rows = [i for pid in pids for i in self.issues[pid].values() if needle in i["title"].lower()]
            if q.get("state"):
                rows = [i for i in rows if i["state"] == q["state"]]
        elif scope == "merge_requests":
            rows = [x for pid in pids for x in self.mrs[pid].values() if needle in x["title"].lower()]
        elif scope == "milestones":
            rows = [x for pid in pids for x in self.milestones.get(pid, []) if needle in x["title"].lower()]
        elif scope == "users":
            rows = [
                self.user(u["id"])
                for u in self.users.values()
                if needle in u["username"].lower() or needle in u["name"].lower()
            ]
        elif scope == "blobs":
            for pid in pids:
                proj = self.projects[pid]
                for path, content in self.files.get((pid, proj["default_branch"]), {}).items():
                    if not isinstance(content, str):
                        continue
                    for number, line in enumerate(content.split("\n"), 1):
                        if needle in line.lower():
                            rows.append(
                                {
                                    "basename": path.rsplit("/", 1)[-1],
                                    "data": line + "\n",
                                    "path": path,
                                    "filename": path,
                                    "id": None,
                                    "ref": proj["default_branch"],
                                    "startline": number,
                                    "project_id": pid,
                                }
                            )
                            break
        elif scope == "commits":
            rows = [c for pid in pids for c in self.commits[pid] if needle in c["title"].lower()]
        return self._page(rows, q)

    def _create_label(self, m, q, j) -> _Response:
        project = self._p(m)
        if not (j or {}).get("name"):
            return _Response(400, {"error": "name is missing"})
        label = {
            "id": self._id("label"),
            "name": j["name"],
            "color": j.get("color", "#000000"),
            "description": j.get("description"),
        }
        self.labels.setdefault(project["id"], []).append(label)
        return _Response(201, label)

    def _tree(self, m, q, j) -> _Response:
        project = self._p(m)
        ref = q.get("ref") or project["default_branch"]
        tree = self.files.get((project["id"], ref))
        if tree is None:
            return _Response(404, {"message": "404 Tree Not Found"})
        prefix = str(q.get("path") or "").strip("/")
        recursive = str(q.get("recursive", "")).lower() in {"true", "1"}
        entries: Dict[str, Dict[str, Any]] = {}
        for path in sorted(tree):
            if prefix and not path.startswith(prefix + "/"):
                continue
            rest = path[len(prefix) + 1 :] if prefix else path
            parts = rest.split("/")
            if len(parts) > 1 and not recursive:
                name = parts[0]
                entries.setdefault(
                    name,
                    {
                        "id": _sha(name),
                        "name": name,
                        "type": "tree",
                        "path": (prefix + "/" if prefix else "") + name,
                        "mode": "040000",
                    },
                )
            else:
                entries[rest] = {"id": _sha(path), "name": parts[-1], "type": "blob", "path": path, "mode": "100644"}
        return self._page(list(entries.values()), q)

    def _file(self, m, q, j) -> _Response:
        project = self._p(m)
        ref = q.get("ref") or "HEAD"
        if ref == "HEAD":
            ref = project["default_branch"]
        path = unquote(m.group("file"))
        tree = self.files.get((project["id"], ref), {})
        if path not in tree:
            return _Response(404, {"message": "404 File Not Found"})
        content = tree[path]
        raw = content.encode("utf-8") if isinstance(content, str) else content
        head = self.branches[project["id"]].get(ref, {}).get("commit", {}).get("id", ref)
        return _Response(
            200,
            {
                "file_name": path.rsplit("/", 1)[-1],
                "file_path": path,
                "size": len(raw),
                "encoding": "base64",
                "content": base64.b64encode(raw).decode("ascii"),
                "content_sha256": hashlib.sha256(raw).hexdigest(),
                "ref": ref,
                "blob_id": _sha(path + ref),
                "commit_id": head,
                "last_commit_id": head,
                "execute_filemode": False,
            },
        )

    def _commits(self, m, q, j) -> _Response:
        project = self._p(m)
        ref = q.get("ref_name") or project["default_branch"]
        rows = [
            c for c in self.commits[project["id"]] if str(q.get("all", "")).lower() == "true" or c.get("_branch") == ref
        ]
        if q.get("path"):
            rows = [
                c
                for c in rows
                if any(
                    d.get("new_path", "").startswith(q["path"])
                    for d in self.commit_diffs.get((project["id"], c["id"]), [])
                )
            ]
        if q.get("author"):
            rows = [c for c in rows if str(q["author"]).lower() in c["author_name"].lower()]
        return self._page([{k: v for k, v in c.items() if not k.startswith("_")} for c in rows], q, total_header=False)

    def _commit(self, m, q, j) -> _Response:
        project = self._p(m)
        sha = unquote(m.group("sha"))
        for c in self.commits[project["id"]]:
            if c["id"].startswith(sha):
                return _Response(200, {k: v for k, v in c.items() if not k.startswith("_")})
        return _Response(404, {"message": "404 Commit Not Found"})

    def _compare(self, m, q, j) -> _Response:
        project = self._p(m)
        frm, to = str(q.get("from")), str(q.get("to"))
        for ref in (frm, to):
            if ref not in self.branches[project["id"]] and not any(
                c["id"].startswith(ref) for c in self.commits[project["id"]]
            ):
                return _Response(404, {"message": "404 Ref Not Found"})
        commits = [c for c in self.commits[project["id"]] if c.get("_branch") == to and c.get("_branch") != frm]
        diffs = [d for c in commits for d in self.commit_diffs.get((project["id"], c["id"]), [])]
        return _Response(
            200,
            {
                "commit": commits[0] if commits else None,
                "commits": commits,
                "diffs": diffs,
                "compare_timeout": False,
                "compare_same_ref": frm == to,
                "web_url": f"{project['web_url']}/-/compare/{frm}...{to}",
            },
        )

    def _branch(self, m, q, j) -> _Response:
        project = self._p(m)
        branch = self.branches[project["id"]].get(unquote(m.group("branch")))
        if not branch:
            return _Response(404, {"message": "404 Branch Not Found"})
        return _Response(200, dict(branch))

    def _tags(self, pid: int) -> List[Dict[str, Any]]:
        head = self.branches[pid].get(self.projects[pid]["default_branch"])
        if not head:
            return []
        return [
            {
                "name": "v1.0.0",
                "message": "Release 1.0.0",
                "target": head["commit"]["id"],
                "protected": False,
                "commit": head["commit"],
                "release": {"tag_name": "v1.0.0", "description": "first"},
            }
        ]

    def _create_commit(self, m, q, j) -> _Response:
        project = self._p(m)
        pid = project["id"]
        j = j or {}
        for key in ("branch", "commit_message", "actions"):
            if not j.get(key):
                return _Response(400, {"error": f"{key} is missing"})
        branch = j["branch"]
        if branch not in self.branches[pid]:
            start = j.get("start_branch")
            if not start or start not in self.branches[pid]:
                return _Response(400, {"message": "You can only create or edit files when you are on a branch"})
            self.branches[pid][branch] = dict(self.branches[pid][start], name=branch, default=False, protected=False)
            self.files[(pid, branch)] = dict(self.files.get((pid, start), {}))
        tree = dict(self.files.get((pid, branch), {}))
        adds = dels = 0
        for action in j["actions"]:
            kind, path = action.get("action"), action.get("file_path")
            if kind == "create":
                if path in tree:
                    return _Response(400, {"message": "A file with this name already exists"})
                tree[path] = action.get("content", "")
                adds += 1
            elif kind == "update":
                if path not in tree:
                    return _Response(400, {"message": "A file with this name doesn't exist"})
                tree[path] = action.get("content", "")
                adds += 1
                dels += 1
            elif kind == "delete":
                if path not in tree:
                    return _Response(400, {"message": "A file with this name doesn't exist"})
                del tree[path]
                dels += 1
            elif kind == "move":
                prev = action.get("previous_path")
                if prev not in tree:
                    return _Response(400, {"message": "A file with this name doesn't exist"})
                tree[path] = action.get("content") if action.get("content") is not None else tree[prev]
                del tree[prev]
            elif kind == "chmod":
                if path not in tree:
                    return _Response(400, {"message": "A file with this name doesn't exist"})
            else:
                return _Response(400, {"error": "actions[0][action] does not have a valid value"})
        files = {k: v for k, v in tree.items()}
        removed = {k: None for k in self.files.get((pid, branch), {}) if k not in tree}
        files.update(removed)
        commit = self.add_commit(pid, branch, j["commit_message"].split("\n")[0], files)
        commit["stats"] = {"additions": adds, "deletions": dels, "total": adds + dels}
        if j.get("author_name"):
            commit["author_name"] = j["author_name"]
        return _Response(201, {k: v for k, v in commit.items() if not k.startswith("_")})

    def _issue_list(self, project: Optional[Dict[str, Any]], q: Dict[str, Any]) -> _Response:
        pids = [project["id"]] if project else list(self.projects)
        rows = [i for pid in pids for i in self.issues[pid].values()]
        state = q.get("state")
        if state and state != "all":
            rows = [i for i in rows if i["state"] == state]
        if q.get("labels"):
            wanted = set(str(q["labels"]).split(","))
            rows = [i for i in rows if wanted <= set(i["labels"])]
        if q.get("assignee_username"):
            rows = [i for i in rows if any(a["username"] == q["assignee_username"] for a in i["assignees"])]
        if q.get("assignee_id") == "None":
            rows = [i for i in rows if not i["assignees"]]
        elif q.get("assignee_id") == "Any":
            rows = [i for i in rows if i["assignees"]]
        elif q.get("assignee_id") not in (None, ""):
            rows = [i for i in rows if any(a["id"] == int(q["assignee_id"]) for a in i["assignees"])]
        if q.get("author_username"):
            rows = [i for i in rows if i["author"]["username"] == q["author_username"]]
        if q.get("search"):
            rows = [
                i for i in rows if str(q["search"]).lower() in (i["title"] + " " + (i["description"] or "")).lower()
            ]
        if q.get("milestone"):
            rows = [i for i in rows if (i.get("milestone") or {}).get("title") == q["milestone"]]
        if q.get("iids[]"):
            wanted_iids = {int(x) for x in (q["iids[]"] if isinstance(q["iids[]"], list) else [q["iids[]"]])}
            rows = [i for i in rows if i["iid"] in wanted_iids]
        if q.get("confidential") is not None:
            want = str(q["confidential"]).lower() in {"true", "1"}
            rows = [i for i in rows if bool(i["confidential"]) == want]
        return self._page(rows, q)

    def _one(self, project: Dict[str, Any], kind: str, iid: int) -> _Response:
        table = self.issues if kind == "issues" else self.mrs
        obj = table[project["id"]].get(iid)
        if obj is None:
            return _Response(404, {"message": "404 Issue Not Found" if kind == "issues" else "404 Not found"})
        return _Response(200, dict(obj))

    def _issue_create(self, m, q, j) -> _Response:
        project = self._p(m)
        j = j or {}
        if not j.get("title"):
            return _Response(400, {"error": "title is missing"})
        if len(j["title"]) > 255:
            return _Response(400, {"message": {"title": ["is too long (maximum is 255 characters)"]}})
        iid = max(self.issues[project["id"]] or [0]) + 1
        issue = self.add_issue(project["id"], iid, j["title"], description=j.get("description"))
        self._apply_issue_fields(project, issue, j)
        return _Response(201, dict(issue))

    def _apply_issue_fields(
        self, project: Dict[str, Any], obj: Dict[str, Any], j: Dict[str, Any]
    ) -> Optional[_Response]:
        if "labels" in j:
            obj["labels"] = [x for x in str(j["labels"]).split(",") if x]
        if j.get("add_labels"):
            obj["labels"] = sorted(set(obj["labels"]) | set(str(j["add_labels"]).split(",")))
        if j.get("remove_labels"):
            obj["labels"] = [x for x in obj["labels"] if x not in str(j["remove_labels"]).split(",")]
        if "assignee_ids" in j:
            obj["assignees"] = [self.user(i) for i in j["assignee_ids"] if i in self.users]
        if "reviewer_ids" in j:
            obj["reviewers"] = [self.user(i) for i in j["reviewer_ids"] if i in self.users]
        if "milestone_id" in j:
            ms = next((x for x in self.milestones.get(project["id"], []) if x["id"] == j["milestone_id"]), None)
            obj["milestone"] = ms
        for key in (
            "title",
            "description",
            "due_date",
            "confidential",
            "discussion_locked",
            "target_branch",
            "squash",
            "issue_type",
        ):
            if key in j:
                obj[key] = j[key]
        if j.get("state_event") == "close":
            obj["state"] = "closed"
            obj["closed_at"] = self._tick()
        elif j.get("state_event") == "reopen":
            obj["state"] = "opened"
            obj["closed_at"] = None
        if "title" in j and "draft" in obj:
            obj["draft"] = obj["work_in_progress"] = obj["title"].lower().startswith("draft:")
        obj["updated_at"] = self._tick()
        return None

    def _issue_update(self, m, q, j) -> _Response:
        project = self._p(m)
        issue = self.issues[project["id"]].get(int(m.group("iid")))
        if issue is None:
            return _Response(404, {"message": "404 Issue Not Found"})
        self._apply_issue_fields(project, issue, j or {})
        return _Response(200, dict(issue))

    def _discussions(self, project: Dict[str, Any], kind: str, iid: int, q: Dict[str, Any]) -> _Response:
        if iid not in (self.issues if kind == "issues" else self.mrs)[project["id"]]:
            return _Response(404, {"message": "404 Not found"})
        return self._page([dict(d) for d in self.discussions.get((project["id"], kind, iid), [])], q)

    def _new_note(self, project: Dict[str, Any], kind: str, iid: int, j: Any) -> _Response:
        table = self.issues if kind == "issues" else self.mrs
        if iid not in table[project["id"]]:
            return _Response(404, {"message": "404 Not found"})
        if not (j or {}).get("body"):
            return _Response(400, {"error": "body is missing"})
        note = self._note(self.current_user_id, j["body"], internal=bool(j.get("internal")))
        self.add_discussion((project["id"], kind, iid), [note], individual=True)
        table[project["id"]][iid]["user_notes_count"] += 1
        return _Response(201, dict(note))

    def _new_discussion(self, project: Dict[str, Any], kind: str, iid: int, j: Any) -> _Response:
        table = self.issues if kind == "issues" else self.mrs
        obj = table[project["id"]].get(iid)
        if obj is None:
            return _Response(404, {"message": "404 Not found"})
        j = j or {}
        if not j.get("body"):
            return _Response(400, {"error": "body is missing"})
        position = j.get("position")
        if position:
            for key in ("base_sha", "start_sha", "head_sha", "position_type"):
                if not position.get(key):
                    return _Response(400, {"error": f"position[{key}] is missing"})
            if position["head_sha"] != obj["sha"]:
                return _Response(
                    400,
                    {
                        "message": '400 Bad request - Note {:line_code=>["can\'t be blank", "must be a valid line code"]}'
                    },
                )
            if kind == "merge_requests" and not self._line_code_valid(project["id"], iid, position):
                return _Response(
                    400,
                    {
                        "message": '400 Bad request - Note {:line_code=>["can\'t be blank", "must be a valid line code"]}'
                    },
                )
        note = self._note(self.current_user_id, j["body"], resolvable=kind == "merge_requests", position=position)
        discussion = self.add_discussion((project["id"], kind, iid), [note])
        obj["user_notes_count"] += 1
        return _Response(201, dict(discussion))

    def _line_code_valid(self, pid: int, iid: int, position: Dict[str, Any]) -> bool:
        """GitLab resolves a diff note by matching (old_line, new_line) against the diff: an added
        line needs new_line only, a removed line old_line only, an unchanged line both."""
        item = next(
            (
                d
                for d in self.mr_diffs.get((pid, iid), [])
                if d.get("new_path") == position.get("new_path") or d.get("old_path") == position.get("old_path")
            ),
            None,
        )
        if item is None:
            return False
        want = (position.get("old_line"), position.get("new_line"))
        old_no = new_no = None
        for raw in (item.get("diff") or "").split("\n"):
            header = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if header:
                old_no, new_no = int(header.group(1)), int(header.group(2))
                continue
            if old_no is None or not raw or raw.startswith("\\"):
                continue
            if raw[0] == "+":
                pair = (None, new_no)
                new_no += 1
            elif raw[0] == "-":
                pair = (old_no, None)
                old_no += 1
            else:
                pair = (old_no, new_no)
                old_no += 1
                new_no += 1
            if pair == want:
                return True
        return False

    def _reply(self, project: Dict[str, Any], kind: str, iid: int, did: str, j: Any) -> _Response:
        for discussion in self.discussions.get((project["id"], kind, iid), []):
            if discussion["id"] == did:
                note = self._note(
                    self.current_user_id,
                    (j or {}).get("body", ""),
                    resolvable=any(n.get("resolvable") for n in discussion["notes"]),
                )
                discussion["notes"].append(note)
                return _Response(201, dict(note))
        return _Response(404, {"message": "404 Discussion Not Found"})

    def _resolve(self, m, q, j) -> _Response:
        project = self._p(m)
        for discussion in self.discussions.get((project["id"], "merge_requests", int(m.group("iid"))), []):
            if discussion["id"] == m.group("did"):
                resolved = bool((j or {}).get("resolved"))
                for note in discussion["notes"]:
                    if note.get("resolvable"):
                        note["resolved"] = resolved
                        note["resolved_by"] = self.user(self.current_user_id) if resolved else None
                return _Response(200, dict(discussion))
        return _Response(404, {"message": "404 Discussion Not Found"})

    def _mr_list(self, project: Optional[Dict[str, Any]], q: Dict[str, Any]) -> _Response:
        pids = [project["id"]] if project else list(self.projects)
        rows = [x for pid in pids for x in self.mrs[pid].values()]
        state = q.get("state")
        if state and state != "all":
            rows = [x for x in rows if x["state"] == state]
        if q.get("labels"):
            wanted = set(str(q["labels"]).split(","))
            rows = [x for x in rows if wanted <= set(x["labels"])]
        if q.get("author_username"):
            rows = [x for x in rows if x["author"]["username"] == q["author_username"]]
        if q.get("reviewer_username"):
            rows = [x for x in rows if any(r["username"] == q["reviewer_username"] for r in x["reviewers"])]
        if q.get("assignee_id") not in (None, ""):
            rows = [x for x in rows if any(a["id"] == int(q["assignee_id"]) for a in x["assignees"])]
        for key in ("source_branch", "target_branch"):
            if q.get(key):
                rows = [x for x in rows if x[key] == q[key]]
        if q.get("search"):
            rows = [x for x in rows if str(q["search"]).lower() in x["title"].lower()]
        if q.get("wip"):
            rows = [x for x in rows if x["draft"] == (q["wip"] == "yes")]
        if q.get("iids[]"):
            wanted_iids = {int(x) for x in (q["iids[]"] if isinstance(q["iids[]"], list) else [q["iids[]"]])}
            rows = [x for x in rows if x["iid"] in wanted_iids]
        return self._page(rows, q)

    def _mr_create(self, m, q, j) -> _Response:
        project = self._p(m)
        j = j or {}
        for key in ("source_branch", "target_branch", "title"):
            if not j.get(key):
                return _Response(400, {"error": f"{key} is missing"})
        if j["source_branch"] not in self.branches[project["id"]]:
            return _Response(400, {"message": ["Source branch not found"]})
        if j["target_branch"] not in self.branches[project["id"]]:
            return _Response(400, {"message": ["Target branch not found"]})
        for mr in self.mrs[project["id"]].values():
            if (
                mr["state"] == "opened"
                and mr["source_branch"] == j["source_branch"]
                and mr["target_branch"] == j["target_branch"]
            ):
                return _Response(
                    409,
                    {"message": [f"Another open merge request already exists for this source branch: !{mr['iid']}"]},
                )
        iid = max(self.mrs[project["id"]] or [0]) + 1
        mr = self.add_mr(
            project["id"], iid, j["title"], j["source_branch"], j["target_branch"], description=j.get("description")
        )
        mr["author"] = self.user(self.current_user_id)
        if j.get("remove_source_branch") is not None:
            mr["force_remove_source_branch"] = bool(j["remove_source_branch"])
        self._apply_issue_fields(project, mr, j)
        return _Response(201, dict(mr))

    def _mr_update(self, m, q, j) -> _Response:
        project = self._p(m)
        mr = self.mrs[project["id"]].get(int(m.group("iid")))
        if mr is None:
            return _Response(404, {"message": "404 Not found"})
        j = dict(j or {})
        if "remove_source_branch" in j:
            mr["force_remove_source_branch"] = bool(j.pop("remove_source_branch"))
        self._apply_issue_fields(project, mr, j)
        return _Response(200, dict(mr))

    def _approvals(self, project: Dict[str, Any], iid: int) -> _Response:
        mr = self.mrs[project["id"]].get(iid)
        if mr is None:
            return _Response(404, {"message": "404 Not found"})
        state = self.approvals[(project["id"], iid)]
        approved_by = [{"user": self.user(uid)} for uid in state["approved_by"]]
        left = max(0, state["required"] - len(approved_by))
        return _Response(
            200,
            {
                "id": mr["id"],
                "iid": iid,
                "project_id": project["id"],
                "title": mr["title"],
                "state": mr["state"],
                "approved": left == 0,
                "approvals_required": state["required"],
                "approvals_left": left,
                "approved_by": approved_by,
                "user_has_approved": self.current_user_id in state["approved_by"],
                "user_can_approve": self.current_user_id not in state["approved_by"],
            },
        )

    def _approve(self, m, q, j) -> _Response:
        project = self._p(m)
        iid = int(m.group("iid"))
        mr = self.mrs[project["id"]].get(iid)
        if mr is None:
            return _Response(404, {"message": "404 Not found"})
        if (j or {}).get("sha") and j["sha"] != mr["sha"]:
            return _Response(409, {"message": "SHA does not match HEAD of source branch"})
        state = self.approvals[(project["id"], iid)]
        if self.current_user_id not in state["approved_by"]:
            state["approved_by"].append(self.current_user_id)
        return self._approvals(project, iid)

    def _unapprove(self, m, q, j) -> _Response:
        project = self._p(m)
        iid = int(m.group("iid"))
        state = self.approvals.get((project["id"], iid))
        if state is None:
            return _Response(404, {"message": "404 Not found"})
        if self.current_user_id in state["approved_by"]:
            state["approved_by"].remove(self.current_user_id)
        return self._approvals(project, iid)

    def _merge(self, m, q, j) -> _Response:
        project = self._p(m)
        mr = self.mrs[project["id"]].get(int(m.group("iid")))
        if mr is None:
            return _Response(404, {"message": "404 Not found"})
        j = j or {}
        if mr["state"] != "opened":
            return _Response(405, {"message": "405 Method Not Allowed"})
        if j.get("sha") and j["sha"] != mr["sha"]:
            return _Response(409, {"message": "409 Conflict: SHA does not match HEAD of source branch"})
        if mr["has_conflicts"] or not self.mergeable or mr["draft"]:
            return _Response(422, {"message": "Branch cannot be merged"})
        if j.get("merge_when_pipeline_succeeds") or j.get("auto_merge"):
            mr["merge_when_pipeline_succeeds"] = True
            return _Response(200, dict(mr))
        merge_sha = _sha(f"merge:{mr['iid']}")
        mr.update(
            {
                "state": "merged",
                "merged_at": self._tick(),
                "merged_by": self.user(self.current_user_id),
                "merge_commit_sha": merge_sha,
                "detailed_merge_status": "not_open",
            }
        )
        commit = self._commit_obj(
            project["id"],
            merge_sha,
            f"Merge branch '{mr['source_branch']}' into '{mr['target_branch']}'",
            [self.head(project["id"], mr["target_branch"]), mr["sha"]],
        )
        commit["_branch"] = mr["target_branch"]
        self.commits[project["id"]].insert(0, commit)
        self.branches[project["id"]][mr["target_branch"]] = self._branch_obj(project["id"], mr["target_branch"], commit)
        if j.get("should_remove_source_branch"):
            self.branches[project["id"]].pop(mr["source_branch"], None)
        return _Response(200, dict(mr))

    def _pipeline_list(self, m, q, j) -> _Response:
        project = self._p(m)
        rows = sorted(self.pipelines[project["id"]].values(), key=lambda p: p["id"], reverse=True)
        for key in ("status", "ref", "sha", "source"):
            if q.get(key):
                rows = [p for p in rows if p[key] == q[key]]
        if q.get("username"):
            rows = [p for p in rows if p["user"]["username"] == q["username"]]
        return self._page(rows, q)

    def _pipeline_latest(self, m, q, j) -> _Response:
        project = self._p(m)
        ref = q.get("ref") or project["default_branch"]
        rows = [p for p in self.pipelines[project["id"]].values() if p["ref"] == ref]
        if not rows:
            return _Response(403, {"message": "403 Forbidden"})
        return _Response(200, dict(max(rows, key=lambda p: p["id"])))

    def _pipeline(self, project: Dict[str, Any], pipeline_id: int) -> _Response:
        pipeline = self.pipelines[project["id"]].get(pipeline_id)
        if pipeline is None:
            return _Response(404, {"message": "404 Not found"})
        return _Response(200, dict(pipeline))

    def _pipeline_create(self, m, q, j) -> _Response:
        project = self._p(m)
        j = j or {}
        if not j.get("ref"):
            return _Response(400, {"error": "ref is missing"})
        if j["ref"] not in self.branches[project["id"]]:
            return _Response(400, {"message": {"base": ["Reference not found"]}})
        pipeline = self.add_pipeline(project["id"], j["ref"], "pending", source="api", variables=j.get("variables"))
        return _Response(201, dict(pipeline))

    def _filter_jobs(self, rows: List[Dict[str, Any]], q: Dict[str, Any]) -> List[Dict[str, Any]]:
        scope = q.get("scope[]")
        if scope:
            wanted = set(scope if isinstance(scope, list) else [scope])
            rows = [x for x in rows if x["status"] in wanted]
        if str(q.get("include_retried", "")).lower() not in {"true", "1"}:
            rows = [x for x in rows if not x.get("retried")]
        return rows

    def _pipeline_jobs(self, m, q, j) -> _Response:
        project = self._p(m)
        pipeline_id = int(m.group("pid"))
        if pipeline_id not in self.pipelines[project["id"]]:
            return _Response(404, {"message": "404 Not found"})
        rows = [x for x in self.jobs[project["id"]].values() if x["pipeline"]["id"] == pipeline_id]
        return self._page(self._filter_jobs(rows, q), q)

    def _pipeline_op(self, m, q, j) -> _Response:
        project = self._p(m)
        pipeline = self.pipelines[project["id"]].get(int(m.group("pid")))
        if pipeline is None:
            return _Response(404, {"message": "404 Not found"})
        pipeline["status"] = "pending" if m.group("op") == "retry" else "canceled"
        return _Response(201 if m.group("op") == "retry" else 200, dict(pipeline))

    def _job(self, project: Dict[str, Any], job_id: int) -> _Response:
        job = self.jobs[project["id"]].get(job_id)
        if job is None:
            return _Response(404, {"message": "404 Not found"})
        return _Response(200, dict(job))

    def _trace(self, m, q, j) -> _Response:
        project = self._p(m)
        job_id = int(m.group("jid"))
        if job_id not in self.jobs[project["id"]]:
            return _Response(404, {"message": "404 Not found"}, text=json.dumps({"message": "404 Not found"}))
        return _Response(200, None, {"Content-Type": "text/plain"}, text=self.traces.get(job_id, ""))

    def _job_op(self, m, q, j) -> _Response:
        project = self._p(m)
        job = self.jobs[project["id"]].get(int(m.group("jid")))
        if job is None:
            return _Response(404, {"message": "404 Not found"})
        op = m.group("op")
        if op == "play" and job["status"] != "manual":
            return _Response(400, {"message": "400 Bad request - Unplayable Job"})
        if op == "retry":
            job["retried"] = True
            new = dict(job, id=self._id("job"), status="pending", retried=False, failure_reason=None)
            self.jobs[project["id"]][new["id"]] = new
            return _Response(201, new)
        job["status"] = "canceled" if op == "cancel" else "pending"
        return _Response(200 if op == "cancel" else 201, dict(job))


class _NotFound(Exception):
    pass


def _wrap_not_found(fake: FakeGitLab) -> None:
    original = fake.request

    def request(*args: Any, **kwargs: Any) -> _Response:
        try:
            return original(*args, **kwargs)
        except _NotFound as exc:
            return _Response(404, {"message": str(exc)})

    fake.request = request  # type: ignore[method-assign]


SECRET_LINE = "glpat-SECRETsecretSECRET1234567"
FAILED_TRACE = (
    "\x1b[0KRunning with gitlab-runner 17.4.0\x1b[0;m\n"
    "section_start:1725870000:prepare_executor\r\x1b[0KPreparing the docker executor\n"
    "Using Docker executor with image python:3.12\n"
    "section_end:1725870005:prepare_executor\r\x1b[0K"
    "section_start:1725870006:step_script\r\x1b[0K\x1b[32;1m$ pytest -q\x1b[0;m\n"
    "collected 42 items\n"
    "Downloading 10%\rDownloading 55%\rDownloading 100%\n"
    "tests/test_login.py::test_token_refresh FAILED\n"
    "E   AssertionError: expected 200 got 401\n"
    "E   token was " + SECRET_LINE + "\n"
    "DB_PASSWORD=hunter2hunter2 exported for the run\n"
    "\x1b[31;1mERROR: Job failed: exit code 1\x1b[0;m\n"
    "section_end:1725870020:step_script\r\x1b[0K"
)


def seeded() -> FakeGitLab:
    """Three projects, a repo with history on two branches, issues, a reviewed MR, and CI."""
    gl = FakeGitLab()
    _wrap_not_found(gl)
    gl.users = {
        1: {
            "id": 1,
            "username": "hermes-bot",
            "name": "Hermes Bot",
            "state": "active",
            "web_url": f"{gl.base_url}/hermes-bot",
            "is_admin": False,
            "bot": True,
            "email": "bot@example.com",
        },
        2: {
            "id": 2,
            "username": "andrew",
            "name": "Andrew Vieyra",
            "state": "active",
            "web_url": f"{gl.base_url}/andrew",
        },
        3: {"id": 3, "username": "dana", "name": "Dana Dev", "state": "active", "web_url": f"{gl.base_url}/dana"},
    }
    gl.token_info = {
        "id": 7,
        "name": "hermes",
        "revoked": False,
        "created_at": "2026-01-01T00:00:00Z",
        "scopes": ["api"],
        "user_id": 1,
        "last_used_at": None,
        "active": True,
        "expires_at": "2027-01-01",
    }
    api = gl.add_project(1, "platform/api", description="The API service")
    gl.add_project(2, "platform/web", description="The web front end")
    gl.add_project(3, "andrew/sandbox", description="Personal sandbox")
    gl.add_commit(2, "main", "Initial web import", {"index.html": "<html></html>\n"})
    gl.add_commit(3, "main", "Initial sandbox import", {"notes.md": "# notes\n"})
    gl.add_commit(
        1,
        "main",
        "Initial import",
        {
            "README.md": "# api\n\nThe API service.\n",
            "src/app.py": "import os\n\nTOKEN = os.environ['TOKEN']\n\ndef main():\n    return 'ok'\n",
            "docs/guide.md": "# Guide\n" + "\n".join(f"line {i}" for i in range(1, 60)) + "\n",
            "bin/data.bin": b"\x00\xff\xfebinary",
        },
    )
    gl.add_commit(
        1,
        "main",
        "Add health endpoint",
        {"src/health.py": "def health():\n    return {'status': 'ok'}\n"},
        diffs=[
            {
                "old_path": "src/health.py",
                "new_path": "src/health.py",
                "a_mode": "0",
                "b_mode": "100644",
                "new_file": True,
                "renamed_file": False,
                "deleted_file": False,
                "diff": "@@ -0,0 +1,2 @@\n+def health():\n+    return {'status': 'ok'}\n",
            }
        ],
    )
    gl.add_commit(
        1,
        "main",
        "Document health endpoint",
        {"README.md": "# api\n\nThe API service.\n\nGET /health\n"},
        diffs=[
            {
                "old_path": "README.md",
                "new_path": "README.md",
                "a_mode": "100644",
                "b_mode": "100644",
                "new_file": False,
                "renamed_file": False,
                "deleted_file": False,
                "diff": "@@ -1,3 +1,5 @@\n # api\n \n The API service.\n+\n+GET /health\n",
            }
        ],
    )
    main_files = gl.files[(1, "main")]
    gl.files[(1, "feature/login")] = dict(main_files)
    gl.branches[1]["feature/login"] = dict(gl.branches[1]["main"], name="feature/login", default=False, protected=False)
    gl.add_commit(
        1,
        "feature/login",
        "Add login handler",
        {"src/login.py": "def login(user, password):\n    return password == 'x'\n"},
        diffs=[
            {
                "old_path": "src/login.py",
                "new_path": "src/login.py",
                "a_mode": "0",
                "b_mode": "100644",
                "new_file": True,
                "renamed_file": False,
                "deleted_file": False,
                "diff": "@@ -0,0 +1,2 @@\n+def login(user, password):\n+    return password == 'x'\n",
            }
        ],
    )
    gl.add_commit(
        1,
        "feature/login",
        "Wire login route",
        {
            "src/app.py": "import os\nfrom login import login\n\nTOKEN = os.environ['TOKEN']\n\ndef main():\n    return 'ok'\n"
        },
        diffs=[
            {
                "old_path": "src/app.py",
                "new_path": "src/app.py",
                "a_mode": "100644",
                "b_mode": "100644",
                "new_file": False,
                "renamed_file": False,
                "deleted_file": False,
                "diff": "@@ -1,4 +1,5 @@\n import os\n+from login import login\n \n TOKEN = os.environ['TOKEN']\n \n",
            }
        ],
    )
    gl.files[(1, "old-feature")] = dict(main_files)
    gl.branches[1]["old-feature"] = dict(
        gl.branches[1]["main"], name="old-feature", default=False, protected=False, merged=True
    )
    gl.milestones[1] = [
        {
            "id": 5,
            "iid": 1,
            "project_id": 1,
            "title": "v1.0",
            "state": "active",
            "due_date": "2026-12-31",
            "start_date": None,
            "web_url": f"{api['web_url']}/-/milestones/1",
        }
    ]
    gl.labels[1] = [{"id": 21, "name": "bug", "color": "#ff0000"}, {"id": 22, "name": "backend", "color": "#00ff00"}]
    gl.releases[1] = [
        {"tag_name": "v1.0.0", "name": "1.0.0", "description": "first", "created_at": "2026-01-01T00:00:00Z"}
    ]
    gl.variables[1] = [{"key": "DEPLOY_KEY", "value": "supersecret", "masked": True}]
    bug = gl.add_issue(
        1,
        1,
        "Login fails with 401 after token refresh",
        labels=["bug", "backend"],
        assignees=[gl.user(2)],
        milestone=gl.milestones[1][0],
    )
    gl.add_issue(1, 2, "Old closed issue", state="closed", closed_at=gl._tick())
    gl.add_issue(
        1,
        3,
        "Confidential security report",
        confidential=True,
        labels=["security"],
        description="Leaked token " + SECRET_LINE + " in prod logs",
    )
    gl.add_issue(3, 1, "Sandbox todo", author=gl.user(2))
    gl.add_discussion(
        (1, "issues", 1),
        [
            gl._note(2, "Reproduced on staging. Stack trace attached."),
            gl._note(3, "Looks like the refresh path drops the header."),
        ],
    )
    gl.add_discussion((1, "issues", 1), [gl._note(2, "added ~bug label", system=True)], individual=True)
    bug["user_notes_count"] = 2
    mr = gl.add_mr(
        1,
        10,
        "Add login handler",
        "feature/login",
        "main",
        description="Closes #1",
        labels=["backend"],
        reviewers=[gl.user(2)],
    )
    gl.add_mr(
        1,
        11,
        "Old merged change",
        "old-feature",
        "main",
        state="merged",
        merged_at=gl._tick(),
        merged_by=gl.user(2),
        detailed_merge_status="not_open",
    )
    gl.add_mr(1, 12, "Draft: Experimental cache", "feature/login", "main", labels=["experiment"])
    gl.add_mr(2, 1, "Web tweak", "main", "main")
    gl.mr_diffs[(1, 10)] = list(gl.commit_diffs[(1, gl.commits[1][1]["id"])]) + list(
        gl.commit_diffs[(1, gl.commits[1][0]["id"])]
    )
    gl.mr_commits[(1, 10)] = [{k: v for k, v in c.items() if not k.startswith("_")} for c in gl.commits[1][:2]]
    failed = gl.add_pipeline(1, "feature/login", "failed", source="merge_request_event")
    ok = gl.add_pipeline(1, "main", "success")
    gl.mr_pipelines[(1, 10)] = [failed["id"]]
    mr["head_pipeline"] = {
        "id": failed["id"],
        "status": "failed",
        "web_url": failed["web_url"],
        "ref": "feature/login",
        "sha": mr["sha"],
    }
    gl.add_job(1, failed["id"], "build", "build", "success", trace="Building...\nDone.\n")
    gl.add_job(1, failed["id"], "test", "test", "failed", trace=FAILED_TRACE)
    gl.add_job(1, failed["id"], "deploy", "deploy", "manual", trace="")
    gl.add_job(1, failed["id"], "lint", "test", "failed", trace="lint failed\n", allow_failure=True)
    gl.add_job(1, ok["id"], "build", "build", "success", trace="ok\n")
    gl.add_discussion(
        (1, "merge_requests", 10),
        [
            gl._note(
                2,
                "Please hash the password before comparing.",
                resolvable=True,
                position={
                    "base_sha": mr["diff_refs"]["base_sha"],
                    "start_sha": mr["diff_refs"]["start_sha"],
                    "head_sha": mr["sha"],
                    "position_type": "text",
                    "new_path": "src/login.py",
                    "old_path": "src/login.py",
                    "new_line": 2,
                    "old_line": None,
                },
            )
        ],
    )
    resolved = gl.add_discussion(
        (1, "merge_requests", 10),
        [gl._note(2, "Typo in docstring", resolvable=True), gl._note(3, "Fixed", resolvable=True)],
    )
    for note in resolved["notes"]:
        note["resolved"] = True
        note["resolved_by"] = gl.user(2)
    gl.add_discussion((1, "merge_requests", 10), [gl._note(3, "added 1 commit", system=True)], individual=True)
    gl.add_discussion((1, "merge_requests", 10), [gl._note(3, "General remark: nice work.")], individual=True)
    mr["user_notes_count"] = 3
    return gl
