"""A project's pane layout, built in the sandbox's herdr from the Mac.

A project may carry `.sandbox/herdr.toml`: a grid of rows, each row a list of
cells, a cell one pane or a stack of panes. `sbx new` builds it after the
recipe, `sbx layout <name>` builds it again. The build runs on the Mac and
drives the sandbox's own herdr server through `herdr --machine sbx-<name>`,
which needs no open window: the panes are there when the sandbox is picked in
the sidebar.

Why not the herdr-spreader plugin: it splits each pane from the one made just
before it, so a row with a 50 | 25 | 25 split above a row with four columns
cannot be made. herdr's pane API splits ANY pane, and `pane layout` reports
every pane's rectangle, so the grid is both exact and checkable.

`--ratio` on `pane split` is the share the ORIGINAL pane keeps (0.25 on a
`down` split leaves the top pane a quarter of the height). Every split here
is computed from that.

Trust: the file is read from the clone in the sandbox and its commands run in
the sandbox, in panes, as the recipe does. Nothing in it runs on the Mac.

```toml
tab = "myapp"

[[row]]
height = 0.5
panes  = ["rails", "compose", "auth"]
widths = [0.5, 0.25, 0.25]

[[row]]
height = 0.5
panes  = ["vite", "go", ["ui-lib", "ui-vue"], "caddy"]   # a list = a stack
widths = [0.25, 0.25, 0.25, 0.25]

[pane.rails]
run = "bin/rails s -p 4400 -b 127.0.0.1"
[pane.auth]
cwd = "services/auth"      # relative to the clone; ../<repo> allowed
run = "docker compose up"
```
"""
from __future__ import annotations

import json
import posixpath
import time
import tomllib
from dataclasses import dataclass, field

from .run import CommandError, Runner


class LayoutError(ValueError):
    pass


@dataclass
class Pane:
    name: str
    cwd: str = "."
    run: str = ""


@dataclass
class Row:
    height: float
    cells: list[list[str]]      # each cell: the pane names of its stack, top to bottom
    widths: list[float]


@dataclass
class Layout:
    tab: str
    rows: list[Row]
    panes: dict[str, Pane] = field(default_factory=dict)

    @property
    def names(self) -> list[str]:
        return [n for row in self.rows for cell in row.cells for n in cell]


def _fractions(values, what: str, count: int) -> list[float]:
    if not isinstance(values, list) or len(values) != count:
        raise LayoutError(f"{what}: needs {count} numbers")
    out = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v < 1:
            raise LayoutError(f"{what}: each value must be a fraction between 0 and 1")
        out.append(float(v))
    if abs(sum(out) - 1.0) > 0.02:
        raise LayoutError(f"{what}: the fractions add up to {sum(out):.2f}, not 1")
    return out


def _cwd(name: str, value) -> str:
    """Relative to the clone; `..` may lead only to a sibling clone, the way a
    manifest's repo input may. Absolute paths and `~` are refused: the layout
    belongs to the repository, and the clone's place is the tool's decision."""
    if not isinstance(value, str) or not value:
        raise LayoutError(f"pane {name}: cwd must be a path")
    if value.startswith(("/", "~")):
        raise LayoutError(f"pane {name}: cwd must be relative to the clone: {value}")
    norm = posixpath.normpath(value)
    parts = norm.split("/")
    if parts[0] == ".." and (len(parts) < 2 or parts[1] == ".."):
        raise LayoutError(f"pane {name}: cwd may leave the clone only into a sibling: {value}")
    return norm


