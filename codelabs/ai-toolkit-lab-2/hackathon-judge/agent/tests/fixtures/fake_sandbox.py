#!/usr/bin/env python3
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
"""
Local stand-in for /usr/local/gcp/bin/sandbox, for tests only.
NO isolation. It maps the sandbox path /work to $FAKE_SANDBOX_ROOT/work so the
real bash scripts from cloudrun_sandbox.py can be exercised end to end.
Every invocation is logged to $FAKE_SANDBOX_ROOT/calls.jsonl.
"""
import json, os, shutil, subprocess, sys, tarfile

root = os.environ["FAKE_SANDBOX_ROOT"]
work = os.path.join(root, "work")
args = sys.argv[1:]
with open(os.path.join(root, "calls.jsonl"), "a") as f:
    f.write(json.dumps(args) + "\n")

def split(a):
    i = a.index("--")
    return a[:i], a[i + 1:]

def run(cmd):
    cmd = [c.replace("/work", work) for c in cmd]
    return subprocess.run(cmd).returncode

verb = args[0]
if verb == "do":
    flags, cmd = split(args[1:])
    export = next((f.split("=", 1)[1] for f in flags if f.startswith("--export-tar=")), None)
    shutil.rmtree(work, ignore_errors=True)
    rc = run(cmd)
    if export:
        with tarfile.open(export, "w") as t:
            t.add(work, arcname="work")
    sys.exit(rc)
elif verb == "run":
    flags, cmd = split(args[2:])
    imp = next((f.split("=", 1)[1] for f in flags if f.startswith("--import-tar=")), None)
    if imp:
        shutil.rmtree(work, ignore_errors=True)
        with tarfile.open(imp) as t:
            t.extractall(root)
    sys.exit(0)  # pretend the detached `sleep` started
elif verb == "exec":
    _, cmd = split(args[2:])
    sys.exit(run(cmd))
elif verb == "delete":
    sys.exit(0)
sys.exit(2)
