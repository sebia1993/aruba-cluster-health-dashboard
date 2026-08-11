"""SSH collectors."""
"""Read-only collection boundary."""

from .base import (
    NO_PAGING,
    READ_ONLY_COMMANDS,
    SHOW_CLIENT_DISTRIBUTION,
    SHOW_GROUP_MEMBERSHIP,
    SHOW_SWITCHES,
    CollectionAttempt,
    CollectionBundle,
    CommandResult,
    SshConnectionOptions,
    SshOperationError,
)
from .cluster_collector import ClusterCollector, collect_cluster
from .mm_collector import MmCollector, collect_mm

__all__ = [
    "NO_PAGING",
    "READ_ONLY_COMMANDS",
    "SHOW_CLIENT_DISTRIBUTION",
    "SHOW_GROUP_MEMBERSHIP",
    "SHOW_SWITCHES",
    "CollectionAttempt",
    "CollectionBundle",
    "CommandResult",
    "SshConnectionOptions",
    "SshOperationError",
    "ClusterCollector",
    "MmCollector",
    "collect_cluster",
    "collect_mm",
]
