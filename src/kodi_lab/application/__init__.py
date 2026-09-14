"""Application services and their stable error contract."""

from kodi_lab.application.errors import (
    ApplicationServiceError,
    EntityNotFoundError,
    KodiVersionIncompatibleError,
    ManagedInstanceConflictError,
    ManagedInstanceNotFoundError,
    NoApprovedRevisionError,
    PersistenceConflictError,
    RevisionNotApprovedError,
    RuntimeDefinitionNotFoundError,
    RuntimeRevisionDefinitionMismatchError,
    RuntimeRevisionNotFoundError,
    RuntimeSelectionError,
    TargetNotFoundError,
    TrustedCustomBuildConfirmationRequiredError,
    TrustedCustomBuildDisabledError,
)
from kodi_lab.application.managed_instances import (
    ExactRevisionSelection,
    ManagedInstanceService,
    NewestApprovedRevisionSelection,
)

__all__ = [
    "ApplicationServiceError",
    "EntityNotFoundError",
    "ExactRevisionSelection",
    "KodiVersionIncompatibleError",
    "ManagedInstanceConflictError",
    "ManagedInstanceNotFoundError",
    "ManagedInstanceService",
    "NewestApprovedRevisionSelection",
    "NoApprovedRevisionError",
    "PersistenceConflictError",
    "RevisionNotApprovedError",
    "RuntimeDefinitionNotFoundError",
    "RuntimeRevisionDefinitionMismatchError",
    "RuntimeRevisionNotFoundError",
    "RuntimeSelectionError",
    "TargetNotFoundError",
    "TrustedCustomBuildConfirmationRequiredError",
    "TrustedCustomBuildDisabledError",
]
