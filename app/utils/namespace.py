"""
Utility functions for Kubernetes namespace management.
"""
import re
from typing import Optional


def generate_namespace_name(domain: str, project: str) -> str:
    """
    Generate a Kubernetes-compliant namespace name from domain and project.
    
    Kubernetes namespace naming rules:
    - Must be lowercase alphanumeric characters or hyphens
    - Must start and end with alphanumeric character
    - Maximum 63 characters
    - Cannot contain consecutive hyphens
    
    Args:
        domain: Domain name
        project: Project name
        
    Returns:
        Valid Kubernetes namespace name (format: domain-project)
        
    Examples:
        generate_namespace_name("test-d", "test-p") -> "test-d-test-p"
        generate_namespace_name("my-domain", "my-project") -> "my-domain-my-project"
        generate_namespace_name("DOMAIN", "PROJECT") -> "domain-project"
    """
    # Normalize: lowercase, replace invalid chars with hyphens
    normalized_domain = re.sub(r'[^a-z0-9-]', '-', domain.lower())
    normalized_project = re.sub(r'[^a-z0-9-]', '-', project.lower())
    
    # Remove consecutive hyphens
    normalized_domain = re.sub(r'-+', '-', normalized_domain)
    normalized_project = re.sub(r'-+', '-', normalized_project)
    
    # Remove leading/trailing hyphens
    normalized_domain = normalized_domain.strip('-')
    normalized_project = normalized_project.strip('-')
    
    # Combine with hyphen
    namespace = f"{normalized_domain}-{normalized_project}"
    
    # Ensure it starts and ends with alphanumeric
    namespace = namespace.strip('-')
    
    # Truncate to 63 characters (Kubernetes limit)
    if len(namespace) > 63:
        # Try to keep both domain and project, but truncate if needed
        max_domain_len = min(len(normalized_domain), 31)
        max_project_len = min(len(normalized_project), 31)
        namespace = f"{normalized_domain[:max_domain_len]}-{normalized_project[:max_project_len]}"
        namespace = namespace[:63].rstrip('-')
    
    # Final validation: must be non-empty and start/end with alphanumeric
    if not namespace:
        namespace = "default"
    elif not re.match(r'^[a-z0-9]', namespace):
        # Doesn't start with alphanumeric, add prefix
        namespace = f"ns-{namespace}"
        namespace = namespace[:63].rstrip('-')
    elif not re.match(r'[a-z0-9]$', namespace):
        # Doesn't end with alphanumeric, remove trailing hyphens
        namespace = namespace.rstrip('-')
    
    return namespace


def validate_namespace_name(namespace: str) -> bool:
    """
    Validate if a namespace name is Kubernetes-compliant.
    
    Args:
        namespace: Namespace name to validate
        
    Returns:
        True if valid, False otherwise
    """
    if not namespace:
        return False
    
    # Must be 1-63 characters
    if len(namespace) > 63:
        return False
    
    # Must match pattern: lowercase alphanumeric or hyphen, start/end with alphanumeric
    pattern = r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$'
    return bool(re.match(pattern, namespace))

