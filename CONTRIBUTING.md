# Contributing

Thanks for helping. This plugin ships as a standalone repository by design: Hermes does not merge
third-party product integrations into its core tree, so this is the place for GitLab work.

## Ground rules

- Keep the engine (`client`, `targets`, `render`, `writes`, `executor`, `store`) free of Hermes
  imports at module load. That is what keeps it testable without a Hermes checkout.
- Every write goes through `executor.perform`. Do not call `client.request` with a non-GET method
  anywhere else.
- A new write action needs: a builder in `writes.py`, its `requires` flags and preconditions, a
  schema entry, a fake-GitLab route, tests for the success, rejection and refusal paths, and a line
  in `docs/tools.md` and the matching row in `docs/api-coverage.md`.
- Handlers return JSON strings and never raise. Everything the model sees goes through `render`.
- Match the existing style: type hints, short docstrings explaining *why*, no dead code.

## Workflow

```bash
git clone https://github.com/andrewvieyra/hermes-plugin-gitlab ~/.hermes/plugins/gitlab
cd ~/.hermes/plugins/gitlab
make test
make lint
make doctor                       # needs the hermes CLI on PATH
HERMES_AGENT_ROOT=~/.hermes/hermes-agent make test-hermes
```

`make test` runs against the fake GitLab in `tests/fake_gitlab.py`. If you need a behaviour the
fake does not model, extend the fake rather than mocking around it; keep it faithful to what GitLab
actually returns (check the [REST API docs](https://docs.gitlab.com/api/rest/)).

To test against a real GitLab, the official `gitlab/gitlab-ce` container is the quickest path.
Point `GITLAB_URL` and `GITLAB_TOKEN` at it and use `/gitlab status` from a Hermes session.

## Pull requests

- One change per PR with a short description of the behaviour, not the diff.
- Add a line under `[Unreleased]` in `CHANGELOG.md`.
- CI must be green: tests on every supported Python, ruff clean.

## Reporting security issues

See [SECURITY.md](SECURITY.md).
