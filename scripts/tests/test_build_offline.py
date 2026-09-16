"""Exercise the host workflow without downloading or compiling dependencies."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "build-offline.sh"


class OfflineWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for directory in ("scripts", "backend", "frontend", "skills", "docker/offline", "bin"):
            (self.root / directory).mkdir(parents=True)
        shutil.copy2(SCRIPT, self.root / "scripts/build-offline.sh")
        for file in ("backend/pyproject.toml", "frontend/package.json", "config.example.yaml",
                     "docker/offline/Dockerfile", "docker/offline/Dockerfile.install",
                     "docker/offline/Dockerfile.dockerignore"):
            (self.root / file).write_text("")
        (self.root / "backend/.env").write_text("SECRET")
        (self.root / "frontend/node_modules").mkdir()
        (self.root / "frontend/node_modules/unwanted").write_text("CACHE")
        self.log = self.root / "calls.jsonl"
        docker = self.root / "bin/docker"
        docker.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as f:
    f.write(json.dumps(args) + "\\n")
if args[0] == "build":
    ctx = pathlib.Path(args[-1])
    assert (ctx / "backend/pyproject.toml").exists()
    assert (ctx / "docker/offline/Dockerfile").exists()
    assert not (ctx / "backend/.env").exists()
    assert not (ctx / "frontend/node_modules").exists()
    if os.environ.get("FAIL_BUILD"):
        sys.exit(17)
''')
        docker.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.root / 'bin'}:{os.environ['PATH']}",
                        CALL_LOG=str(self.log))

    def run_script(self, action):
        return subprocess.run(["bash", str(self.root / "scripts/build-offline.sh"), action],
                              env=self.env, capture_output=True, text=True)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_prepare_builds_and_verifies_without_network(self):
        result = self.run_script("prepare")
        self.assertEqual(result.returncode, 0, result.stderr)
        builds = [args for args in self.calls() if args[0] == "build"]
        self.assertEqual(len(builds), 2)
        self.assertIn("--network=none", builds[1])
        self.assertIn("--pull=false", builds[1])
        verify = self.calls()[-1]
        self.assertEqual(verify[0], "run")
        self.assertIn("--network=none", verify)
        self.assertEqual(sum("volume-nocopy" in arg for arg in verify), 3)
        self.assertFalse(Path(builds[0][-1]).exists(), "temporary context leaked")

    def test_failure_stops_before_offline_build_and_verify(self):
        self.env["FAIL_BUILD"] = "1"
        self.assertEqual(self.run_script("prepare").returncode, 17)
        self.assertEqual(len(self.calls()), 1)

    def test_offline_build_never_prepares_resources(self):
        result = self.run_script("build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([args[0] for args in self.calls()], ["image", "build"])
        self.assertIn("--network=none", self.calls()[-1])


if __name__ == "__main__":
    unittest.main()
