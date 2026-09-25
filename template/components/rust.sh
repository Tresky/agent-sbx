# Rust through rustup, with cargo
#
# Settings, in a definition's [rust] table:
#   toolchains  the toolchains to install; the first is the default
#               (default "stable"). A project's rust-toolchain.toml still
#               picks its own: rustup installs it on first use.
#   components  more rustup components, such as "clippy rustfmt"
#               (default "clippy rustfmt")
: "${SBX_RUST_TOOLCHAINS:=stable}"
: "${SBX_RUST_COMPONENTS:=clippy rustfmt}"

step "rust (rustup): $SBX_RUST_TOOLCHAINS"
first="${SBX_RUST_TOOLCHAINS%% *}"
as_user "curl -fsSL --retry 5 --retry-delay 5 https://sh.rustup.rs | sh -s -- -y --no-modify-path --profile minimal --default-toolchain '$first'"
for tc in $SBX_RUST_TOOLCHAINS; do
  as_user "rustup toolchain install '$tc' --profile minimal"
  for c in $SBX_RUST_COMPONENTS; do
    as_user "rustup component add '$c' --toolchain '$tc'"
  done
done
CHECK_TOOLS+=" cargo rustc"
