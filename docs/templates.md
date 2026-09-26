# Templates

A sandbox is a linked clone of a template. You can have several templates,
one for each kind of project, and each person builds only the ones that they
use. A Rust developer never builds Ruby, and a Rails developer never builds
Rust.

For every command and key, read [reference.md](reference.md). For how a build
works inside, read [architecture.md](architecture.md), "The template".

## What is in a template

Every template has the **core**: the base tools, Docker, Node, Chrome (through
`agent-browser`), Claude Code, herdr, and the port mirror. sbx itself needs
these.

A template adds **components**. A component is one shell file in
`template/components/`:

| Component | What it adds |
|---|---|
| `ruby` | Ruby through rvm, with Bundler |
| `rails` | the libraries that a Rails app links: Postgres, SQLite, libvips, ImageMagick, Redis. Needs `ruby`. |
| `go` | Go, from the official tarball |
| `rust` | Rust through rustup, with cargo, clippy and rustfmt |
| `python` | Python through uv |
| `odin` | Odin, with wgpu-native, premake, SDL2, Lua and software Vulkan |
| `gis` | GEOS, GDAL and PROJ |
| `media` | ffmpeg |

`sbx template components` lists them, with your own.

A **definition** names the components and their settings. It is one TOML
file:

```toml
# templates/rails.toml
description = "Ruby on Rails"
components  = ["ruby", "rails"]

[ruby]
versions = ["3.4.10"]

[node]
versions = ["lts/*"]

[docker]
images = ["postgres:17", "redis:7"]
```

## The shared definitions

The repository has these definitions in `templates/`:

| Template | Components |
|---|---|
| `minimal` | the core only |
| `sidecar` | `sidecar`, with `bare = true`: no core, on Debian 13 genericcloud (4 GB disk, 512 MB). The sidecar of each agent sandbox. `sbx setup` builds it. |
| `rails` | `ruby`, `rails` |
| `go` | `go` |
| `rust` | `rust` |
| `python` | `python` |

A definition in `templates/local/` is yours. It is not in git, and it wins over
a shared definition of the same name.

## See your templates

```
sbx template list
```

```
TEMPLATE  DEFINITION  STATE        BUILT          SANDBOXES  DESCRIPTION
rails *   shared      current      9002           3          Ruby on Rails
rust      local       OUT OF DATE  9004 (+1 old)  1          Rust, with wasm and Postgres
go        shared      not built    -              0          Go
```

- **STATE** is `current` when the built template matches its definition and
  its component files. After an edit to either, the state is `OUT OF DATE`.
- **BUILT** is the ID of the newest version, and the number of older versions
  that sandboxes still use.
- `*` marks `default_template`.

`sbx template show <name>` shows one template in detail: its components, the
exact build settings, and each version.

## Build a template

```
sbx template rebuild rust            one template
sbx template rebuild rust go         several
sbx template rebuild --changed       each one that is not current
```

- A build takes 15 to 40 minutes, and asks for the host's root password one
  time.
- Each build is a **new version**. Sandboxes keep the version that they were
  cloned from; new sandboxes get the new version.
- A version that no sandbox uses is removed after the next build of that
  template, or by `sbx template prune`.
- If your SSH session drops during a build, run `sbx template finish <name>`.

## Choose the template of a sandbox

sbx takes the first of these:

1. `sbx new lab --template rust`
2. the project's manifest, `.sandbox/sandbox.toml`:

   ```toml
   [recipe]
   template = "rails"
   ```

3. `default_template` in `~/.config/sbx/config.toml`
4. the only template that is built

`sbx list` shows the template of each sandbox.

## Make your own template

1. Start from a shared definition, or from an empty one:

   ```
   sbx template new web --from rails
   sbx template new tools
   ```

   Each writes `templates/local/<name>.toml`.

2. Edit the file. For example, a Rust template that also builds WebAssembly
   and needs Postgres:

   ```toml
   description = "Rust, with wasm and Postgres"
   components  = ["rust"]
   apt         = ["postgresql-client", "binaryen"]

   [rust]
   toolchains = ["stable", "nightly"]
   components = "clippy rustfmt rust-src"

   [docker]
   images = ["postgres:17"]
   ```

3. Build it:

   ```
   sbx template rebuild web
   ```

To change a shared template for yourself only, copy it with the same name:
`sbx template new rails --from rails`. Your copy in `templates/local/` then wins.

[reference.md](reference.md) lists every key and every component setting.

## Cache what your projects need

The template only caches versions. A project's recipe still installs its own
Ruby, Node and Go versions. A cached version costs nothing at recipe time; any
other one costs a compile or a download.

