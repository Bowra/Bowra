"""Owner confirmation queue for outbound email sent by the ``send_email`` tool.

Same interaction contract as personal MCP call confirmation: the tool handler
waits inside the turn while the owner reviews the exact snapshot (recipient,
subject, full body) in the browser, then either approves or rejects it. Only an
explicit approval releases delivery; rejection, timeout and cancellation all
leave the email unsent.

Built on the shared :mod:`bowhead_core.delivery_confirmation` queue; email keeps its
own instance so the email dialog only ever sees emails.
"""

from __future__ import annotations

from bowhead_core.delivery_confirmation import CONFIRM_SECONDS, MAX_PENDING_PER_OWNER, Decision, DeliveryConfirmations

__all__ = ["CONFIRM_SECONDS", "MAX_PENDING_PER_OWNER", "Decision", "EmailSendConfirmations", "default_email_send_confirmations"]


class EmailSendConfirmations(DeliveryConfirmations):
    def __init__(self, *, timeout_seconds: float = CONFIRM_SECONDS) -> None:
        super().__init__(id_prefix="esc", timeout_seconds=timeout_seconds)

    async def request(
        self,
        *,
        owner_user_id: str,
        turn_id: str,
        session_id: str,
        to: str,
        subject: str,
        body: str,
        timeout_seconds: float | None = None,
        attachments: list[dict[str, str | int]] | None = None,
    ) -> Decision:
        """Wait for the owner's decision on this exact email snapshot."""
        return await self.request_snapshot(
            owner_user_id=owner_user_id,
            turn_id=turn_id,
            session_id=session_id,
            payload={"to": to, "subject": subject, "body": body, "attachments": [dict(item) for item in attachments or []]},
            timeout_seconds=timeout_seconds,
        )


_default = EmailSendConfirmations()


def default_email_send_confirmations() -> EmailSendConfirmations:
    return _default
