# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# src/adapters/outbound/cloudrun_sandbox.py
"""
Thin async wrapper around the Cloud Run `sandbox` CLI.

Replaces the GKE Agent Sandbox (CRDs, router, gateway, warm pool) with the
sandbox launcher that Cloud Run exposes inside the container itself.

Lifecycle of one submission:

  1. fetch      `sandbox do --allow-egress --write --export-tar`
                Clone the repo and install dependencies. This is the ONLY
                phase with internet access. Its file system is exported to a
                tarball on the host.
  2. evaluate   `sandbox run --write --import-tar --detach`
                A long-lived sandbox WITHOUT egress, seeded from the tarball.
                The tarball is deleted right after the sandbox starts.
  3. exec       `sandbox exec` for every command the harness or the agent runs.
  4. close      `sandbox delete --force`

Things the sandbox can NOT see: host env vars, secrets, the metadata server
(so no service account token). Things it CAN see: the host container's root
file system, read-only. So never bake credentials into the image, and keep
submission data off the host file system for as short as possible.
"""
import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import tempfile
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SANDBOX_BIN = os.getenv("SANDBOX_BIN", "/usr/local/gcp/bin/sandbox")
WORKDIR = "/work"
REPO_DIR = f"{WORKDIR}/repo"
META_DIR = f"{WORKDIR}/.judge"

# Sandboxes don't inherit the host environment, so every script sets its own.
SANDBOX_ENV = (
    "export PATH=/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin:/root/go/bin; "
    "export HOME=/tmp/judge-home; mkdir -p $HOME; "
    "export CI=true GOFLAGS=-mod=mod PIP_DISABLE_PIP_VERSION_CHECK=1; "
)

MAX_OUTPUT_CHARS = int(os.getenv("SANDBOX_MAX_OUTPUT_CHARS", "8000"))

_GITHUB_URL = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+?(\.git)?/?$")


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def as_text(self) -> str:
        return (
            f"exit_code: {self.exit_code}{' (timed out)' if self.timed_out else ''}\n"
            f"--- stdout ---\n{_tail(self.stdout)}\n"
            f"--- stderr ---\n{_tail(self.stderr)}"
        )


def _tail(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} chars truncated ...]\n" + text[-limit:]


def validate_github_url(url: str) -> str:
    """Only plain public GitHub repo URLs. Blocks file://, ssh, option injection."""
    url = url.strip()
    if not _GITHUB_URL.match(url):
        raise ValueError(f"Unsupported repository URL: {url!r}")
    return url


async def _run_cli(args: list[str], timeout: float) -> ExecResult:
    """Run the sandbox CLI on the host. Separate function so tests can mock it."""
    logger.debug("sandbox cli: %s", " ".join(shlex.quote(a) for a in args))
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return ExecResult(proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))
    except asyncio.TimeoutError:
        proc.kill()
        out, err = await proc.communicate()
        return ExecResult(124, out.decode(errors="replace"), err.decode(errors="replace"), timed_out=True)


FETCH_SCRIPT = r"""
set -u
mkdir -p {meta}
git clone --depth 1 --no-tags -- {url} {repo} > {meta}/clone.log 2>&1
echo $? > {meta}/clone.exit
cd {repo} 2>/dev/null || exit 0

# Install dependencies (still has egress). Failures are recorded, not fatal.
install_rc=skipped
if [ -f package.json ]; then
  if [ -f pnpm-lock.yaml ]; then timeout 300 pnpm install --frozen-lockfile; elif [ -f yarn.lock ]; then timeout 300 yarn install --frozen-lockfile; else timeout 300 npm install --no-audit --no-fund; fi
  install_rc=$?
elif [ -f pyproject.toml ] || [ -f requirements.txt ]; then
  python3 -m venv .venv && . .venv/bin/activate
  if [ -f requirements.txt ]; then timeout 300 pip install -q -r requirements.txt; else timeout 300 pip install -q -e . ; fi
  install_rc=$?
  timeout 120 pip install -q pytest >/dev/null 2>&1 || true
elif [ -f go.mod ]; then
  timeout 300 go mod download
  install_rc=$?
fi
echo $install_rc > {meta}/install.exit
"""

