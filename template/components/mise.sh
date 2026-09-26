# mise, the tool-version manager, for the sandbox user
#
# Settings, in a definition's [mise] table:
#   tools  what `mise use --global` installs in the template, such as
#          ["python@3.13", "go@1.25"] (default: none). A project's own
#          mise.toml or .tool-versions still picks its versions: mise
#          installs them on `mise install`.
#
# mise's shims go on the PATH through ~/.zshenv and the recipes' bash_env, so a
# non-interactive shell (an agent's tool call, `ssh host command`) finds the
# tools too. A tool that mise does not manage has no shim, so the core's node
# from nvm stays in place.
: "${SBX_MISE_TOOLS:=}"

step "mise"
# The installer puts mise in ~/.local/bin, which ~/.zshenv puts on the PATH.
as_user 'curl -fsSL --retry 5 --retry-delay 5 https://mise.run | MISE_INSTALL_PATH="$HOME/.local/bin/mise" sh'
as_user 'mise --version'
# The shims directory exists from the start: a shell puts it on its PATH only
# if it exists, and the first tool of a sandbox would otherwise need a new shell.
as_user 'mkdir -p ~/.local/share/mise/shims'
for tool in $SBX_MISE_TOOLS; do
  as_user "mise use --global --yes '$tool'"
done
CHECK_TOOLS+=" mise"
