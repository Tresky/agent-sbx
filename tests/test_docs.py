"""The docs keep step with the code: docs/reference.md names every command,
option and setting, and every relative link in the docs resolves."""
import argparse
import dataclasses
import re
import unittest
from pathlib import Path

from sbxlib import cli
from sbxlib.config import DEFAULTS_ENV, Config, parse_env_file

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = (ROOT / "docs" / "reference.md").read_text()
DOCS = [ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "docs").glob("*.md"))]


def _commands(parser: argparse.ArgumentParser, prefix: str = "sbx"):
    """(the command line, its options) for every leaf command. An option is
    the list of its spellings, such as ["-y", "--yes"]."""
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subs:
        yield prefix, [a.option_strings for a in parser._actions
                       if a.option_strings and "--help" not in a.option_strings]
        return
    for name, sub in subs[0].choices.items():
        yield from _commands(sub, f"{prefix} {name}")


class ReferenceTest(unittest.TestCase):
    def test_every_command_and_option_is_in_the_reference(self):
        for command, options in _commands(cli.build_parser()):
            if command.startswith("sbx gpu"):
                continue
            with self.subTest(command=command):
                self.assertIn(f"`{command}", REFERENCE)
                for spellings in options:
                    found = any(re.search(rf"(?<![\w-]){re.escape(s)}\b", REFERENCE) for s in spellings)
                    self.assertTrue(found, f"{command} {'/'.join(spellings)} is not in docs/reference.md")
        for action in ("status", "attach", "detach"):
            self.assertIn(f"`sbx gpu {action}", REFERENCE)

    def test_every_config_key_is_in_the_reference(self):
        for field in dataclasses.fields(Config):
            with self.subTest(key=field.name):
                self.assertIn(f"`{field.name}`", REFERENCE)

    def test_every_host_setting_is_in_the_reference(self):
        for key in parse_env_file(DEFAULTS_ENV.read_text()):
            with self.subTest(key=key):
                self.assertIn(f"`{key}`", REFERENCE)


class LinkTest(unittest.TestCase):
    def test_every_relative_link_resolves(self):
        for doc in DOCS:
            for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", doc.read_text()):
                if "://" in target:
                    continue
                with self.subTest(doc=doc.name, link=target):
                    self.assertTrue((doc.parent / target).exists(), f"{doc.name} links to {target}")

    def test_no_doc_names_a_retired_file(self):
        for doc in DOCS:
            for old in ("HANDBOOK.md", "SETUP.md", "MANIFEST.md", "DESIGN.md"):
                with self.subTest(doc=doc.name, old=old):
                    self.assertNotIn(old, doc.read_text())


if __name__ == "__main__":
    unittest.main()