```
sbx versions              for each template: what its projects need, and what it caches
sbx versions --write      save that for the next build
```

`sbx template rebuild` does the `--write` step for you. The saved versions are
in `templates/local/versions.toml`. They come after the definition's own
versions, so the definition's first version stays the default.

## Write your own component

A component is a bash file that the build sources as root, after the core
packages, in the order of the definition. Put yours in
`template/components/local/<name>.sh` (not in git), or in
`template/components/<name>.sh` to share it.

```bash
# Zig, from the official tarball
#
# Settings, in a definition's [zig] table:
#   version  the Zig release (default 0.14.1)
: "${SBX_ZIG_VERSION:=0.14.1}"

step "zig $SBX_ZIG_VERSION"
tmp="$(mktemp -d)"
wget -q "https://ziglang.org/download/${SBX_ZIG_VERSION}/zig-x86_64-linux-${SBX_ZIG_VERSION}.tar.xz" -O "$tmp/zig.txz"
rm -rf /opt/zig && mkdir -p /opt/zig
tar -C /opt/zig --strip-components=1 -xJf "$tmp/zig.txz"
ln -sf /opt/zig/zig /usr/local/bin/zig
rm -rf "$tmp"
CHECK_TOOLS+=" zig"
```

The rules:

- **The first comment line** is the description that `sbx template components`
  shows.
- **`# requires: <names>`**, as a comment line, lists the components that must
  come before this one in a definition. sbx refuses a definition that breaks
  the order. `rails.sh` has `# requires: ruby`.
- **A setting** `[zig] version` reaches the file as `SBX_ZIG_VERSION`. A list
  becomes a space-separated string. Give each setting a default with `: "${VAR:=default}"`.
- **`step "title"`** prints a heading in the build log.
- **`as_user '<command>'`** runs a command as the sandbox user, in zsh, with
  rvm, nvm and the user's PATH. Use it for a tool that installs into the
  user's home.
- **`$U`** is the user name.
- **`CHECK_TOOLS+=" <command>"`** adds a command to the final check. The build
  fails if the command is not on the user's PATH in a non-interactive shell.
  A tool in `~/.local/bin`, `~/.cargo/bin` or `/usr/local/bin` is on it.
- An error stops the build, and the build VM stays up so that you can read the
  log. `bash /root/sbx/host/vm-diag.sh <vmid>` on the host shows it. The next
  build of that template removes the failed VM.

## Share a template

A definition or a component in `templates/local/` or
`template/components/local/` is yours alone. There are two ways to share one.

**One file, for one person or one project.** Export the template:

```
sbx template export rust-wasm -o rust-wasm.sbx-template.toml
```

The file holds the definition, the full text of each of your own components
that it uses, and a hash of each shared component. Send it in a message, put
it in a gist, or keep it in a project's repository. The other person imports
it:

```
sbx template import rust-wasm.sbx-template.toml
sbx template import https://gist.githubusercontent.com/.../rust-wasm.sbx-template.toml
sbx template rebuild rust-wasm
```

- The import prints every file that it will write, and asks first.
  **Caution:** a component runs as root in the template build, and its result
  is in every sandbox of that template. Read each script before you answer.
- It refuses a template or a component name that exists with other content.
  `--as <name>` imports under another name. `--force` replaces yours.
- It warns when your copy of a shared component differs from the exporter's,
  and it refuses when you lack one. Pull the latest sbx, then import again.
- An import of what you have already writes nothing.
- Nothing reaches your host until you run `sbx template rebuild`.

**Git, for a team standard.** Move the definition to `templates/`, and each
component to `template/components/`, and commit them. Everyone then has the
template after a pull, with its fixes, and a review of each change.

A second Mac that uses your host gets your local definitions and components
from the host, with `sbx setup --mac-only`.

## Change the core

The core is `template/provision.sh`. A change there reaches every template of
every person who pulls it, and marks every template `OUT OF DATE`. Put a
change in a component when only some templates need it.

`template/files/zshenv`, `zshrc` and `sbx_mirror.py` need no rebuild: `sbx
new` copies them from your checkout into each new sandbox.

## A setup from before named templates

A setup from before named templates had one template, with no name. sbx shows
it as `default`, and `sbx doctor` warns about it. `sbx setup` offers to adopt
it, or you can adopt it by hand:

```
sbx template new default --from rails     write a definition for it; edit it to match
sbx template adopt <vmid> default         give the old template the name "default"
```

The adopted template keeps working as it is. `sbx template list` shows it as
`OUT OF DATE`, because it was built before fingerprints existed. The next
`sbx template rebuild default` builds a proper version.
