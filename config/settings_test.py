"""Settings for pytest: the normal settings with TESTING on."""

import os

os.environ.setdefault("TESTING", "1")

from .settings import *  # noqa: E402,F403
