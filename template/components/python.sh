# Python through uv, with the headers that native packages build against
#
# Settings, in a definition's [python] table:
#   versions  the Pythons that uv caches, such as "3.13 3.12"
#             (default "3.13"). A project's .python-version still picks its
#             own: uv downloads it on first use.
: "${SBX_PYTHON_VERSIONS:=3.13}"

step "python build libraries"
apt-get install -y -q --no-install-recommends python3-dev libbz2-dev libreadline-dev libsqlite3-dev liblzma-dev

step "python (uv): $SBX_PYTHON_VERSIONS"
# uv installs itself into ~/.local/bin, which ~/.zshenv puts on the PATH.
as_user 'curl -fsSL --retry 5 --retry-delay 5 https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh'
# shellcheck disable=SC2086  # a space-separated list
as_user "uv python install $SBX_PYTHON_VERSIONS"
CHECK_TOOLS+=" uv"
