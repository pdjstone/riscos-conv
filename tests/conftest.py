"""pytest fixtures shared across the per-format test files."""

import pytest
from helpers import build_adf, build_hfe


@pytest.fixture
def adf_bytes():
    return build_adf()


@pytest.fixture
def hfe_bytes():
    return build_hfe()