"""Tenant lifecycle state machine. Every status change goes through transition()."""
from ..models import Tenant, now

TRANSITIONS: dict[str, set[str]] = {
    "pending":             {"provisioning", "cancelled"},
    "provisioning":        {"active", "provisioning_failed"},
    "provisioning_failed": {"provisioning", "cancelled"},
    "active":              {"suspended", "cancelling"},
    "suspended":           {"active", "cancelling"},
    "cancelling":          {"cancelled"},
    "cancelled":           {"terminated"},
    "terminated":          set(),
}


class InvalidTransition(Exception):
    def __init__(self, frm: str, to: str):
        super().__init__(f"cannot move tenant from '{frm}' to '{to}'")
        self.frm, self.to = frm, to


def transition(t: Tenant, to: str) -> None:
    if to not in TRANSITIONS.get(t.status, set()):
        raise InvalidTransition(t.status, to)
    t.status = to
    if to == "terminated":
        t.terminated_at = now()
    if to == "active":
        t.suspend_mode, t.suspension_reason, t.suspended_at = "none", None, None


def chatwoot_locked(t: Tenant) -> bool:
    """Whether the Chatwoot account should be suspended, given the tenant's current state."""
    return t.status in ("cancelling", "cancelled", "terminated") or (t.status == "suspended" and t.suspend_mode == "lock")


def suspend(t: Tenant, mode: str, reason: str | None) -> None:
    if mode not in ("pause_outbound", "lock"):
        raise ValueError("bad suspend mode")
    if t.status == "suspended":          # change of mode only
        t.suspend_mode, t.suspension_reason = mode, reason or t.suspension_reason
        return
    transition(t, "suspended")
    t.suspend_mode, t.suspension_reason, t.suspended_at = mode, reason, now()


def unsuspend(t: Tenant) -> None:
    if t.status != "suspended":
        raise InvalidTransition(t.status, "active")
    transition(t, "active")


def cancel(t: Tenant) -> bool:
    """Returns True when a Chatwoot lock must complete before the tenant reaches 'cancelled'."""
    if t.status in ("pending", "provisioning_failed") and not t.chatwoot_account_id:
        transition(t, "cancelled")
        return False
    if t.status in ("pending", "provisioning_failed"):
        transition(t, "cancelled")
        return True      # account exists in Chatwoot; still lock it
    transition(t, "cancelling")
    return True


def terminate(t: Tenant) -> None:
    transition(t, "terminated")
