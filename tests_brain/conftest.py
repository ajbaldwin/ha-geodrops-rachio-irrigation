"""Test config for the in-tree brain (bundled_app) suite.

The scheduler brain is vendored into custom_components/geodrops_rachio/bundled_app
as the namespaced package `geodrops_rachio_lib`. These are its pure-logic unit
tests, moved here when the standalone scheduler repo was merged in. They run on
any platform (no HA stack): `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain`.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(
    0,
    os.path.join(_HERE, "..", "custom_components", "geodrops_rachio", "bundled_app"),
)

import pytest  # noqa: E402


@pytest.fixture
def example_config_path():
    return os.path.join(_HERE, "examples", "config.example.yaml")
