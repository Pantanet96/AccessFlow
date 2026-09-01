"""AccessFlow.

Single source of truth for the app version. Bump on every release:
PATCH = bugfix/security, MINOR = new feature, MAJOR = breaking change.
Keep in lockstep with the git tag (vX.Y.Z) and the Docker Hub image tag.
"""

__version__ = "1.2.3"
