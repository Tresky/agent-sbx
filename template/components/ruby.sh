# Ruby through mise, with Bundler
# requires: mise
#
# Settings, in a definition's [ruby] table:
#   versions  the Rubies to cache, such as "3.4.10". The first is the default.
#             `sbx versions --write` adds the versions that the template's
#             projects need. Default: 3.4.10.
#
# mise downloads a precompiled Ruby when one exists, and compiles one otherwise.
# A project's .ruby-version, .tool-versions or mise.toml picks its version.
: "${SBX_RUBY_VERSIONS:=3.4.10}"

step "ruby build libraries"
# For native gems, and for a Ruby that has no precompiled build.
apt-get install -y -q --no-install-recommends libreadline-dev libyaml-dev libgmp-dev

step "ruby (mise): $SBX_RUBY_VERSIONS"
# mise reads .ruby-version only with this setting.
as_user 'mise settings add idiomatic_version_file_enable_tools ruby'
first=1
for v in $SBX_RUBY_VERSIONS; do
  as_user_retry "mise install 'ruby@$v'"
  if [[ $first -eq 1 ]]; then as_user "mise use --global --yes 'ruby@$v'"; first=0; fi
done
as_user_retry 'gem install bundler --no-document'
CHECK_TOOLS+=" ruby bundle"
