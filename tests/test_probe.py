import shutil

import pytest

from hermes_control.engines.registry import get_adapter

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_probe_finds_the_real_binary(engine):
    if not shutil.which(engine):
        pytest.skip(f"{engine} not installed")
    info = get_adapter(engine).probe()
    assert info is not None
    assert info.name == engine
    assert info.version
