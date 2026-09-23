"""Test config for the brain (pure-logic) suite.

The scheduler's pure logic lives in custom_components/geodrops_rachio/brain.
Putting the integration directory on sys.path lets these tests import it as a
standalone top-level package (`brain`) without importing Home Assistant, so
they run on any platform:
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests_brain`.
"""
import os
import sys
from importlib.util import spec_from_file_location, module_from_spec

_HERE = os.path.dirname(os.path.abspath(__file__))
_BRAIN_PATH = os.path.join(_HERE, "..", "custom_components", "geodrops_rachio", "brain")

# Load brain package directly to avoid shadowing stdlib modules
_spec = spec_from_file_location("brain", os.path.join(_BRAIN_PATH, "__init__.py"), submodule_search_locations=[_BRAIN_PATH])
_brain = module_from_spec(_spec)
sys.modules["brain"] = _brain
_spec.loader.exec_module(_brain)

import pytest  # noqa: E402


@pytest.fixture
def example_config_path():
    return os.path.join(_HERE, "examples", "config.example.yaml")
