# Go, from the official tarball
#
# Settings, in a definition's [go] table:
#   version  a full Go version from https://go.dev/dl/ (default 1.27.1). A
#            project still gets the toolchain that its go.mod names: Go
#            downloads it.
: "${SBX_GO_VERSION:=1.27.1}"

step "go $SBX_GO_VERSION"
# The official tarball, not Ubuntu's package, which lags by years. Modules and
# built binaries go to ~/go, which ~/.zshenv puts on the PATH. The old tree is
# removed first, as the Go instructions require: a tarball over an old tree
# leaves stale files behind.
tmp="$(mktemp -d)"
wget -q --tries=5 --waitretry=5 --retry-connrefused "https://go.dev/dl/go${SBX_GO_VERSION}.linux-amd64.tar.gz" -O "$tmp/go.tgz"
rm -rf /usr/local/go
tar -C /usr/local -xzf "$tmp/go.tgz"
rm -rf "$tmp"
/usr/local/go/bin/go version
CHECK_TOOLS+=" go"
