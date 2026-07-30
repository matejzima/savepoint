import io
import tarfile
from unittest.mock import MagicMock

from app import docker_client


def test_put_archive_file_wraps_local_file_and_extracts_at_dest_dir(tmp_path):
    local_path = tmp_path / "source.txt"
    local_path.write_bytes(b"hello world")

    client = MagicMock()
    container = MagicMock()
    client.containers.get.return_value = container

    docker_client.put_archive_file(client, "my-container", "/tmp/dest/target.txt", str(local_path))

    client.containers.get.assert_called_once_with("my-container")
    put_args = container.put_archive.call_args[0]
    assert put_args[0] == "/tmp/dest"

    tar_bytes = put_args[1]
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        members = tar.getmembers()
        assert len(members) == 1
        assert members[0].name == "target.txt"
        assert tar.extractfile(members[0]).read() == b"hello world"


def test_stop_container_calls_stop_on_the_named_container():
    client = MagicMock()
    container = MagicMock()
    client.containers.get.return_value = container

    docker_client.stop_container(client, "my-container")

    client.containers.get.assert_called_once_with("my-container")
    container.stop.assert_called_once()


def test_start_container_calls_start_on_the_named_container():
    client = MagicMock()
    container = MagicMock()
    client.containers.get.return_value = container

    docker_client.start_container(client, "my-container")

    client.containers.get.assert_called_once_with("my-container")
    container.start.assert_called_once()
