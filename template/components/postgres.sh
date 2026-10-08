# PostgreSQL from the PostgreSQL project's apt repository, with PostGIS
#
# Settings, in a definition's [postgres] table:
#   version  the major version (default 18). apt.postgresql.org has every
#            supported one for Debian and Ubuntu.
#   postgis  true (default) or false: the PostGIS 3 extension for it.
#
# The cluster `main` runs on port 5432 in every sandbox. The sandbox user is
# a superuser of it, by its own name, so `psql`, `createdb` and an app's
# `pg` adapter work with no password over the local socket.
: "${SBX_POSTGRES_VERSION:=18}"
: "${SBX_POSTGRES_POSTGIS:=1}"

step "postgres $SBX_POSTGRES_VERSION$([[ "$SBX_POSTGRES_POSTGIS" == 1 ]] && echo " + postgis")"
install -d -m 0755 /usr/share/postgresql-common/pgdg
curl -fsSL --retry 5 --retry-delay 5 https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo "$VERSION_CODENAME")-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list
apt-get update -q
pg_packages="postgresql-$SBX_POSTGRES_VERSION postgresql-client-$SBX_POSTGRES_VERSION libpq-dev"
[[ "$SBX_POSTGRES_POSTGIS" == 1 ]] && pg_packages+=" postgresql-$SBX_POSTGRES_VERSION-postgis-3"
# shellcheck disable=SC2086  # a space-separated list
apt-get install -y -q --no-install-recommends $pg_packages
systemctl enable --now postgresql
sudo -u postgres createuser --superuser "$U" 2>/dev/null || true
CHECK_TOOLS+=" psql"
