# Projects: recipes, manifests and layouts

A project tells sbx how to set it up in a sandbox. It keeps up to three files
in its own repository:

```
.sandbox/
  setup.sh        the recipe: one setup script, run after the clone
  sandbox.toml    the manifest: the inputs that the recipe needs (optional)
  herdr.toml      the pane layout of the sandbox (optional)
```

Start from `examples/rails/.sandbox/`. It works for a standard Rails 7 or 8
application with no edit, and it shows each file.

## Template or recipe

- Put a part in the template if it is slow and many projects use it.
- Put a part in the recipe if it is specific to one project and fast.

A project picks its template in the manifest (`[recipe] template = "rails"`).
The template caches the Ruby and Node versions and the Docker images of its
projects. The recipe still asks for its own versions (`rvm install`,
`nvm install`); a version that the template caches costs nothing, and any
other version costs a compile or a download at recipe time. `sbx versions`
shows what the projects need and what each template caches.
[templates.md](templates.md) explains templates.

## How a recipe runs

- `sbx-recipe-run` starts the recipe with `bash`, from the project directory.
- The shell has `node`, `docker` and the `nvm` function in every template. It
  also has the tools of its template's components: `ruby` and the `rvm`
  function with `ruby`, `go` with `go`, `cargo` with `rust`, and so on.
- The `env` inputs are exported for the recipe. They are also in the file that
  `env_file` names.
- Three variables come from sbx itself, first in that file:
  - `SBX_HOSTNAME`: `sbx-<name>`.
  - `SBX_FQDN`: the name by which a browser on your Mac reaches the sandbox.
  - `SBX_PROFILE`: `agent` or `personal`.
- The output goes to `~/.local/state/sbx/recipe.log` in the sandbox.
- If the recipe fails, the sandbox stays up and `sbx new` exits with code 1.

To run the recipe again in the same sandbox, after an edit:

```
ssh sbx-<name> sbx-recipe-run code/<project> .sandbox/setup.sh <env_file>
```

### Rules for a recipe

- **Do not use `set -u`.** The `rvm` and `nvm` functions read unset variables.
- **Take every URL that your Mac follows from `SBX_FQDN`:** a callback, an OIDC
  issuer, a CORS origin. In the sandbox, `hostname -f` answers with the short
  name only.
- **With `.ruby-gemset`, run `rvm use <version>@<gemset> --create` before
  `bundle install`.** rvm's `cd` hook selects that gemset in every later
  shell, so gems in another gemset are invisible to `bin/rails`.
- **Do not edit the project's `.env` for the sandbox's ports.** Write
  `.env.development.local`; dotenv-rails reads it first.
- **Bind dev servers to `127.0.0.1`.** The sandbox then serves them with
  `https://` on the same port. A server on every interface gets no TLS.

### Write a recipe that can run again

You will run a recipe again in the same sandbox after each fix. Make each step
safe to repeat:

- `bin/rails db:prepare` is safe. A `db:schema:load` from `structure.sql` fails
  the second time; check for `schema_migrations` first.
- Load a sample database one time only. Leave a marker file in `tmp/`.
- Skip a `yarn link` that exists.
- `touch` each file that a compose `env_file` names. A compose file fails when
  the file is missing, even when it is empty and git-ignored.
- Start a container only when it is not there already: `docker start` an
  existing one.
- `rvm install` of a version that exists is a no-op.

## The manifest

```toml
[recipe]
setup    = ".sandbox/setup.sh"   # the default
env_file = ".sandbox.env"        # the default; ".env.local" suits Rails and Vite
template = "rails"               # optional: the template to clone

[[input]]
name        = "rails-master-key"
kind        = "file"             # file | env | repo
dest        = "config/master.key"
required    = true               # the default
secret      = false              # the default; display only
agent       = true               # the default; false = never sent to an agent sandbox
placeholder = "..."              # optional; used when the input is withheld or absent
about       = "One line for the `sbx inputs` table"
```

| Kind | Keys | What sbx does |
|---|---|---|
| `file` | `dest` | sends a file from your Mac to `dest` in the clone, mode `0600` |
| `env` | none; `name` is the variable | writes `NAME='value'` to `env_file` |
| `repo` | `url`, `dest` | clones a second repository; `dest` can be `../<name>` |

