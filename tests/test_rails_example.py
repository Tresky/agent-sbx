"""examples/rails/.sandbox/setup.sh against three kinds of Rails application,
with every tool replaced by a stub that logs its arguments. The recipe runs
on Ubuntu's bash 5; the test needs bash 4 or later."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

RECIPE = Path(__file__).resolve().parent.parent / "examples" / "rails" / ".sandbox" / "setup.sh"


def _bash() -> str | None:
    for candidate in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash", shutil.which("bash")):
        if candidate and Path(candidate).exists():
            out = subprocess.run([candidate, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True)
            if out.stdout.strip().isdigit() and int(out.stdout.strip()) >= 4:
                return candidate
    return None


BASH = _bash()

# Each stub appends "<name> <args>" to $LOG. A few answer questions.
STUBS = {
    "gem": 'if [[ "$1 $2" == "list -i" ]]; then exit 1; fi',          # the Bundler version is missing
    "bundle": 'if [[ "$1" == check ]]; then exit 1; fi',                # the gems are not installed
    "nproc": "echo 4",
    "ruby": "echo ruby-stub",
    "node": "echo node-stub",
    "docker": 'if [[ "$1" == ps ]]; then [[ -f "$STATE/$4" ]] && echo id; exit 0; fi\n'
              'if [[ "$1" == run ]]; then touch "$STATE/name=^$4\\$"; fi',
    "yarn": "", "pnpm": "", "npm": "", "corepack": "", "curl": "", "bun": "", "gunzip": "",
}
BASH_ENV = '''
rvm() { echo "rvm $*" >> "$LOG"; if [[ "$1 $2" == "list strings" ]]; then printf 'ruby-3.3.6\\nruby-3.3.10\\nruby-3.4.1\\n'; fi; }
nvm() { echo "nvm $*" >> "$LOG"; }
'''


def lockfile(*gems: str, bundler: str = "2.6.2", ruby: str = "") -> str:
    specs = "".join(f"    {g} (1.0.0)\n" for g in gems)
    tail = f"\nRUBY VERSION\n   ruby {ruby}p0\n" if ruby else ""
    return f"GEM\n  remote: https://rubygems.org/\n  specs:\n{specs}\nBUNDLED WITH\n   {bundler}\n{tail}"


@unittest.skipIf(BASH is None, "needs bash 4 or later")
class RailsRecipeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.app, self.bin, self.home, self.state = root / "app", root / "bin", root / "home", root / "state"
        for d in (self.app / "bin", self.bin, self.home, self.state):
            d.mkdir(parents=True)
        self.log = root / "log"
        for name, body in {**STUBS, "rails": ""}.items():
            path = (self.app / "bin" / name) if name == "rails" else (self.bin / name)
            path.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "$LOG"\n{body}\n')
            path.chmod(0o755)
        (root / "bash_env").write_text(BASH_ENV)
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "LOG": str(self.log),
                    "STATE": str(self.state), "BASH_ENV": str(root / "bash_env"), "SBX_FQDN": "sbx-t.sbx.internal"}

    def tearDown(self):
        self._tmp.cleanup()

    def files(self, **files):
        for name, text in files.items():
            path = self.app / name.replace("__", "/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def run_recipe(self) -> list[str]:
        self.log.write_text("")
        done = subprocess.run([BASH, str(RECIPE)], cwd=self.app, env=self.env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return self.log.read_text().splitlines()

    def test_a_rails_8_app_with_sqlite_and_tailwind(self):
        self.files(**{".ruby-version": "ruby-3.4.1\n", "Gemfile.lock": lockfile("rails", "sqlite3", "tailwindcss-rails")})
        calls = self.run_recipe()
        for want in ("rvm install 3.4.1", "rvm use 3.4.1", "gem install bundler -v 2.6.2 --no-document",
                     "bundle install --jobs 4", "rails db:prepare", "rails tailwindcss:build"):
            self.assertIn(want, calls)
        self.assertFalse([c for c in calls if c.startswith(("docker", "nvm", "yarn", "npm"))], calls)

    def test_postgres_redis_yarn_1_a_gemset_and_a_partial_ruby(self):
        self.files(**{".ruby-version": "3.3\n", ".ruby-gemset": "shop\n", ".nvmrc": "22\n",
                      "Gemfile.lock": lockfile("pg", "sidekiq", "jsbundling-rails"),
                      "package.json": "{}", "yarn.lock": ""})
        calls = self.run_recipe()
        # The newest cached 3.3, by version order: 3.3.10, not 3.3.6.
        for want in ("rvm install 3.3.10", "rvm use 3.3.10@shop --create", "nvm install 22",
                     "yarn install --frozen-lockfile", "rails javascript:build"):
            self.assertIn(want, calls)
        runs = [c for c in calls if c.startswith("docker run")]
        self.assertTrue(any("sbx-postgres" in c and "postgres:17" in c for c in runs), runs)
        self.assertTrue(any("sbx-redis" in c for c in runs), runs)
        self.assertIn("PGHOST=127.0.0.1", (self.home / ".zshenv").read_text())
        # A second run starts the same containers, and writes the variables once.
        again = self.run_recipe()
        self.assertFalse([c for c in again if c.startswith("docker run")], again)
        self.assertIn("docker start sbx-postgres", again)
        self.assertEqual((self.home / ".zshenv").read_text().count("sbx-rails-postgres"), 1)

    def test_compose_pnpm_and_a_seed_that_loads_once(self):
        self.files(**{"Gemfile.lock": lockfile("pg", ruby="3.4.1"), "compose.yaml": "services: {}\n",
                      "package.json": '{"packageManager": "pnpm@9.0.0"}', "pnpm-lock.yaml": "",
                      "db__seed.sql.gz": "x"})
        calls = self.run_recipe()
        for want in ("rvm install 3.4.1", "docker compose -f compose.yaml up -d --wait",
                     "pnpm install --frozen-lockfile", "rails db -p"):
            self.assertIn(want, calls)
        self.assertTrue(any(c.startswith("corepack enable") for c in calls), calls)
        # The compose file owns the services: the recipe starts no container of its own.
        self.assertFalse([c for c in calls if c.startswith("docker run")], calls)
        self.assertNotIn("rails db -p", self.run_recipe())


if __name__ == "__main__":
    unittest.main()
