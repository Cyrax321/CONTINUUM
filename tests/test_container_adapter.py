import subprocess
from unittest.mock import MagicMock, patch

from continuum.adapters.container import ContainerAdapter
from tests.test_environment_adapters import _storage


def test_container_adapter_init():
    storage = _storage()
    adapter = ContainerAdapter(storage, "alpine:latest")
    assert adapter.storage == storage
    assert adapter.image == "alpine:latest"


def test_available_when_docker_present():
    with patch("continuum.adapters.container.shutil.which", return_value="/usr/bin/docker"):
        assert ContainerAdapter.available() is True


def test_available_when_docker_missing():
    with patch("continuum.adapters.container.shutil.which", return_value=None):
        assert ContainerAdapter.available() is False


def test_run_in_container_happy_path():
    storage = _storage()
    adapter = ContainerAdapter(storage, "alpine:latest")

    mock_completed = MagicMock()
    mock_completed.returncode = 0
    mock_completed.stdout = "hello"
    mock_completed.stderr = ""

    with (
        patch("continuum.adapters.container.shutil.which", return_value="/usr/bin/docker"),
        patch("continuum.adapters.container.subprocess.run", return_value=mock_completed),
    ):
        result = adapter.run_in_container("run_1", "echo hi")
        assert result.status == "completed"
        assert result.output == "hello"
        assert result.error is None


def test_run_in_container_called_process_error():
    storage = _storage()
    adapter = ContainerAdapter(storage, "alpine:latest")

    cpe = subprocess.CalledProcessError(
        returncode=1,
        cmd="docker run ...",
        output="output",
        stderr="container failed",
    )

    with (
        patch("continuum.adapters.container.shutil.which", return_value="/usr/bin/docker"),
        patch("continuum.adapters.container.subprocess.run", side_effect=cpe),
    ):
        result = adapter.run_in_container("run_1", "exit 1")
        assert result.status == "failed"
        assert result.error is not None
