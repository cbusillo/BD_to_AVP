"""The source descriptor the Mac app authorized, received once over a Unix socket.

The Mac app opens the ISO under the approved folder's security scope, so it holds the
user's consent and the open happens in the app. A child process does not inherit that
consent, so the worker never opens the source by path: it connects to the one-shot
socket named in ``source.descriptor_handoff``, sends the token, and receives the open
file with ``SCM_RIGHTS``. Every read then goes through that descriptor, which
``ssif_probe`` opens as ``/dev/fd/N``.
"""

from __future__ import annotations

import fcntl
import os
import socket
import stat
import time

from types import TracebackType

from bd_to_avp.worker.protocol import DescriptorHandoff, JobSource, LiveSourceIdentity
from bd_to_avp.worker.source_errors import SourceServiceError


HANDOFF_DEADLINE_SECONDS = 10.0
HANDOFF_FAILED = "live_source_handoff_failed"
SOURCE_CHANGED = "live_source_changed"
SOURCE_CHANGED_MESSAGE = "The live source is no longer the file that was approved."


def identity_matches(info: os.stat_result, identity: LiveSourceIdentity) -> bool:
    """Whether ``info`` describes the file the host authorized."""
    observed = (info.st_dev & 0xFFFF_FFFF, info.st_ino, info.st_size, info.st_mtime_ns)
    expected = (
        identity.device,
        identity.inode,
        identity.size_bytes,
        identity.modification_seconds * 1_000_000_000 + identity.modification_nanoseconds,
    )
    return stat.S_ISREG(info.st_mode) and observed == expected


class HeldSource:
    """Owns the received source descriptor for one job and closes it exactly once."""

    def __init__(self, descriptor: int, identity: LiveSourceIdentity) -> None:
        self._descriptor: int | None = descriptor
        self.identity = identity

    @property
    def descriptor(self) -> int:
        if self._descriptor is None:
            raise SourceServiceError(SOURCE_CHANGED, "The live source descriptor was already released.")
        return self._descriptor

    @property
    def device_path(self) -> str:
        """The path ``ssif_probe`` opens; it duplicates the inherited descriptor."""
        return f"/dev/fd/{self.descriptor}"

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return (self.descriptor,)

    def verify(self) -> None:
        """Confirms the held descriptor still describes the authorized file."""
        try:
            info = os.fstat(self.descriptor)
        except OSError as error:
            raise SourceServiceError(SOURCE_CHANGED, SOURCE_CHANGED_MESSAGE, str(error)) from error
        if not identity_matches(info, self.identity):
            raise SourceServiceError(SOURCE_CHANGED, SOURCE_CHANGED_MESSAGE)

    def close(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            os.close(descriptor)

    def __enter__(self) -> HeldSource:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def receive_source(source: JobSource, *, deadline_seconds: float = HANDOFF_DEADLINE_SECONDS) -> HeldSource | None:
    """Receives and checks the source descriptor, or returns None when the job has no handoff.

    The returned owner must be closed; it is a context manager. A descriptor that fails the
    identity check is closed before this raises.
    """
    handoff = source.descriptor_handoff
    if handoff is None:
        return None
    if source.identity is None:
        raise SourceServiceError(HANDOFF_FAILED, "The live source handoff has no approved identity.")
    held = HeldSource(_receive_descriptor(handoff, deadline_seconds), source.identity)
    try:
        held.verify()
    except BaseException:
        held.close()
        raise
    return held


def _receive_descriptor(handoff: DescriptorHandoff, deadline_seconds: float) -> int:
    deadline = time.monotonic() + deadline_seconds

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("The source handoff deadline passed.")
        return left

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(remaining())
            connection.connect(os.fspath(handoff.socket_path))
            connection.settimeout(remaining())
            connection.sendall(handoff.token.encode("ascii"))
            connection.settimeout(remaining())
            data, descriptors, _, _ = socket.recv_fds(connection, 1, 1)
    except (OSError, TimeoutError) as error:
        raise SourceServiceError(
            HANDOFF_FAILED,
            "The Mac app could not hand the approved source to the worker.",
            str(error),
        ) from error
    if len(data) != 1 or len(descriptors) != 1:
        for descriptor in descriptors:
            os.close(descriptor)
        raise SourceServiceError(HANDOFF_FAILED, "The Mac app did not hand over the approved source.")
    descriptor = descriptors[0]
    try:
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFD)
        fcntl.fcntl(descriptor, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
    except OSError as error:
        os.close(descriptor)
        raise SourceServiceError(HANDOFF_FAILED, "The handed-over source could not be held.", str(error)) from error
    return descriptor
