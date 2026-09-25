import os

# The tests see host/defaults.conf only, never this setup's host/local.conf.
os.environ["SBX_LOCAL_CONF"] = os.devnull
