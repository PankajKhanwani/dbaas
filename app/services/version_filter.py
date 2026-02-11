"""
Version Filter Service

Filters database engine versions based on allowed_versions.yaml configuration.
Only versions explicitly allowed in the config will be shown to users.

Supports both legacy (simple list) and flavor-based (nested structure) formats.
"""
import os
import re
from typing import Dict, List, Any, Optional, Union
import yaml
from pathlib import Path

from app.config.logging import get_logger

logger = get_logger(__name__)


class VersionFilterService:
    """Service to filter database versions based on configuration."""

    def __init__(self):
        self.config_path = Path(__file__).parent.parent / "config" / "allowed_versions.yaml"
        self._allowed_versions: Optional[Dict[str, List[str]]] = None  # Legacy format
        self._flavor_config: Optional[Dict[str, Dict[str, Any]]] = None  # New flavor format
        self._config_options: Dict[str, Any] = {}
        self._use_legacy_mode: bool = True
        self._load_config()

    def _load_config(self):
        """Load allowed versions from YAML config file. Supports both legacy and flavor-based formats."""
        try:
            if not self.config_path.exists():
                logger.warning(
                    "version_config_not_found",
                    path=str(self.config_path),
                    message="Allowed versions config not found, allowing all versions"
                )
                self._allowed_versions = {}
                self._flavor_config = {}
                self._config_options = {"allow_all": True}
                self._use_legacy_mode = True
                return

            with open(self.config_path, "r") as f:
                config = yaml.safe_load(f)

            self._config_options = config.get("config", {})

            # Detect format by checking first engine entry
            detected_format = self._detect_config_format(config)

            if detected_format == "flavor":
                # New flavor-based format
                self._use_legacy_mode = False
                self._flavor_config = {
                    k: v for k, v in config.items()
                    if k != "config" and isinstance(v, dict) and "flavors" in v
                }
                self._allowed_versions = {}  # Not used in flavor mode

                logger.info(
                    "version_config_loaded",
                    format="flavor-based",
                    engines=list(self._flavor_config.keys()),
                    allow_all=self._config_options.get("allow_all", False),
                )
            else:
                # Legacy simple list format
                self._use_legacy_mode = True
                self._allowed_versions = {
                    k: v for k, v in config.items() if k != "config" and isinstance(v, list)
                }
                self._flavor_config = {}  # Not used in legacy mode

                logger.info(
                    "version_config_loaded",
                    format="legacy",
                    engines=list(self._allowed_versions.keys()),
                    allow_all=self._config_options.get("allow_all", False),
                    total_versions=sum(len(v) for v in self._allowed_versions.values())
                )

        except Exception as e:
            logger.error(
                "failed_to_load_version_config",
                path=str(self.config_path),
                error=str(e),
                exc_info=True
            )
            # Fallback: allow all versions if config fails to load
            self._allowed_versions = {}
            self._flavor_config = {}
            self._config_options = {"allow_all": True}
            self._use_legacy_mode = True

    def _detect_config_format(self, config: Dict[str, Any]) -> str:
        """
        Detect whether config uses legacy (list) or new (flavor) format.

        Args:
            config: Parsed YAML configuration

        Returns:
            "legacy" or "flavor"
        """
        # Check first non-config key to determine format
        for key, value in config.items():
            if key == "config":
                continue

            if isinstance(value, list):
                return "legacy"
            elif isinstance(value, dict) and "flavors" in value:
                return "flavor"

        # Default to legacy if no engine entries found
        return "legacy"

    def reload_config(self):
        """Reload configuration from file (useful for hot-reload)."""
        logger.info("reloading_version_config")
        self._load_config()

    def _match_flavor_pattern(self, version_name: str, pattern: str) -> bool:
        """
        Match version name against flavor regex pattern.

        Args:
            version_name: Version name from KubeDB (e.g., "percona-7.0.14", "16.4-bookworm")
            pattern: Regex pattern (e.g., "^percona-", ".*-bookworm$")

        Returns:
            True if version name matches pattern, False otherwise
        """
        try:
            return bool(re.match(pattern, version_name))
        except re.error as e:
            logger.error(
                "invalid_flavor_pattern",
                pattern=pattern,
                error=str(e)
            )
            return False

    def _detect_version_flavor(self, engine: str, version_name: str) -> Optional[str]:
        """
        Detect which flavor a version belongs to based on name pattern matching.

        Args:
            engine: Database engine name
            version_name: Version name from KubeDB metadata.name field

        Returns:
            Flavor name (e.g., "official", "percona", "bookworm") or None if no match
        """
        if self._use_legacy_mode or not self._flavor_config:
            return None

        engine_config = self._flavor_config.get(engine, {})
        flavors = engine_config.get("flavors", {})

        for flavor_name, flavor_config in flavors.items():
            pattern = flavor_config.get("name_pattern")
            if pattern and self._match_flavor_pattern(version_name, pattern):
                return flavor_name

        return None

    def _filter_versions_by_flavor(self, engine: str, versions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Filter versions based on flavor configuration (enabled/disabled, pattern matching).

        For each version, checks ALL flavors to see which one(s) claim it in their allowed list.
        Prefers enabled flavors over disabled ones when patterns overlap.

        Args:
            engine: Database engine name
            versions: List of version dicts from KubeDB

        Returns:
            Filtered list containing only versions from enabled flavors
        """
        if self._use_legacy_mode or not self._flavor_config:
            return versions

        engine_config = self._flavor_config.get(engine, {})
        if not engine_config:
            logger.debug(
                "no_flavor_config_for_engine",
                engine=engine,
                message="No flavor config found, returning all versions"
            )
            return versions

        flavors = engine_config.get("flavors", {})
        if not flavors:
            return versions

        filtered = []
        for version_info in versions:
            version_name = version_info.get("name", "")
            version_str = version_info.get("version", "")

            # Check ALL flavors to see which ones claim this version
            # (since patterns can overlap, e.g., redis official and valkey both match "^[0-9]")
            matched_flavor = None

            for flavor_name, flavor_config in flavors.items():
                enabled = flavor_config.get("enabled", False)
                pattern = flavor_config.get("name_pattern", "")
                allowed_versions = flavor_config.get("versions", [])

                # Check if version is in this flavor's allowed list
                if version_name in allowed_versions or version_str in allowed_versions:
                    # Also verify pattern matches (as a sanity check)
                    if pattern and self._match_flavor_pattern(version_name, pattern):
                        if enabled:
                            matched_flavor = flavor_name
                            break  # Found an enabled flavor that claims this version
                        elif not matched_flavor:
                            # Remember disabled flavor in case no enabled flavor claims it
                            matched_flavor = (flavor_name, False)  # Tuple indicates disabled

            # Process the match result
            if matched_flavor:
                if isinstance(matched_flavor, tuple):
                    # Matched a disabled flavor
                    flavor_name, _ = matched_flavor
                    logger.debug(
                        "version_filtered_out",
                        engine=engine,
                        version=version_name,
                        flavor=flavor_name,
                        reason="flavor disabled"
                    )
                else:
                    # Matched an enabled flavor
                    filtered.append(version_info)
                    logger.debug(
                        "version_allowed",
                        engine=engine,
                        version=version_name,
                        flavor=matched_flavor,
                        reason="flavor enabled and version in allowed list"
                    )
            else:
                # Version not claimed by any flavor
                logger.debug(
                    "version_no_flavor_match",
                    engine=engine,
                    version=version_name,
                    message="Version not in any flavor's allowed list"
                )

        logger.info(
            "flavors_filtered",
            engine=engine,
            total_from_kubedb=len(versions),
            allowed_count=len(filtered),
            filtered_out=len(versions) - len(filtered)
        )

        return filtered

    def filter_versions(self, engine: str, versions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Filter version list based on allowed versions configuration.
        Supports both legacy (simple list) and flavor-based filtering.

        Args:
            engine: Database engine name (mongodb, postgres, mysql, etc.)
            versions: List of version dicts from KubeDB (each has 'version' field)

        Returns:
            Filtered list of versions that are allowed by config
        """
        # If allow_all is true, return all versions
        if self._config_options.get("allow_all", False):
            logger.debug(
                "version_filter_bypassed",
                engine=engine,
                reason="allow_all is enabled"
            )
            return versions

        # Use flavor-based filtering if in flavor mode
        if not self._use_legacy_mode:
            return self._filter_versions_by_flavor(engine, versions)

        # Legacy mode: simple list filtering
        allowed = self._allowed_versions.get(engine, [])
        if not allowed:
            logger.warning(
                "no_version_config_for_engine",
                engine=engine,
                message="No allowed versions configured for this engine, returning all"
            )
            return versions

        # Filter versions
        filtered = []
        for version_info in versions:
            version_str = version_info.get("version", "")
            if version_str in allowed:
                filtered.append(version_info)
            else:
                logger.debug(
                    "version_filtered_out",
                    engine=engine,
                    version=version_str,
                    reason="not in allowed list"
                )

        logger.info(
            "versions_filtered",
            engine=engine,
            total_from_kubedb=len(versions),
            allowed_count=len(filtered),
            filtered_out=len(versions) - len(filtered)
        )

        return filtered

    def filter_all_engines(self, engines_dict: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
        """
        Filter all engine versions in a dictionary.

        Args:
            engines_dict: Dictionary with engine names as keys and version lists as values

        Returns:
            Filtered dictionary with only allowed versions
        """
        if self._config_options.get("allow_all", False):
            return engines_dict

        filtered_dict = {}
        for engine, versions in engines_dict.items():
            filtered_versions = self.filter_versions(engine, versions)
            # Only include engine if it has at least one allowed version
            if filtered_versions:
                filtered_dict[engine] = filtered_versions
            else:
                logger.warning(
                    "engine_excluded_no_allowed_versions",
                    engine=engine,
                    total_versions=len(versions)
                )

        return filtered_dict

    def get_allowed_versions_for_engine(self, engine: str) -> List[str]:
        """
        Get list of allowed version strings for an engine.
        In flavor mode, returns all versions from enabled flavors.

        Args:
            engine: Database engine name

        Returns:
            List of allowed version strings
        """
        if self._config_options.get("allow_all", False):
            return []  # Empty list means "all allowed"

        if not self._use_legacy_mode:
            # Flavor mode: collect versions from all enabled flavors
            engine_config = self._flavor_config.get(engine, {})
            flavors = engine_config.get("flavors", {})

            all_versions = []
            for flavor_name, flavor_config in flavors.items():
                if flavor_config.get("enabled", False):
                    versions = flavor_config.get("versions", [])
                    all_versions.extend(versions)

            return all_versions

        return self._allowed_versions.get(engine, [])

    def is_version_allowed(self, engine: str, version: str) -> bool:
        """
        Check if a specific version is allowed for an engine.
        In flavor mode, checks if version is in any enabled flavor's allowed list.

        Args:
            engine: Database engine name
            version: Version string to check

        Returns:
            True if version is allowed, False otherwise
        """
        if self._config_options.get("allow_all", False):
            return True

        if not self._use_legacy_mode:
            # Flavor mode: check all enabled flavors
            allowed_versions = self.get_allowed_versions_for_engine(engine)
            if not allowed_versions:  # No config = allow all
                return True
            return version in allowed_versions

        allowed = self._allowed_versions.get(engine, [])
        if not allowed:  # No config for this engine = allow all
            return True

        return version in allowed


# Global instance
version_filter_service = VersionFilterService()
