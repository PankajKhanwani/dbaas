"""
Version comparison utilities for database version upgrades.
Handles semantic versioning comparison and upgrade type detection.
"""
import re
from typing import Tuple, Optional
from enum import Enum


class UpgradeType(str, Enum):
    """Types of version upgrades."""

    PATCH = "patch"  # e.g., 8.0.35 -> 8.0.36
    MINOR = "minor"  # e.g., 8.0.x -> 8.2.x
    MAJOR = "major"  # e.g., 8.x.x -> 9.x.x
    UNKNOWN = "unknown"


def parse_version(version: str) -> Tuple[int, int, int]:
    """
    Parse a version string into (major, minor, patch) tuple.

    Supports various version formats:
    - "16.4" -> (16, 4, 0)
    - "8.0.36" -> (8, 0, 36)
    - "7.0.8" -> (7, 0, 8)
    - "8.0.35-debian" -> (8, 0, 35)  # Ignores suffix
    - "xpack-8.8.2" -> (8, 8, 2)  # Handles prefix
    - "percona-7.0.4" -> (7, 0, 4)  # Handles prefix

    Args:
        version: Version string to parse

    Returns:
        Tuple of (major, minor, patch) as integers

    Raises:
        ValueError: If version string cannot be parsed
    """
    # Remove common prefixes
    version = re.sub(r"^(xpack-|percona-|opensearch-|searchguard-|timescaledb-.*-pg)", "", version)

    # Extract version numbers (first occurrence of X.Y.Z or X.Y pattern)
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", version)

    if not match:
        raise ValueError(f"Cannot parse version: {version}")

    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3)) if match.group(3) else 0

    return (major, minor, patch)


def compare_versions(version1: str, version2: str) -> int:
    """
    Compare two version strings.

    Args:
        version1: First version string
        version2: Second version string

    Returns:
        -1 if version1 < version2
         0 if version1 == version2
         1 if version1 > version2

    Raises:
        ValueError: If versions cannot be parsed
    """
    try:
        v1 = parse_version(version1)
        v2 = parse_version(version2)

        if v1 < v2:
            return -1
        elif v1 > v2:
            return 1
        else:
            return 0
    except ValueError as e:
        raise ValueError(f"Error comparing versions '{version1}' and '{version2}': {str(e)}")


def is_upgrade(current: str, target: str) -> bool:
    """
    Check if target version is an upgrade from current version.

    Args:
        current: Current version string
        target: Target version string

    Returns:
        True if target > current, False otherwise
    """
    try:
        return compare_versions(current, target) < 0
    except ValueError:
        return False


def get_upgrade_type(current: str, target: str) -> UpgradeType:
    """
    Determine the type of upgrade between two versions.

    Args:
        current: Current version string
        target: Target version string

    Returns:
        UpgradeType enum value (PATCH, MINOR, MAJOR, or UNKNOWN)
    """
    try:
        current_parts = parse_version(current)
        target_parts = parse_version(target)

        # Not an upgrade if target <= current
        if target_parts <= current_parts:
            return UpgradeType.UNKNOWN

        # Major upgrade: major version differs
        if target_parts[0] != current_parts[0]:
            return UpgradeType.MAJOR

        # Minor upgrade: minor version differs
        if target_parts[1] != current_parts[1]:
            return UpgradeType.MINOR

        # Patch upgrade: only patch version differs
        if target_parts[2] != current_parts[2]:
            return UpgradeType.PATCH

        return UpgradeType.UNKNOWN

    except ValueError:
        return UpgradeType.UNKNOWN


def is_upgrade_compatible(current: str, target: str, allow_major: bool = False) -> Tuple[bool, Optional[str]]:
    """
    Check if upgrade from current to target version is compatible.

    Args:
        current: Current version string
        target: Target version string
        allow_major: Whether to allow major version upgrades

    Returns:
        Tuple of (is_compatible, reason_if_not_compatible)
    """
    if not is_upgrade(current, target):
        return False, "Target version is not newer than current version"

    upgrade_type = get_upgrade_type(current, target)

    if upgrade_type == UpgradeType.MAJOR and not allow_major:
        return False, "Major version upgrades require manual approval and may not be supported"

    if upgrade_type == UpgradeType.UNKNOWN:
        return False, "Cannot determine upgrade type"

    return True, None


def get_latest_patch_version(current: str, available_versions: list[str]) -> Optional[str]:
    """
    Find the latest patch version for the current major.minor version.

    Args:
        current: Current version string
        available_versions: List of available version strings

    Returns:
        Latest patch version string, or None if no upgrades available
    """
    try:
        current_parts = parse_version(current)
        current_major_minor = (current_parts[0], current_parts[1])

        # Filter to same major.minor, higher patch
        patch_upgrades = []
        for version in available_versions:
            try:
                v_parts = parse_version(version)
                if (v_parts[0], v_parts[1]) == current_major_minor and v_parts[2] > current_parts[2]:
                    patch_upgrades.append((v_parts, version))
            except ValueError:
                continue

        if not patch_upgrades:
            return None

        # Return the highest patch version
        patch_upgrades.sort(key=lambda x: x[0], reverse=True)
        return patch_upgrades[0][1]

    except ValueError:
        return None


def get_latest_minor_version(current: str, available_versions: list[str]) -> Optional[str]:
    """
    Find the latest minor version for the current major version.

    Args:
        current: Current version string
        available_versions: List of available version strings

    Returns:
        Latest minor version string, or None if no upgrades available
    """
    try:
        current_parts = parse_version(current)
        current_major = current_parts[0]

        # Filter to same major, higher or equal minor
        minor_upgrades = []
        for version in available_versions:
            try:
                v_parts = parse_version(version)
                if v_parts[0] == current_major and v_parts >= current_parts:
                    minor_upgrades.append((v_parts, version))
            except ValueError:
                continue

        if not minor_upgrades:
            return None

        # Return the highest minor version
        minor_upgrades.sort(key=lambda x: x[0], reverse=True)
        return minor_upgrades[0][1]

    except ValueError:
        return None