def parse(text: str) -> Layout:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LayoutError(f"herdr.toml: {exc}") from None
    if set(data) - {"tab", "row", "pane"}:
        raise LayoutError(f"herdr.toml: unknown keys {sorted(set(data) - {'tab', 'row', 'pane'})}")
    tab = data.get("tab", "dev")
    if not isinstance(tab, str) or not tab.strip():
        raise LayoutError("herdr.toml: tab must be a name")
    rows_in = data.get("row")
    if not isinstance(rows_in, list) or not rows_in:
        raise LayoutError("herdr.toml: needs at least one [[row]]")
    rows: list[Row] = []
    for i, r in enumerate(rows_in, 1):
        if not isinstance(r, dict) or set(r) - {"height", "panes", "widths"}:
            raise LayoutError(f"row {i}: keys are height, panes, widths")
        panes = r.get("panes")
        if not isinstance(panes, list) or not panes:
            raise LayoutError(f"row {i}: panes must be a list of names, a nested list is a stack")
        cells: list[list[str]] = []
        for cell in panes:
            stack = cell if isinstance(cell, list) else [cell]
            if not stack or not all(isinstance(n, str) and n for n in stack):
                raise LayoutError(f"row {i}: a pane is a name, a stack a list of names")
            cells.append(stack)
        if len(cells) > 1:
            widths = _fractions(r.get("widths", [1 / len(cells)] * len(cells)), f"row {i} widths", len(cells))
        else:
            widths = [1.0]
        h = r.get("height", 1 / len(rows_in))
        if isinstance(h, bool) or not isinstance(h, (int, float)):
            raise LayoutError(f"row {i}: height must be a fraction")
        rows.append(Row(height=float(h), cells=cells, widths=widths))
    if len(rows) > 1:
        for r, h in zip(rows, _fractions([r.height for r in rows], "row heights", len(rows))):
            r.height = h
    else:
        rows[0].height = 1.0
    layout = Layout(tab=tab.strip(), rows=rows)
    seen: set[str] = set()
    for n in layout.names:
        if n in seen:
            raise LayoutError(f"pane {n} appears twice")
        seen.add(n)
    panes_in = data.get("pane", {})
    if not isinstance(panes_in, dict):
        raise LayoutError("[pane.<name>] tables expected")
    for n, p in panes_in.items():
        if n not in seen:
            raise LayoutError(f"[pane.{n}] is not in any row")
        if not isinstance(p, dict) or set(p) - {"cwd", "run"}:
            raise LayoutError(f"[pane.{n}]: keys are cwd and run")
        run = p.get("run", "")
        if not isinstance(run, str):
            raise LayoutError(f"[pane.{n}]: run must be a command string")
        layout.panes[n] = Pane(n, cwd=_cwd(n, p.get("cwd", ".")), run=run.strip())
    for n in seen:
        layout.panes.setdefault(n, Pane(n))
    return layout


# --- the build ---------------------------------------------------------------

class Herdr:
    """`herdr --machine <label>` on the Mac. Every call returns the JSON result."""

    def __init__(self, runner: Runner, machine: str):
        self.runner, self.machine = runner, machine

    def call(self, *args: str) -> dict:
        argv = ["herdr", "--machine", self.machine, *args]
        try:
            done = self.runner.run(argv, input=b"", check=False, timeout=60)
        except CommandError as exc:
            raise LayoutError(f"herdr did not answer: {exc}") from None
        if done.code != 0:
            text = (done.stderr or done.stdout).strip().splitlines()
            raise LayoutError(f"herdr {' '.join(args[:2])} failed: {text[-1] if text else done.code}")
        if not done.stdout.strip():
            return {}       # `pane run` and `pane send-text` answer with nothing
        try:
            return json.loads(done.stdout)["result"]
        except (ValueError, KeyError, TypeError):
            raise LayoutError(f"herdr {' '.join(args[:2])}: no JSON result") from None


def _first_leaf(cell_names: list[list[str]]) -> str:
    return cell_names[0][0]


