# CLAUDE.md

Context for Claude Code working in this repo.

## What this is

A fork of Google's `codelabs/ai-toolkit-lab-2/hackathon-judge` (originally GKE + Agent Sandbox),
ported to run on **Cloud Run only**, using **Cloud Run sandboxes** (Preview). It is the demo for a
DevFest Jakarta 2026 lightning talk, so the goal is a system that really runs on real submissions
and produces real numbers, not just green unit tests.

Read `README-CLOUDRUN.md` first. It has the architecture, the file list and the "Not verified yet" list.

## Layout that matters

```
agent/src/adapters/outbound/cloudrun_sandbox.py   sandbox CLI wrapper: fetch -> start -> exec -> delete
agent/src/adapters/outbound/cloudrun_judge.py     ADK judge, self_check(), judge_metrics log line
agent/src/main.py                                  AGENT_TYPE=cloudrun (default) picks the adapter above
agent/Dockerfile.cloudrun                          agent image + toolchain the sandboxes borrow
frontend/Dockerfile.cloudrun, nginx.cloudrun.conf  nginx ingress, proxies /api to backend sidecar on 127.0.0.1:8080
cloudrun/web-service.yaml                          Cloud Run service: web (nginx) + api (Go backend sidecar)
cloudrun/cloudbuild.cloudrun.yaml                  builds backend, frontend-cloudrun, agent-cloudrun
cloudrun/deploy-cloudrun.sh, destroy-cloudrun.sh   one-shot deploy / teardown
cloudrun/smoke/                                    sample bad submission + Pub/Sub task for end-to-end tests
k8s/, deploy.sh, agent-sandbox/                    ORIGINAL GKE path. Leave it working, don't delete it
```

## Facts about Cloud Run sandboxes (from the official docs, Oct 2026)

- Enabled per resource with `--sandbox-launcher` (gcloud **beta**). Worker pools support it.
- Binary inside the container: `/usr/local/gcp/bin/sandbox`.
- Commands: `sandbox do` (create, run, delete), `run <id> --detach`, `exec <id>`, `tar`, `fork`, `delete --force`.
- Flags on `do`/`run`: `--allow-egress`, `--write`, `--import-tar`, `--mount`, `-e/--env`, `-w/--workdir`.
  `do` also has `--export-tar`, `--sync-tar`. `exec` only has `-e` and `-w`.
- Egress is **blocked by default**. Sandboxes get no host env vars, no secrets, no metadata server.
- Sandboxes **can read the host container's root file system (read-only)**. Toolchains must be in the image.
- Sandboxes share the instance's CPU and memory. Requires the second-generation execution environment.
- Docs: https://docs.cloud.google.com/run/docs/code-execution and https://docs.cloud.google.com/run/docs/reference/sandbox-cli

## Design invariants. Do not break these, they are the point of the talk

1. The model never runs inside a sandbox. The ADK agent runs on the host and only sends commands in.
2. Egress is allowed only in the fetch phase (clone + install). The judging sandbox stays offline.
3. Nothing secret in any image. No keys, no `.env`, no credential files.
4. One evaluation per instance (`MAX_CONCURRENT_EVALS=1`). Scale with more worker-pool instances.
5. Facts come from code: test results, injection markers, score clamping and the total are computed
   in code, never taken from the model.
6. Text from submissions (files, test output, file names) is wrapped with `wrap_untrusted()` and never
   placed in the trusted part of the prompt.

If a fix seems to require breaking one of these, stop and explain the trade-off instead of doing it.

## Commands

```bash
# Agent tests (run before and after every change to agent/)
cd agent && uv sync && GOOGLE_CLOUD_PROJECT=p TASKS_SUBSCRIPTION=s RESULTS_TOPIC=t USE_MOCK_PUBSUB=true uv run pytest -q

# Deploy everything
GOOGLE_CLOUD_PROJECT=<id> GOOGLE_CLOUD_REGION=<region> ./cloudrun/deploy-cloudrun.sh

# Rebuild only, without redeploying
gcloud builds submit --config=cloudrun/cloudbuild.cloudrun.yaml --substitutions=_IMAGE_BASE=<base>,_TAG=<tag> .

# Open the private web UI locally
gcloud run services proxy hackathon-judge-web --region=<region>
```

## Logs to look for

- `"event": "sandbox_self_check"` at agent startup. `launcher_ok` must be true and
  `egress_blocked_by_default` must be true.
- `"event": "judge_metrics"` once per submission, with fetch/start/verify/agent timings,
  test result and number of injection markers. These are the numbers for the talk.

## Working rules

- Ask before anything destructive or costly: deleting resources, `destroy-cloudrun.sh`,
  `PUBLIC=true`, raising instance counts, changing IAM beyond what the deploy script grants.
- Keep diffs small. Prefer fixing the Cloud Run files over touching the original GKE files.
- When a fix depends on a gcloud flag or CLI behavior, check `--help` or the docs above. Don't guess.
- When you verify something on the "Not verified yet" list in `README-CLOUDRUN.md`, update that list.
- Keep `agent/tests` green. Add a test when you fix a bug in the agent.
