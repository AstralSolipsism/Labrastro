from __future__ import annotations

from http import HTTPStatus

from labrastro_server.interfaces.http.remote.protocol import (
    LocalActionCancelRequest,
    LocalActionCancelResponse,
    LocalActionClaimRequest,
    LocalActionCompleteRequest,
    LocalActionCompleteResponse,
    LocalActionProgressRequest,
    LocalActionProgressResponse,
)
from labrastro_server.services.agent_runtime.local_actions import (
    LocalActionLeaseError,
    LocalActionNotFoundError,
    LocalActionTerminalError,
)


class RemoteLocalActionRoutes:
    def _handle_local_action_claim(self) -> None:
        try:
            req = LocalActionClaimRequest.from_dict(self._read_json())
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_local_action_claim_request")
            return
        peer_id = self._verify_peer_token(req.peer_token)
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return
        if req.peer_id != peer_id:
            self._send_error(HTTPStatus.FORBIDDEN, "peer_identity_mismatch")
            return
        service = self._local_action_service()
        if service is None:
            return
        try:
            claim = service.claim_local_actions(
                peer_id=peer_id,
                worker_kind=req.worker_kind,
                features=req.features,
                workspace_root=req.workspace_root,
                max_actions=req.max_actions,
            )
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_local_action_claim")
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        self._send_json(HTTPStatus.OK, claim.to_dict())

    def _handle_local_action_progress(self) -> None:
        try:
            req = LocalActionProgressRequest.from_dict(self._read_json())
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_local_action_progress_request")
            return
        peer_id = self._verify_local_action_peer(req.peer_token)
        if peer_id is None:
            return
        if not req.local_action_id or not req.lease_id:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "local_action_id_and_lease_id_required",
            )
            return
        service = self._local_action_service()
        if service is None:
            return
        try:
            action = service.progress_local_action(
                local_action_id=req.local_action_id,
                peer_id=peer_id,
                lease_id=req.lease_id,
                progress=req.progress,
                status=req.status,
            )
        except LocalActionNotFoundError:
            self._send_error(HTTPStatus.NOT_FOUND, "local_action_not_found")
            return
        except (LocalActionLeaseError, LocalActionTerminalError):
            self._send_error(HTTPStatus.CONFLICT, "local_action_lease_invalid")
            return
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_local_action_progress")
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        self._send_json(
            HTTPStatus.OK,
            LocalActionProgressResponse(ok=True, action=action).to_dict(),
        )

    def _handle_local_action_complete(self) -> None:
        try:
            req = LocalActionCompleteRequest.from_dict(self._read_json())
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_local_action_complete_request")
            return
        peer_id = self._verify_local_action_peer(req.peer_token)
        if peer_id is None:
            return
        if not req.local_action_id or not req.lease_id:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "local_action_id_and_lease_id_required",
            )
            return
        service = self._local_action_service()
        if service is None:
            return
        try:
            action = service.complete_local_action(
                local_action_id=req.local_action_id,
                peer_id=peer_id,
                lease_id=req.lease_id,
                status=req.status,
                result=req.result,
                error=req.error,
            )
        except LocalActionNotFoundError:
            self._send_error(HTTPStatus.NOT_FOUND, "local_action_not_found")
            return
        except (LocalActionLeaseError, LocalActionTerminalError):
            self._send_error(HTTPStatus.CONFLICT, "local_action_lease_invalid")
            return
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_local_action_complete")
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        self._send_json(
            HTTPStatus.OK,
            LocalActionCompleteResponse(ok=True, action=action).to_dict(),
        )

    def _handle_local_action_cancel(self) -> None:
        try:
            req = LocalActionCancelRequest.from_dict(self._read_json())
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_local_action_cancel_request")
            return
        peer_id = self._verify_local_action_peer(req.peer_token)
        if peer_id is None:
            return
        if not req.local_action_id or not req.lease_id:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "local_action_id_and_lease_id_required",
            )
            return
        service = self._local_action_service()
        if service is None:
            return
        try:
            action = service.complete_local_action(
                local_action_id=req.local_action_id,
                peer_id=peer_id,
                lease_id=req.lease_id,
                status="cancelled",
                result={"reason": req.reason},
                error=req.reason,
            )
        except LocalActionNotFoundError:
            self._send_error(HTTPStatus.NOT_FOUND, "local_action_not_found")
            return
        except (LocalActionLeaseError, LocalActionTerminalError):
            self._send_error(HTTPStatus.CONFLICT, "local_action_lease_invalid")
            return
        except Exception:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_local_action_cancel")
            return
        self.service.relay_server.registry.update_heartbeat(peer_id)
        self._send_json(
            HTTPStatus.OK,
            LocalActionCancelResponse(ok=True, action=action).to_dict(),
        )

    def _verify_local_action_peer(self, peer_token: str) -> str | None:
        if not peer_token:
            self._send_error(HTTPStatus.BAD_REQUEST, "peer_token_required")
            return None
        peer_id = self._verify_peer_token(peer_token)
        if peer_id is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "invalid_peer_token")
            return None
        return peer_id

    def _local_action_service(self):
        service = getattr(self.service, "local_action_service", None)
        if service is None:
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "local_actions_unavailable")
            return None
        return service
