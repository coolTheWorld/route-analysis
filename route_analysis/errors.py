"""User-safe application error types."""


class RouteAnalysisError(Exception):
    """Base class for recoverable application errors."""


class DataContractError(RouteAnalysisError):
    """The backend or imported file did not satisfy the expected contract."""


class ApiError(RouteAnalysisError):
    """A scheduler API call failed."""


class AuthenticationError(ApiError):
    """Login failed or a renewed session was rejected."""


class StorageError(RouteAnalysisError):
    """Local configuration or lane data could not be read or written."""


class ImportMismatchError(StorageError):
    """Imported lane metadata does not match the active server/map key."""
