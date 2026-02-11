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


def generate_database_namespace(domain: str, project: str, database_name: str) -> str:
    """
    Generate a Kubernetes-compliant namespace name for a specific database.
    Each database gets its own dedicated namespace for better isolation.

    Kubernetes namespace naming rules:
    - Must be lowercase alphanumeric characters or hyphens
    - Must start and end with alphanumeric character
    - Maximum 63 characters
    - Cannot contain consecutive hyphens

    Args:
        domain: Domain name
        project: Project name
        database_name: Database name

    Returns:
        Valid Kubernetes namespace name (format: domain-project-database)

    Examples:
        generate_database_namespace("test-d", "test-p", "mydb") -> "test-d-test-p-mydb"
        generate_database_namespace("my-domain", "my-project", "postgres-1") -> "my-domain-my-project-postgres-1"
        generate_database_namespace("DOMAIN", "PROJECT", "DB") -> "domain-project-db"
    """
    # Normalize: lowercase, replace invalid chars with hyphens
    normalized_domain = re.sub(r'[^a-z0-9-]', '-', domain.lower())
    normalized_project = re.sub(r'[^a-z0-9-]', '-', project.lower())
    normalized_db = re.sub(r'[^a-z0-9-]', '-', database_name.lower())

    # Remove consecutive hyphens
    normalized_domain = re.sub(r'-+', '-', normalized_domain)
    normalized_project = re.sub(r'-+', '-', normalized_project)
    normalized_db = re.sub(r'-+', '-', normalized_db)

    # Remove leading/trailing hyphens
    normalized_domain = normalized_domain.strip('-')
    normalized_project = normalized_project.strip('-')
    normalized_db = normalized_db.strip('-')

    # Combine with hyphens
    namespace = f"{normalized_domain}-{normalized_project}-{normalized_db}"

    # Ensure it starts and ends with alphanumeric
    namespace = namespace.strip('-')

    # Truncate to 63 characters (Kubernetes limit)
    if len(namespace) > 63:
        # Try to keep all three parts, but truncate if needed
        # Allocate space proportionally, with minimum for each part
        total_len = len(normalized_domain) + len(normalized_project) + len(normalized_db)

        if total_len > 0:
            # Calculate proportional max lengths (leaving room for 2 hyphens)
            available = 61  # 63 - 2 hyphens
            max_domain_len = max(10, min(len(normalized_domain), int(available * len(normalized_domain) / total_len)))
            max_project_len = max(10, min(len(normalized_project), int(available * len(normalized_project) / total_len)))
            max_db_len = max(10, min(len(normalized_db), int(available * len(normalized_db) / total_len)))

            # Adjust if still too long
            while max_domain_len + max_project_len + max_db_len + 2 > 63:
                # Reduce the longest component
                if max_domain_len >= max_project_len and max_domain_len >= max_db_len:
                    max_domain_len = max(5, max_domain_len - 1)
                elif max_project_len >= max_db_len:
                    max_project_len = max(5, max_project_len - 1)
                else:
                    max_db_len = max(5, max_db_len - 1)

            namespace = f"{normalized_domain[:max_domain_len]}-{normalized_project[:max_project_len]}-{normalized_db[:max_db_len]}"
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

