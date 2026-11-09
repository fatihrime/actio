"""Hypothesis profiles. Pick one with HYPOTHESIS_PROFILE=dev|ci|nightly (default: dev)."""

import os

from hypothesis import settings

settings.register_profile("dev", max_examples=100)
settings.register_profile("ci", max_examples=200, print_blob=True)
settings.register_profile("nightly", max_examples=5_000, print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
