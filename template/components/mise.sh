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
as_user_retry 'curl -fsSL --retry 5 --retry-delay 5 https://mise.run | MISE_INSTALL_PATH="$HOME/.local/bin/mise" sh'
as_user 'mise --version'
# The shims directory exists from the start: a shell puts it on its PATH only
# if it exists, and the first tool of a sandbox would otherwise need a new shell.
as_user 'mkdir -p ~/.local/share/mise/shims'
# A shim has no terminal in which to ask for trust. A mise.toml with [env], a
# template or a hook would stop the shims in that project with "not trusted"; a
# file with [tools] only needs no trust. The sandbox runs the project's recipe
# anyway, so this trust gives the project nothing more.
as_user 'mise settings add trusted_config_paths "$HOME/code"'
for tool in $SBX_MISE_TOOLS; do
  as_user "mise use --global --yes '$tool'"
done
CHECK_TOOLS+=" mise"
