---
description: Deploy the Cloud Run port of Hackathon Judge, fix whatever breaks, and prove it end to end
argument-hint: <gcp-project-id> [region]
---

Goal: get this repo running on Cloud Run only (no GKE), fix whatever stops it, and prove it works
end to end on real submissions. Read `CLAUDE.md` and `README-CLOUDRUN.md` first and follow the
design invariants in CLAUDE.md.

Arguments: $ARGUMENTS
The first argument is the GCP project ID. The second, optional, is the region
(default `asia-southeast2`). If no project ID was given, ask for it before doing anything.

Work in the phases below. After each phase, give me a short report (what you ran, what broke,
what you changed, and why) and wait for my OK before starting the next phase.

## Phase 0. Preflight (read-only)

1. Check `gcloud auth list`, the active project, and that `gcloud`, `bq`, `envsubst` and `uv` exist.
2. Run the agent tests (command in CLAUDE.md). They must be green before anything else.
3. Check that this gcloud version knows the flags we rely on:
   `gcloud beta run worker-pools deploy --help` must list `--sandbox-launcher`, and check
   whether it uses `--instances` or another flag for instance count. Note every mismatch with
   `cloudrun/deploy-cloudrun.sh`.
4. Find out whether Cloud Run sandboxes are available in the chosen region (docs, release notes,
   or a dry-run deploy of a tiny worker pool). If not, propose a supported region.
5. Report and wait.

## Phase 1. Build

1. Fix the flag mismatches from Phase 0 in `cloudrun/deploy-cloudrun.sh`.
2. Run the script up to and including the Cloud Build step only (comment out or guard step 7, or
   run the commands by hand). Fix Dockerfile or build errors you hit.
3. Report and wait.

## Phase 2. Web service (frontend + backend sidecar)

1. Deploy `cloudrun/web-service.yaml`.
2. Through `gcloud run services proxy`, check that `/` serves the React app and `/api/hackathons`
   returns the seeded hackathons as JSON (this proves nginx keeps the `/api` prefix and the backend
   reaches BigQuery).
3. Check backend logs for the Pub/Sub results subscriber starting without errors.
4. Report and wait.

## Phase 3. Agent worker pool

1. Deploy the worker pool with `--sandbox-launcher`.
2. In the logs, find the `sandbox_self_check` line. `launcher_ok` and `egress_blocked_by_default`
   must both be true. If not, debug (execution environment, flags, image contents, binary path).
3. Report and wait.

## Phase 4. End to end

1. Smoke test with a known-bad submission. `cloudrun/smoke/sample-submission/` has a broken
   function, a failing test, a network call, and prompt-injection text. It needs to be a public
   GitHub repo. Ask me whether to create it with `gh repo create --public` or whether I'll push it
   myself, then put the URL into `cloudrun/smoke/task.json`.
2. Create a temporary debug subscription on the results topic, publish `task.json` to the tasks
   topic, and pull the result from the debug subscription.
   Expected: status `success`, tests detected and FAILED, injection markers found in README.md and
   app.py, low scores for Execution and Honesty of Claims, and a `judge_metrics` log line.
3. Full path through the UI or API: `POST /api/projects/<id>/judge` for 2 or 3 seeded projects,
   then confirm evaluations show up via `GET /api/projects/<id>/evaluations`.
4. Delete the debug subscription (ask first).
5. Report and wait.

## Phase 5. Numbers for the talk

Collect every `judge_metrics` line from this session (Cloud Logging) and give me a short table with:
number of submissions judged, median and max of total time and of each phase, how many had tests,
how many tests failed, how many injection markers were found, and any failures with their cause.
Then update the "Not verified yet" list in `README-CLOUDRUN.md` with what is now verified.

## Rules

- Never weaken a design invariant to make something pass. Explain the trade-off and ask instead.
- Ask before deleting resources, making anything public, or increasing cost (instances, CPU, memory).
- Keep `agent/tests` green after every change to `agent/`.
- Commit after each phase with a clear message, but don't push unless I ask.
