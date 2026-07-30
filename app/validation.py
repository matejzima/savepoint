from __future__ import annotations

from docker.errors import NotFound

from . import docker_client


def validate_connection_fields(client, engine, container_name, db_user, db_name, file_path):
    """Shared by local create/edit and the agent-mode /api/validate handler: same rules,
    same order, everywhere a target's connection details are validated, so none of these
    call sites can ever drift apart on what "valid" means. Returns an error message, or
    None if valid.
    """
    if engine == "sqlite":
        if not file_path:
            return "file path is required for sqlite targets"
    else:
        if not db_user or not db_name:
            return "DB user and DB name are required for this engine"

    try:
        docker_client.get_container(client, container_name)
    except NotFound:
        return f"container '{container_name}' not found"

    if engine == "sqlite" and not docker_client.path_exists_in_container(client, container_name, file_path):
        return f"file '{file_path}' not found in container '{container_name}'"

    return None
