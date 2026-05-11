"""Route handler mixins for the remote HTTP control plane."""

from labrastro_server.interfaces.http.remote.routes.admin import RemoteAdminRoutes
from labrastro_server.interfaces.http.remote.routes.artifacts import RemoteArtifactRoutes
from labrastro_server.interfaces.http.remote.routes.auth import RemoteAuthRoutes
from labrastro_server.interfaces.http.remote.routes.base import RemoteRelayBaseHandler
from labrastro_server.interfaces.http.remote.routes.chat import RemoteChatRoutes
from labrastro_server.interfaces.http.remote.routes.collaboration import RemoteCollaborationRoutes
from labrastro_server.interfaces.http.remote.routes.manifests import RemoteManifestRoutes
from labrastro_server.interfaces.http.remote.routes.peer import RemotePeerRoutes
from labrastro_server.interfaces.http.remote.routes.agent_runs import RemoteAgentRunRoutes
from labrastro_server.interfaces.http.remote.routes.sessions import RemoteSessionRoutes
from labrastro_server.interfaces.http.remote.routes.taskflow import RemoteTaskflowRoutes

__all__ = [
    "RemoteAdminRoutes",
    "RemoteArtifactRoutes",
    "RemoteAuthRoutes",
    "RemoteChatRoutes",
    "RemoteCollaborationRoutes",
    "RemoteManifestRoutes",
    "RemotePeerRoutes",
    "RemoteRelayBaseHandler",
    "RemoteAgentRunRoutes",
    "RemoteSessionRoutes",
    "RemoteTaskflowRoutes",
]
