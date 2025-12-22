"""
KubeDB Installer Service - Production Grade Installation
Handles automatic KubeDB installation on provider clusters.
"""
import asyncio
import tempfile
import os
from typing import Optional, Dict, Any
from pathlib import Path

import structlog
from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = structlog.get_logger()


class KubeDBInstaller:
    """Production-grade KubeDB installer service."""

    def __init__(self):
        """Initialize KubeDB installer."""
        self.kubedb_version = "v2024.11.18"  # Latest stable version
        self.kubedb_namespace = "kubedb"
        self.helm_chart = "oci://ghcr.io/appscode-charts/kubedb"

    async def check_kubedb_installed(
        self,
        kubeconfig_content: Optional[str] = None,
        kubeconfig_path: Optional[str] = None
    ) -> bool:
        """
        Check if KubeDB is already installed in the cluster.

        Args:
            kubeconfig_content: Kubeconfig file content
            kubeconfig_path: Path to kubeconfig file

        Returns:
            True if KubeDB is installed, False otherwise
        """
        try:
            # Load kubeconfig
            if kubeconfig_content:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.kubeconfig', delete=False) as f:
                    f.write(kubeconfig_content)
                    temp_kubeconfig = f.name

                config.load_kube_config(config_file=temp_kubeconfig)
                os.unlink(temp_kubeconfig)
            elif kubeconfig_path:
                config.load_kube_config(config_file=kubeconfig_path)
            else:
                config.load_kube_config()

            # Check if kubedb namespace exists
            v1 = client.CoreV1Api()
            try:
                v1.read_namespace(name=self.kubedb_namespace)
                logger.info("kubedb_namespace_exists", namespace=self.kubedb_namespace)
            except ApiException as e:
                if e.status == 404:
                    logger.info("kubedb_namespace_not_found")
                    return False
                raise

            # Check if KubeDB CRDs are installed
            api_client = client.ApiextensionsV1Api()
            crds = api_client.list_custom_resource_definition()

            kubedb_crds = [
                "mongodbs.kubedb.com",
                "postgreses.kubedb.com",
                "mysqls.kubedb.com",
                "redises.kubedb.com",
                "elasticsearches.kubedb.com"
            ]

            installed_crds = [crd.metadata.name for crd in crds.items]
            kubedb_installed = any(crd in installed_crds for crd in kubedb_crds)

            if kubedb_installed:
                logger.info("kubedb_crds_found", crds=len([c for c in kubedb_crds if c in installed_crds]))
                return True
            else:
                logger.info("kubedb_crds_not_found")
                return False

        except Exception as e:
            logger.error("failed_to_check_kubedb_installation", error=str(e), exc_info=True)
            return False

    async def install_kubedb(
        self,
        kubeconfig_content: Optional[str] = None,
        kubeconfig_path: Optional[str] = None,
        license_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Install KubeDB on the Kubernetes cluster using Helm.

        Args:
            kubeconfig_content: Kubeconfig file content
            kubeconfig_path: Path to kubeconfig file
            license_content: KubeDB license content (optional, will use community edition if not provided)

        Returns:
            Installation result dictionary

        Raises:
            Exception: If installation fails
        """
        try:
            logger.info(
                "starting_kubedb_installation",
                version=self.kubedb_version,
                namespace=self.kubedb_namespace,
            )

            # Check if already installed
            is_installed = await self.check_kubedb_installed(
                kubeconfig_content=kubeconfig_content,
                kubeconfig_path=kubeconfig_path
            )

            if is_installed:
                logger.info("kubedb_already_installed")
                return {
                    "status": "already_installed",
                    "message": "KubeDB is already installed on this cluster",
                    "version": self.kubedb_version,
                }

            # Prepare kubeconfig file
            temp_kubeconfig = None
            if kubeconfig_content:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.kubeconfig', delete=False) as f:
                    f.write(kubeconfig_content)
                    temp_kubeconfig = f.name
            elif kubeconfig_path:
                temp_kubeconfig = kubeconfig_path
            else:
                temp_kubeconfig = os.path.expanduser("~/.kube/config")

            # Prepare license file if provided
            temp_license = None
            if license_content:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
                    f.write(license_content)
                    temp_license = f.name

            # Build helm install command
            helm_cmd = [
                "helm", "install", "kubedb",
                self.helm_chart,
                "--version", self.kubedb_version,
                "--namespace", self.kubedb_namespace,
                "--create-namespace",
                "--kubeconfig", temp_kubeconfig,
                "--wait",
                "--timeout", "10m",
                "--burst-limit=10000",
            ]

            # Add license if provided, otherwise use community edition
            if temp_license:
                helm_cmd.extend(["--set-file", f"global.license={temp_license}"])
                logger.info("installing_kubedb_with_enterprise_license")
            else:
                # Community edition - no license required
                logger.info("installing_kubedb_community_edition")

            # Execute helm install
            logger.info("executing_helm_install", command=" ".join(helm_cmd))

            process = await asyncio.create_subprocess_exec(
                *helm_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await process.communicate()

            # Clean up temp files
            if temp_license:
                os.unlink(temp_license)
            if kubeconfig_content and temp_kubeconfig:
                os.unlink(temp_kubeconfig)

            if process.returncode != 0:
                error_msg = stderr.decode() if stderr else "Unknown error"
                logger.error(
                    "kubedb_installation_failed",
                    return_code=process.returncode,
                    error=error_msg,
                )
                raise Exception(f"KubeDB installation failed: {error_msg}")

            logger.info(
                "kubedb_installation_successful",
                version=self.kubedb_version,
                namespace=self.kubedb_namespace,
            )

            return {
                "status": "installed",
                "message": "KubeDB installed successfully",
                "version": self.kubedb_version,
                "namespace": self.kubedb_namespace,
                "output": stdout.decode() if stdout else "",
            }

        except Exception as e:
            logger.error("kubedb_installation_error", error=str(e), exc_info=True)
            raise

    async def verify_installation(
        self,
        kubeconfig_content: Optional[str] = None,
        kubeconfig_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Verify KubeDB installation by checking CRDs and operator pods.

        Args:
            kubeconfig_content: Kubeconfig file content
            kubeconfig_path: Path to kubeconfig file

        Returns:
            Verification result dictionary
        """
        try:
            # Load kubeconfig
            if kubeconfig_content:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.kubeconfig', delete=False) as f:
                    f.write(kubeconfig_content)
                    temp_kubeconfig = f.name

                config.load_kube_config(config_file=temp_kubeconfig)
                os.unlink(temp_kubeconfig)
            elif kubeconfig_path:
                config.load_kube_config(config_file=kubeconfig_path)
            else:
                config.load_kube_config()

            v1 = client.CoreV1Api()
            api_client = client.ApiextensionsV1Api()

            # Check CRDs
            crds = api_client.list_custom_resource_definition()
            kubedb_crds = [
                crd.metadata.name for crd in crds.items
                if "kubedb.com" in crd.metadata.name
            ]

            # Check operator pods
            pods = v1.list_namespaced_pod(
                namespace=self.kubedb_namespace,
                label_selector="app.kubernetes.io/instance=kubedb"
            )

            running_pods = [
                pod.metadata.name for pod in pods.items
                if pod.status.phase == "Running"
            ]

            all_ready = all(
                all(
                    container.ready
                    for container in pod.status.container_statuses or []
                )
                for pod in pods.items
            )

            logger.info(
                "kubedb_verification_complete",
                crds_count=len(kubedb_crds),
                total_pods=len(pods.items),
                running_pods=len(running_pods),
                all_ready=all_ready,
            )

            return {
                "installed": len(kubedb_crds) > 0,
                "crds_count": len(kubedb_crds),
                "crds": kubedb_crds,
                "total_pods": len(pods.items),
                "running_pods": running_pods,
                "all_pods_ready": all_ready,
            }

        except Exception as e:
            logger.error("kubedb_verification_failed", error=str(e), exc_info=True)
            return {
                "installed": False,
                "error": str(e),
            }

    async def uninstall_kubedb(
        self,
        kubeconfig_content: Optional[str] = None,
        kubeconfig_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Uninstall KubeDB from the cluster.

        Args:
            kubeconfig_content: Kubeconfig file content
            kubeconfig_path: Path to kubeconfig file

        Returns:
            Uninstallation result dictionary
        """
        try:
            logger.info("starting_kubedb_uninstallation")

            # Prepare kubeconfig file
            temp_kubeconfig = None
            if kubeconfig_content:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.kubeconfig', delete=False) as f:
                    f.write(kubeconfig_content)
                    temp_kubeconfig = f.name
            elif kubeconfig_path:
                temp_kubeconfig = kubeconfig_path
            else:
                temp_kubeconfig = os.path.expanduser("~/.kube/config")

            # Build helm uninstall command
            helm_cmd = [
                "helm", "uninstall", "kubedb",
                "--namespace", self.kubedb_namespace,
                "--kubeconfig", temp_kubeconfig,
                "--wait",
            ]

            logger.info("executing_helm_uninstall", command=" ".join(helm_cmd))

            process = await asyncio.create_subprocess_exec(
                *helm_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await process.communicate()

            # Clean up temp kubeconfig
            if kubeconfig_content and temp_kubeconfig:
                os.unlink(temp_kubeconfig)

            if process.returncode != 0:
                error_msg = stderr.decode() if stderr else "Unknown error"
                logger.error(
                    "kubedb_uninstallation_failed",
                    return_code=process.returncode,
                    error=error_msg,
                )
                raise Exception(f"KubeDB uninstallation failed: {error_msg}")

            logger.info("kubedb_uninstallation_successful")

            return {
                "status": "uninstalled",
                "message": "KubeDB uninstalled successfully",
                "output": stdout.decode() if stdout else "",
            }

        except Exception as e:
            logger.error("kubedb_uninstallation_error", error=str(e), exc_info=True)
            raise


# Global instance
kubedb_installer = KubeDBInstaller()
