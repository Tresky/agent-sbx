#!/usr/bin/env bash
# A recipe for a standard Rails application (Rails 7 or 8). Copy the .sandbox
# directory to the root of the application. Each step looks at what the
# application uses, so most applications need no edit.
#
# The recipe is safe to run again in the same sandbox:
#   ssh sbx-<name> sbx-recipe-run code/<app> .sandbox/setup.sh .env.local
#
# No `set -u`: the rvm and nvm shell functions read unset variables.
set -eo pipefail

step() { printf '\n==> %s\n' "$*"; }
# A gem in Gemfile.lock. Its specs are indented four spaces: "    pg (1.5.9)".
has_gem() { [[ -f Gemfile.lock ]] && grep -qE "^    $1 \(" Gemfile.lock; }
# The value of a tool in .tool-versions (asdf, mise).
tool_version() { [[ -f .tool-versions ]] && awk -v t="$1" '$1 == t {print $2; exit}' .tool-versions; }
trim() { tr -d '[:space:]' < "$1"; }

step "ruby"
ruby_version=""
if [[ -f .ruby-version ]]; then
  ruby_version="$(trim .ruby-version)"
elif [[ -n "$(tool_version ruby)" ]]; then
  ruby_version="$(tool_version ruby)"
elif [[ -f Gemfile.lock ]]; then
  # RUBY VERSION / "   ruby 3.4.1p0"
  ruby_version="$(awk '/^RUBY VERSION/ {getline; print $2; exit}' Gemfile.lock | sed 's/p[0-9]*$//')"
