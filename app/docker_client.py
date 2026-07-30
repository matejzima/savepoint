from __future__ import annotations

import io
import tarfile

import docker
from docker.errors import NotFound


def get_client() -> docker.DockerClient:
    return docker.from_env()


def get_container(client: docker.DockerClient, name: str):
    return client.containers.get(name)


def parse_env(container) -> dict:
    env_list = container.attrs.get("Config", {}).get("Env") or []
    env = {}
    for item in env_list:
        if "=" in item:
            key, value = item.split("=", 1)
            env[key] = value
    return env


def get_image_name(container) -> str:
    return (container.attrs.get("Config", {}).get("Image") or "").lower()


def get_container_env(client: docker.DockerClient, name: str) -> dict:
    return parse_env(get_container(client, name))


def exec_and_capture(client, container_name: str, cmd, environment: dict, dest_path: str):
    """Run a command inside the container, streaming stdout to dest_path.

    Returns (exit_code, stderr_text).
    """
    container = get_container(client, container_name)
    exec_id = client.api.exec_create(container.id, cmd, environment=environment)["Id"]
    stream = client.api.exec_start(exec_id, stream=True, demux=True)

    stderr_chunks = []
    with open(dest_path, "wb") as f:
        for stdout_chunk, stderr_chunk in stream:
            if stdout_chunk:
                f.write(stdout_chunk)
            if stderr_chunk:
                stderr_chunks.append(stderr_chunk)

    exit_code = client.api.exec_inspect(exec_id)["ExitCode"]
    stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    return exit_code, stderr_text


def exec_pg_dump(client, container_name: str, user: str, db_name: str, password: str, dest_path: str):
    cmd = ["pg_dump", "-U", user, "-d", db_name, "-Fc"]
    return exec_and_capture(client, container_name, cmd, {"PGPASSWORD": password}, dest_path)


def exec_simple(client, container_name: str, cmd, environment: dict | None = None):
    """Run a short command inside the container without streaming output to a file.

    Returns (exit_code, stdout_text, stderr_text). Raises docker.errors.NotFound if the
    container is gone, docker.errors.APIError if the command itself can't be started
    (e.g. the binary isn't present in the container).
    """
    container = get_container(client, container_name)
    exec_id = client.api.exec_create(container.id, cmd, environment=environment or {})["Id"]
    stdout_bytes, stderr_bytes = client.api.exec_start(exec_id, stream=False, demux=True)
    exit_code = client.api.exec_inspect(exec_id)["ExitCode"]
    stdout_text = (stdout_bytes or b"").decode("utf-8", errors="replace")
    stderr_text = (stderr_bytes or b"").decode("utf-8", errors="replace")
    return exit_code, stdout_text, stderr_text


def get_archive_file(client, container_name: str, path: str, dest_path: str) -> None:
    """Extract a single file from the container's filesystem at `path` into dest_path.

    Raises docker.errors.NotFound if the path doesn't exist in the container.
    """
    container = get_container(client, container_name)
    stream, _stat = container.get_archive(path)
    tar_bytes = io.BytesIO()
    for chunk in stream:
        tar_bytes.write(chunk)
    tar_bytes.seek(0)
    with tarfile.open(fileobj=tar_bytes) as tar:
        member = tar.getmembers()[0]
        extracted = tar.extractfile(member)
        with open(dest_path, "wb") as f:
            f.write(extracted.read())


def path_exists_in_container(client, container_name: str, path: str) -> bool:
    container = get_container(client, container_name)
    try:
        container.get_archive(path)
        return True
    except NotFound:
        return False


def put_archive_file(client, container_name: str, dest_path: str, local_path: str) -> None:
    """Push a single local file into the container's filesystem at dest_path (a full file
    path, not a directory). Mirrors get_archive_file() in the opposite direction: Docker's
    put_archive() takes a directory plus tar bytes and extracts them there, so this wraps
    local_path in an in-memory tar whose sole member is named after dest_path's basename,
    then extracts it into dest_path's parent directory.

    Raises docker.errors.NotFound if the container is gone.
    """
    container = get_container(client, container_name)
    dest_dir, dest_name = dest_path.rsplit("/", 1)
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w") as tar:
        tar.add(local_path, arcname=dest_name)
    tar_bytes.seek(0)
    container.put_archive(dest_dir, tar_bytes.read())


def stop_container(client, container_name: str) -> None:
    get_container(client, container_name).stop()


def start_container(client, container_name: str) -> None:
    get_container(client, container_name).start()
