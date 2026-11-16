"""Hearing that the process was told to stop, at the moment it is told.

A web server that is told to stop, by Docker's SIGTERM or by Ctrl-C, first lets the
requests it is answering come to an end, and tells the application only after that. An
application with work of its own to end needs to hear it sooner: the readers of the
answers being written are among those requests, and should still be connected when
their answer ends.

`on_stop_signal` puts a listener in front of whoever handles those signals now. They
are called right after, so whatever they did, they still do.
"""

import signal
import threading
from collections.abc import Callable
from types import FrameType

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def on_stop_signal(heard: Callable[[], None]) -> Callable[[], None]:
    """Call `heard` when the process is told to stop, then whoever handled the signal
    before. Returns what takes the listener out again.

    `heard` runs inside a signal handler: between any two steps of whatever the process
    was doing. It may do no more than leave a note, such as
    `loop.call_soon_threadsafe(...)`.

    Nothing is listened for where nothing can be: outside the main thread, which is
    the only one that gets signals, and for a signal nobody handles, whose default
    ends the process at once.
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    listening: dict[int, tuple[object, object]] = {}
    for stop in STOP_SIGNALS:
        before = signal.getsignal(stop)
        if not callable(before):
            continue

        def listener(
            received: int,
            frame: FrameType | None,
            before: Callable[[int, FrameType | None], object] = before,
        ) -> None:
            heard()
            before(received, frame)

        signal.signal(stop, listener)
        listening[stop] = (listener, before)

    def stop_listening() -> None:
        for stop, (listener, before) in listening.items():
            # Whoever was handling the signal may have taken it back already.
            if signal.getsignal(stop) is listener:
                signal.signal(stop, before)  # type: ignore[arg-type]

    return stop_listening
