from __future__ import annotations

from . import docker_client
from .adapters import ADAPTERS

DISCOVERABLE_ENGINES = ("postgres", "mysql", "mariadb")


def find_candidates(client) -> list:
    """Every running container matching a known database image, regardless of whether
    it's already tracked as a target. Shared by the local /discover route and the
    agent-mode GET /api/discover handler, which has no state db of its own to filter
    "already added" candidates out itself, that filtering happens one level up, on
    whichever side actually knows what's already tracked.
    """
    candidates = []
    for summary in client.containers.list():
        container = docker_client.get_container(client, summary.name)
        for engine in DISCOVERABLE_ENGINES:
            adapter = ADAPTERS[engine]
            if adapter.discover(container):
                info = adapter.default_connection_info(container)
                candidates.append(
                    {
                        "engine": engine,
                        "container_name": container.name,
                        "image": docker_client.get_image_name(container),
                        "db_user": info.get("db_user", ""),
                        "db_name": info.get("db_name", ""),
                    }
                )
                break
    return candidates
