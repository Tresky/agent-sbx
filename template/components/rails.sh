# The libraries and clients that a Rails application links: Postgres, SQLite, libvips, ImageMagick, Redis
# requires: ruby

step "rails libraries"
apt-get install -y -q --no-install-recommends \
  libpq-dev postgresql-client libsqlite3-dev sqlite3 libvips-dev imagemagick redis-tools
