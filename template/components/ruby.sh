# Ruby through rvm, with Bundler
#
# Settings, in a definition's [ruby] table:
#   versions  the Rubies to cache, FULL versions such as "3.4.1". The first is
#             the default. rvm stable's database ends in 2021, so it cannot
#             complete a partial "3.4". `sbx versions --write` adds the
#             versions that the template's projects need. Default: 3.4.10.
: "${SBX_RUBY_VERSIONS:=3.4.10}"

step "ruby build libraries"
apt-get install -y -q --no-install-recommends libreadline-dev libyaml-dev libgmp-dev

step "ruby (rvm): $SBX_RUBY_VERSIONS"
as_user 'curl -fsSL --retry 5 --retry-delay 5 https://rvm.io/mpapis.asc | gpg --import - && curl -fsSL --retry 5 --retry-delay 5 https://rvm.io/pkuczynski.asc | gpg --import -'
# The installer downloads its own tarball from GitHub. When that download
# fails it stops with "There has been an error fetching the ruby interpreter",
# and it does not try again. This loop does, with a pause between attempts.
for attempt in 1 2 3; do
  if as_user 'curl -fsSL --retry 5 --retry-delay 5 https://get.rvm.io | bash -s stable --ignore-dotfiles'; then break; fi
  if [[ $attempt -eq 3 ]]; then echo "ERROR: the rvm installer failed three times" >&2; false; fi
  echo "the rvm installer failed (attempt $attempt of 3); next attempt in 30 s"
  sleep 30
done
first=1
for v in $SBX_RUBY_VERSIONS; do
  as_user "rvm install '$v'"
  if [[ $first -eq 1 ]]; then as_user "rvm alias create default '$v'"; first=0; fi
done
as_user 'gem install bundler --no-document'
CHECK_TOOLS+=" ruby bundle"
