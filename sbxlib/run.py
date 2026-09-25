"""One place that starts processes, so tests can replace it."""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass


class CommandError(RuntimeError):
    def __init__(self, argv, code, stderr=""):
        self.argv, self.code, self.stderr = argv, code, stderr
        super().__init__(f"exit {code}: {shlex.join(argv)}" + (f"\n{stderr.strip()}" if stderr else ""))


@dataclass
class Result:
    code: int
    stdout: str = ""
    stderr: str = ""


class Runner:
    """`responder`, when given, REPLACES process execution: it receives
    (argv, input) and returns a Result, a str (stdout), or None."""

    def __init__(self, responder=None, verbose=False):
        self.responder = responder
        self.verbose = verbose
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, input: bytes | None = None, capture: bool = True,
            check: bool = True, timeout: float | None = None) -> Result:
        self.calls.append(list(argv))
        if self.verbose:
            print(f"+ {shlex.join(argv)}")
        if self.responder is not None:
            got = self.responder(argv, input)
            result = got if isinstance(got, Result) else Result(0, got or "")
        else:
            try:
                done = subprocess.run(argv, input=input, timeout=timeout,
                                      stdout=subprocess.PIPE if capture else None,
                                      stderr=subprocess.PIPE if capture else None)
            except FileNotFoundError:
                raise CommandError(argv, 127, f"{argv[0]}: command not found") from None
            except subprocess.TimeoutExpired:
                raise CommandError(argv, 124, "timeout") from None
            result = Result(done.returncode,
                            (done.stdout or b"").decode(errors="replace"),
                            (done.stderr or b"").decode(errors="replace"))
        if check and result.code != 0:
            raise CommandError(argv, result.code, result.stderr)
        return result