def build(h: Herdr, layout: Layout, clone: str, *, run: bool = True, replace: bool = False) -> dict[str, str]:
    """Makes the tab and its panes; returns pane name -> pane id.

    The tab gets a root pane; each split makes ONE new pane, whose cwd must
    be that of the first pane that will end up in the new region, because
    later splits of that region keep the original on the left or top.
    """
    def cwd(name: str) -> str:
        return posixpath.normpath(posixpath.join(clone, layout.panes[name].cwd))

    ws = h.call("workspace", "list")["workspaces"]
    if not ws:
        raise LayoutError("the sandbox's herdr has no workspace")
    workspace = next((w for w in ws if w.get("focused")), ws[0])["workspace_id"]
    for t in h.call("tab", "list", "--workspace", workspace).get("tabs", []):
        if t.get("label") == layout.tab:
            if not replace:
                raise LayoutError(f"herdr already has a tab '{layout.tab}' in this sandbox; --replace closes it first")
            h.call("tab", "close", t["tab_id"])

    first = _first_leaf(layout.rows[0].cells)
    made = h.call("tab", "create", "--workspace", workspace, "--label", layout.tab, "--cwd", cwd(first), "--no-focus")
    ids: dict[str, str] = {}

    # Rows: split the remaining area down, top row first.
    region = made["root_pane"]["pane_id"]
    row_roots: list[str] = []
    remaining = 1.0
    for i, row in enumerate(layout.rows):
        row_roots.append(region)
        if i == len(layout.rows) - 1:
            break
        below = _first_leaf(layout.rows[i + 1].cells)
        new = h.call("pane", "split", region, "--direction", "down", "--ratio", f"{row.height / remaining:.4f}",
                     "--cwd", cwd(below), "--no-focus")["pane"]["pane_id"]
        remaining -= row.height
        region = new

    # Cells: split each row's area to the right, left cell first; then stacks down.
    for row, root in zip(layout.rows, row_roots):
        region, remaining = root, 1.0
        cell_roots: list[str] = []
        for j, (cell, width) in enumerate(zip(row.cells, row.widths)):
            cell_roots.append(region)
            if j == len(row.cells) - 1:
                break
            new = h.call("pane", "split", region, "--direction", "right", "--ratio", f"{width / remaining:.4f}",
                         "--cwd", cwd(row.cells[j + 1][0]), "--no-focus")["pane"]["pane_id"]
            remaining -= width
            region = new
        for cell, croot in zip(row.cells, cell_roots):
            region = croot
            for k, name in enumerate(cell):
                ids[name] = region
                if k == len(cell) - 1:
                    break
                region = h.call("pane", "split", region, "--direction", "down", "--ratio", f"{1 / (len(cell) - k):.4f}",
                                "--cwd", cwd(cell[k + 1]), "--no-focus")["pane"]["pane_id"]

    for name, pid in ids.items():
        h.call("pane", "rename", pid, name)
    # The last pane's shell is still starting (zsh loads rvm and nvm); a
    # command typed before its prompt is buffered by the pty, but a moment
    # keeps the first screen of every pane tidy.
    time.sleep(2)
    for name, pid in ids.items():
        command = layout.panes[name].run
        if not command:
            continue
        if run:
            h.call("pane", "run", pid, command)
        else:
            h.call("pane", "send-text", pid, command)
    return ids


def check(h: Herdr, layout: Layout, ids: dict[str, str]) -> list[str]:
    """Compares each pane's rectangle with the grid; returns the deviations."""
    expected: dict[str, tuple[float, float, float, float]] = {}
    y = 0.0
    for row in layout.rows:
        x = 0.0
        for cell, w in zip(row.cells, row.widths):
            for k, name in enumerate(cell):
                expected[name] = (x, y + row.height * k / len(cell), w, row.height / len(cell))
            x += w
        y += row.height
    any_id = next(iter(ids.values()))
    lay = h.call("pane", "layout", "--pane", any_id)["layout"]
    area = lay["area"]
    rects = {p["pane_id"]: p["rect"] for p in lay["panes"]}
    problems = []
    for name, pid in ids.items():
        r = rects.get(pid)
        if r is None:
            problems.append(f"{name}: pane {pid} is not in the layout")
            continue
        got = (r["x"] / area["width"], r["y"] / area["height"], r["width"] / area["width"], r["height"] / area["height"])
        want = expected[name]
        # One cell of rounding per edge, and the split bars, over a small default area.
        tol = 2.5 / min(area["width"], area["height"])
        if any(abs(g - w) > max(0.03, tol) for g, w in zip(got, want)):
            problems.append(f"{name}: at x={got[0]:.2f} y={got[1]:.2f} w={got[2]:.2f} h={got[3]:.2f}, "
                            f"wanted x={want[0]:.2f} y={want[1]:.2f} w={want[2]:.2f} h={want[3]:.2f}")
    return problems