# Deterministic verification. Produced by code, never by the model.
VERIFY_SCRIPT = r"""
cd {repo} 2>/dev/null || {{ echo '{{"detected": "none", "reason": "repo missing"}}'; exit 0; }}
[ -f .venv/bin/activate ] && . .venv/bin/activate
runner=none
if [ -f package.json ] && grep -q '"test"' package.json; then runner="npm test"
elif [ -f go.mod ]; then runner="go test ./..."
elif {{ [ -d tests ] || [ -d test ]; }} && command -v pytest >/dev/null; then runner="python -m pytest -q"
elif [ -f Makefile ] && grep -qE '^test:' Makefile; then runner="make test"
fi
if [ "$runner" = none ]; then echo '{{"detected": "none"}}'; exit 0; fi
timeout {timeout} bash -c "$runner" > {meta}/verify.log 2>&1
rc=$?
printf '{{"detected": "%s", "exit_code": %d}}\n' "$runner" "$rc"
"""


class CloudRunSandbox:
    """One isolated sandbox per submission."""

    def __init__(self, task_id: str, ttl_seconds: int = 1800):
        safe_task = re.sub(r"[^a-zA-Z0-9-]", "-", task_id)[:40]
        self.name = f"judge-{safe_task}-{uuid.uuid4().hex[:6]}"
        self.ttl_seconds = ttl_seconds
        self._tmpdir = tempfile.mkdtemp(prefix="judge-")
        self._tar = os.path.join(self._tmpdir, "fetch.tar")
        self.started = False
        self.fetch_output = ""

    # ---------- lifecycle ----------

    async def fetch(self, github_url: str, timeout: float = 600) -> dict:
        """Phase 1: clone + install WITH egress, export the file system."""
        url = validate_github_url(github_url)
        script = SANDBOX_ENV + FETCH_SCRIPT.format(
            url=shlex.quote(url), repo=REPO_DIR, meta=META_DIR
        )
        result = await _run_cli(
            [SANDBOX_BIN, "do", "--allow-egress", "--write",
             f"--export-tar={self._tar}", "--", "/bin/bash", "-c", script],
            timeout=timeout,
        )
        self.fetch_output = _tail(result.as_text(), 1500)
        if result.exit_code != 0:
            logger.warning("Fetch sandbox exited %s for %s:\n%s", result.exit_code, self.name, self.fetch_output)
        if not os.path.exists(self._tar):
            raise RuntimeError(f"Fetch phase produced no workspace. {self.fetch_output}")
        return {"fetch_exit_code": result.exit_code, "timed_out": result.timed_out}

    async def start(self) -> None:
        """Phase 2: offline sandbox seeded from the fetched workspace."""
        result = await _run_cli(
            [SANDBOX_BIN, "run", self.name, "--write", f"--import-tar={self._tar}",
             "--detach", "--", "/bin/sleep", str(self.ttl_seconds)],
            timeout=60,
        )
        if result.exit_code != 0:
            raise RuntimeError(f"Could not start sandbox {self.name}. {result.as_text()}")
        self.started = True
        # Keep submission data off the host file system: other sandboxes on this
        # instance can read the host root (read-only).
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    async def close(self) -> None:
        if self.started:
            await _run_cli([SANDBOX_BIN, "delete", self.name, "--force"], timeout=60)
            self.started = False
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    # ---------- operations ----------

    async def exec(self, command: str, timeout: float = 120) -> ExecResult:
        """Run a shell command inside the offline sandbox, from the repo root."""
        if not self.started:
            raise RuntimeError("Sandbox not started")
        script = (
            SANDBOX_ENV
            # Never fall back to the launcher's cwd (/app): that is the agent's own code.
            + f"cd {REPO_DIR} 2>/dev/null || cd {WORKDIR} 2>/dev/null || exit 97; "
            + "[ -f .venv/bin/activate ] && . .venv/bin/activate; "
            + f"timeout {int(timeout)} /bin/bash -c {shlex.quote(command)}"
        )
        return await _run_cli(
            [SANDBOX_BIN, "exec", self.name, "--", "/bin/bash", "-c", script],
            timeout=timeout + 15,
        )

    async def read_meta(self, name: str) -> str:
        r = await self.exec(f"cat {META_DIR}/{name} 2>/dev/null || true", timeout=15)
        return r.stdout.strip()

    async def verify(self, timeout: int = 300) -> dict:
        """Detect a test runner and run it. Returns facts, not opinions."""
        facts = {
            "clone_exit_code": _to_int(await self.read_meta("clone.exit")),
            "install_exit_code": await self.read_meta("install.exit") or "skipped",
        }
        script = VERIFY_SCRIPT.format(repo=REPO_DIR, meta=META_DIR, timeout=timeout)
        r = await self.exec(script, timeout=timeout + 30)
        try:
            facts["tests"] = json.loads(r.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            facts["tests"] = {"detected": "unknown", "log_tail": _tail(r.stdout, 500)}
        if facts["tests"].get("exit_code") is not None:
            facts["tests"]["log_tail"] = _tail(await self.read_meta("verify.log"), 2000)
        return facts


def _to_int(value: str):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
