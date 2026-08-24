"""Small shared helpers for verification policies."""

import uuid

from dprauto.domain.enums import VerificationLevel, VerificationStatus
from dprauto.domain.models import VerificationCheck


def verification_id(level: VerificationLevel) -> str:
    return f"{level.value}-{uuid.uuid4().hex}"


def aggregate_status(checks: tuple[VerificationCheck, ...]) -> VerificationStatus:
    statuses = {check.status for check in checks}
    if VerificationStatus.ERROR in statuses:
        return VerificationStatus.ERROR
    if VerificationStatus.FAILED in statuses:
        return VerificationStatus.FAILED
    if statuses == {VerificationStatus.SKIPPED} or not statuses:
        return VerificationStatus.SKIPPED
    return VerificationStatus.PASSED