fi
ruby_version="${ruby_version#ruby-}"
if [[ -n "$ruby_version" ]]; then
  if [[ ! "$ruby_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    # rvm stable knows no Ruby newer than 2021, so it cannot complete a partial
    # "3.4". Take the newest match that the template caches.
    match="$(rvm list strings | grep -E "^ruby-${ruby_version//./\\.}\\." | sort -V | tail -1 || true)"
    if [[ -z "$match" ]]; then
      echo "ruby '$ruby_version' is not a full version, and the template has no match." >&2
      echo "Write a full version such as 3.4.1 to .ruby-version." >&2
      exit 1
    fi
    ruby_version="${match#ruby-}"
  fi
  rvm install "$ruby_version"   # no operation when the template has it
  if [[ -f .ruby-gemset ]]; then
    # rvm's cd hook selects the gemset in every later shell, so the gems must go there.
    rvm use "$ruby_version@$(trim .ruby-gemset)" --create
  else
    rvm use "$ruby_version"
  fi
fi
ruby --version

step "gems"
bundler_version="$(awk '/^BUNDLED WITH/ {getline; print $1; exit}' Gemfile.lock 2>/dev/null || true)"
if [[ -n "$bundler_version" ]] && ! gem list -i bundler -v "$bundler_version" >/dev/null 2>&1; then
  gem install bundler -v "$bundler_version" --no-document
fi
bundle check >/dev/null 2>&1 || bundle install --jobs "$(nproc)"

if [[ -f package.json ]]; then
  step "javascript"
  node_version=""
  if [[ -f .nvmrc ]]; then node_version="$(trim .nvmrc)"
  elif [[ -f .node-version ]]; then node_version="$(trim .node-version)"
  elif [[ -n "$(tool_version nodejs)" ]]; then node_version="$(tool_version nodejs)"
  fi
  if [[ -n "$node_version" ]]; then nvm install "$node_version"; fi
  node --version
  if grep -q '"packageManager"' package.json; then
    # The pinned yarn or pnpm, through corepack. Its shims go in ~/.local/bin,
    # which is first on the PATH, so they do not fight the template's yarn.
    command -v corepack >/dev/null || npm install -g corepack
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 corepack enable --install-directory "$HOME/.local/bin"
    export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
  fi
  if [[ -f bun.lock || -f bun.lockb ]]; then
    command -v bun >/dev/null || { curl -fsSL https://bun.sh/install | bash; export PATH="$HOME/.bun/bin:$PATH"; }
    bun install --frozen-lockfile
  elif [[ -f pnpm-lock.yaml ]]; then
    pnpm install --frozen-lockfile
  elif [[ -f yarn.lock && -f .yarnrc.yml ]]; then
    yarn install --immutable        # yarn 2 and later
  elif [[ -f yarn.lock ]]; then
    yarn install --frozen-lockfile  # yarn 1
  elif [[ -f package-lock.json ]]; then
    npm ci
  else
    npm install
  fi
fi

step "services"
compose=""
for f in compose.yaml compose.yml docker-compose.yaml docker-compose.yml; do
  if [[ -f "$f" ]]; then compose="$f"; break; fi
done
if [[ -n "$compose" ]]; then
  # Docker publishes on the wildcard address, so a database is reachable from
  # your Mac at sbx-<name>.<domain>:<port> with no mirror involved.
  # --wait holds until each service with a health check is healthy.
  docker compose -f "$compose" up -d --wait || docker compose -f "$compose" up -d
else
  # No compose file: start what the gems ask for, the way a developer's Mac
  # has it installed. Each container restarts with the sandbox.
  start_container() { # <name> <port> <image> [docker run options...]
    local name="$1" port="$2" image="$3"; shift 3
    if [[ -z "$(docker ps -aq -f "name=^$name$")" ]]; then
      docker run -d --name "$name" --restart unless-stopped -p "$port:$port" "$@" "$image" >/dev/null
    else
      docker start "$name" >/dev/null
    fi
  }
  if has_gem pg && [[ -z "${DATABASE_URL:-}" ]]; then
    image="postgres:17"
    if has_gem activerecord-postgis-adapter; then image="postgis/postgis:17-3.5"; fi
    start_container sbx-postgres 5432 "$image" -e POSTGRES_HOST_AUTH_METHOD=trust
    # The default database.yml names no host, so libpq would try a socket.
    # These two variables point it at the container, in this recipe and in
    # every later shell of the sandbox.
    export PGHOST=127.0.0.1 PGUSER=postgres
    grep -q '# sbx-rails-postgres' "$HOME/.zshenv" 2>/dev/null \
      || echo 'export PGHOST=127.0.0.1 PGUSER=postgres  # sbx-rails-postgres' >> "$HOME/.zshenv"
    for _ in $(seq 1 60); do
      docker exec sbx-postgres pg_isready -q -U postgres && break
      sleep 1
    done
  fi
  if { has_gem redis || has_gem sidekiq || has_gem resque; } && [[ -z "${REDIS_URL:-}" ]]; then
    start_container sbx-redis 6379 redis:7
  fi
  if has_gem mysql2 || has_gem trilogy; then
    echo "NOTE: this app uses MySQL. Add a compose file with a MySQL service; the recipe starts none." >&2
  fi
fi

step "database"
bin/rails db:prepare
# A sample database, sent as the optional seed-dump input. It loads once: a
# second run of the recipe must not load it on top of itself.
if [[ -f db/seed.sql.gz && ! -f tmp/.sbx-seed-loaded ]]; then
  gunzip -c db/seed.sql.gz | bin/rails db -p
  mkdir -p tmp && touch tmp/.sbx-seed-loaded
fi

step "assets"
# Build the CSS and JavaScript once, so a plain `bin/rails server` has them.
# `bin/dev` watches and rebuilds them after this.
if has_gem tailwindcss-rails; then bin/rails tailwindcss:build; fi
if has_gem dartsass-rails; then bin/rails dartsass:build; fi
if has_gem cssbundling-rails; then bin/rails css:build; fi
if has_gem jsbundling-rails; then bin/rails javascript:build; fi

# A snapshot or a crash can leave the pid file of a server that is gone.
rm -f tmp/pids/server.pid

echo
echo "Ready. Start the app with bin/dev, or bin/rails server. It binds 127.0.0.1:3000,"
echo "and the sandbox serves it at https://${SBX_FQDN:-sbx-<name>.<domain>}:3000"