An unknown key is an error, so a typing mistake cannot remove a restriction.

`sbx inputs <project>` shows each input, where its value comes from, and the
repositories that a git token must cover. It makes nothing.

## Where a value comes from

1. A binding in `~/.config/sbx/bindings/<project>.toml`, if one exists.
2. For `file`: the same relative path in your local checkout.
3. For `env`: the variable of the same name on your Mac.

The bindings file is on your Mac only. It is the only place that can name a
command or a path outside the checkout:

```toml
[inputs.rails-master-key]
command = ["op", "read", "op://dev/app/master-key"]   # a list, not a shell string

[inputs.STRIPE_SECRET_KEY]
path = "~/secrets/stripe-test-key"
```

A credential that a tool reads from `$HOME` (`~/.npmrc`, for one) cannot have
a `dest` outside the clone. Send it as a `file` input to `.sandbox/npmrc`, and
let the recipe install it in `$HOME` with mode 0600.

## One git token per project

An agent sandbox clones with a token, never with your SSH agent. The token
belongs to one project, so a `myapp` sandbox cannot read `otherapp`, and the
reverse. A personal sandbox does not use it: it uses your forwarded SSH agent.

One command does the whole setup:

```
sbx git-token ~/code/myapp
```

1. It prints the project name and the repositories that the token must cover:
   the origin, plus each `repo` input of the manifest.
2. Make a fine-grained token on GitHub (**Settings**, **Developer settings**,
   **Fine-grained tokens**). Under **Repository access**, choose **Only select
   repositories**, and select those repositories. Give it **Contents: Read**,
   or **Read and write** if the agent must push its branches. Set an expiry.
3. Paste the token at the hidden prompt.
4. The command asks GitHub whether the token can read each repository. A token
   that misses one stores nothing.
5. It stores the token in the keychain as `sbx-git-<project>`, and writes the
   `[git]` section of the project's bindings file:

   ```toml
   # ~/.config/sbx/bindings/myapp.toml
   [git]
   token_command = ["security", "find-generic-password", "-s", "sbx-git-myapp", "-w"]
   # host = "github.com"          # the default
   # username = "x-access-token"  # the default; GitLab accepts any name with a token
   ```

The project name is the last part of the repository URL without `.git`.
`--stdin` reads the token from a pipe. `--host` and `--username` serve a host
other than GitHub, which the command does not check. `--remove` forgets the
token and the section.

Without a `[git]` section, sbx uses `git_token_command` in `config.toml`. Without
that, an agent sandbox can clone public repositories only.

## A second repository

A project that builds against a sibling checkout declares it as a `repo` input
with `dest = "../<repo>"`. The recipe builds it first.

When the packages of a private registry are also that sibling clone, the
sandbox needs no registry token. yarn 1 fetches a package from the registry
even when a `yarn link` points at it, so a link alone is not enough. For the
install only, rewrite those entries in `package.json` to
`link:../<repo>/packages/<name>`, run `yarn install`, and put `package.json`
and `yarn.lock` back. The limit: a `yarn add` in the sandbox resolves every
dependency again and needs the token.

## The pane layout (`herdr.toml`)

A project may carry `.sandbox/herdr.toml`: a grid of rows, each row a list of
cells, each cell one pane or a stack of panes. `sbx new` builds the grid in the
sandbox's herdr after the recipe and starts each pane's command, so the
servers run when you open the sandbox. `sbx layout <name>` builds it again.

```toml
tab = "myapp"

[[row]]
height = 0.5
panes  = ["rails", "compose", "auth"]
widths = [0.5, 0.25, 0.25]

[[row]]
height = 0.5
panes  = ["vite", "go", ["ui-lib", "ui-vue"], "caddy"]   # a list is a stack
widths = [0.25, 0.25, 0.25, 0.25]

[pane.rails]
run = "bin/rails s -p 4400 -b 127.0.0.1"

[pane.auth]
cwd = "services/auth"      # relative to the clone; ../<repo> for a sibling
run = "docker compose up"
```

- `height` and `widths` are fractions that add up to 1. Without them, the rows
  and the cells share the space equally.
- A pane with no `[pane.<name>]` table is a shell in the clone.
- The commands run in the sandbox, as the recipe does. Nothing runs on your Mac.

## A route for a dev server behind the app

