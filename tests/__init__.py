import os

# The tests see host/defaults.conf only, never this setup's host/local.conf.
os.environ["SBX_LOCAL_CONF"] = os.devnull
# The secrets go through `security`, which the tests fake, on every system.
# tests/test_secretstore.py covers the file store.
os.environ["SBX_SECRET_STORE"] = "keychain"