An app that proxies its own dev server is slow through that proxy. For
example, vite_ruby sends each ES module through Rails, one request at a time.
A **route** tells the sandbox's port mirror to send one path of a port
straight to the dev server, before the app sees the request. The page keeps
its same-origin URLs.

The recipe writes a drop-in file:

```toml
# /etc/sbx/mirror.d/<project>.toml
[[route]]
port = 4400                      # a mirrored HTTP port
path = "/vite-dev/*"             # a Caddy path matcher
to   = "https://127.0.0.1:3036"  # https: TLS to the upstream, with no verification
```

The mirror reads each `*.toml` in `/etc/sbx/mirror.d/` every two seconds. So a
route takes effect at once, and a recipe can write its file again on each run.

## The safety rules

An agent can edit the manifest on its branch. sbx reads the manifest on your
Mac, where it can read your files. The manifest is therefore untrusted.

- **The manifest can restrict. Only you can permit.** `agent = false` makes an
  input stricter. No key makes an input looser.
- **A `dest` cannot leave the clone.** sbx refuses an absolute path, `~`, and
  `..`. A `repo` input can use exactly `../<name>`.
- **sbx does not follow a symbolic link.** A branch can commit
  `config/master.key` as a link to `~/.ssh/id_ed25519`. sbx refuses each link
  on the path.
- **The manifest never holds a command or a Mac path.** Only your bindings file
  can.
- **A git URL must be a plain remote.** sbx refuses a URL that starts with `-`,
  and the `ext::` and `file://` transports.
- **An agent sandbox needs a decision for each `file` and `env` input.** Pass
  `--with <name>` or `--without <name>`. There is no prompt, so a script cannot
  answer one wrongly. The `secret` flag does not lower this gate, because the
  manifest sets it.
- **A personal sandbox sends each input that sbx finds.** sbx prints the list.

One limit remains. `--with <name>` permits an input by its name. If a branch
changes the `dest` of that name, sbx sends a different file from the checkout.
Read the manifest diff of an agent branch before you run `sbx new` from it. The
table that `sbx new` prints shows the real path.

[security.md](security.md) puts these rules in the context of the whole design.

## How a value gets into the sandbox

- Through SSH, after the clone and before the recipe. A value is never on a
  command line and never in a temporary file on your Mac.
- Not through cloud-init: cloud-init data stays readable on the Proxmox host
  and in the VM.
- sbx adds each file that it sends to `.git/info/exclude` in the clone. An
  agent cannot commit the file by accident, and the repository does not change.
- A snapshot contains the values on the disk. `sbx rm` destroys the VM and its
  snapshots together.

## Lessons from real recipes

A large application (Rails, Vite, a Go service, a second repository, a private
npm registry, PostGIS, Redis and an OIDC provider) took nine runs in one
sandbox before its recipe reached the end. The rules above come from it. These
lessons complete them:

- **The database version is the project's, not your Mac's.** A `structure.sql`
  from pg_dump 17 does not load into PostgreSQL 15. Move the compose file to
  the new version with a NEW volume name: a data directory that one major
  version wrote cannot serve the next.
- **Seeds may need data that an empty database lacks.** Use the project's own
  sample-data task when it has one, and make it safe to run twice. An optional
  dump input (`pg_dump -Fc`) can restore real-shaped data instead.
- **An identity provider needs the sandbox's name.** An OIDC provider builds
  each URL of its discovery document from its configured host name. When the
  compose file pins that to `localhost`, a browser on your Mac goes to its own
  `localhost`. A compose override, local to the clone, names the sandbox. A
  provider that refuses plain HTTP from a non-private address also refuses
  your Mac, which arrives from its Tailscale address. And the app needs the
  sandbox's issuer, callback and client secret, in the git-ignored files that
  it reads first.
- **A dev server that turns on TLS when a certificate file exists needs one in
  the sandbox.** Without the file, such a server can listen for TLS with no
  certificate, and a proxy to it fails on each request. Copy the sandbox's own
  certificate from `/etc/sbx/tls/` to the place that the server reads.
- **A dev server behind the app's own proxy needs a route.** One page loaded in
  4 s because Rails proxied 309 Vite modules one by one. A route for
  `/vite-dev/*` on port 4400 made it load in 0.3 s.
