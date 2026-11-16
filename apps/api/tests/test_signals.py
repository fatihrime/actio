"""Hearing that the process is told to stop: a listener in front of whoever handles the
signal, who still handles it afterwards.

The signals here are real ones, sent to the test process itself. Each test first puts
a handler of its own in place, so that none of them stops the test run.
"""

import os
import signal
import threading
from collections.abc import Iterator
from types import FrameType

import pytest

from actio.core.signals import on_stop_signal


@pytest.fixture
def handled() -> Iterator[list[str]]:
    """What happened to SIGTERM, in order. While a test runs, a handler notes each one
    under `server`: it stands for the web server's own."""
    heard: list[str] = []

    def server(received: int, frame: FrameType | None) -> None:
        assert received == signal.SIGTERM
        assert frame is not None
        heard.append("server")

    before = signal.signal(signal.SIGTERM, server)
    yield heard
    signal.signal(signal.SIGTERM, before)


def told_to_stop() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


def test_the_listener_hears_first_and_the_handler_still_handles(handled: list[str]) -> None:
    stop_listening = on_stop_signal(lambda: handled.append("listener"))

    told_to_stop()
    told_to_stop()
    stop_listening()

    assert handled == ["listener", "server", "listener", "server"]


def test_after_listening_the_signal_is_handled_as_before(handled: list[str]) -> None:
    server = signal.getsignal(signal.SIGTERM)
    stop_listening = on_stop_signal(lambda: handled.append("listener"))
    listening = signal.getsignal(signal.SIGTERM)

    stop_listening()
    told_to_stop()

    assert listening is not server
    assert signal.getsignal(signal.SIGTERM) is server
    assert handled == ["server"]


def test_a_handler_that_was_put_back_meanwhile_is_left_alone(handled: list[str]) -> None:
    """The web server restores the handlers it found when it stops serving. Taking the
    listener out after that must not bring the server's own back."""
    server = signal.getsignal(signal.SIGTERM)
    stop_listening = on_stop_signal(lambda: handled.append("listener"))

    def someone_else(received: int, frame: FrameType | None) -> None:
        handled.append(f"someone else, {received}, {frame is not None}")

    signal.signal(signal.SIGTERM, someone_else)
    stop_listening()

    assert signal.getsignal(signal.SIGTERM) is someone_else
    assert server is not someone_else


def test_a_signal_that_nobody_handles_is_not_listened_for() -> None:
    """Its default ends the process at once: there would be nothing to hand it on to,
    and nobody left to act on what was heard."""
    before = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        stop_listening = on_stop_signal(lambda: pytest.fail("nothing should be heard"))
        listening = signal.getsignal(signal.SIGTERM)
        stop_listening()
    finally:
        signal.signal(signal.SIGTERM, before)

    assert listening is signal.SIG_DFL


def test_outside_the_main_thread_nothing_is_listened_for(handled: list[str]) -> None:
    """Only the main thread gets signals, and only it may say who handles them."""
    server = signal.getsignal(signal.SIGTERM)
    done: list[bool] = []

    def elsewhere() -> None:
        stop_listening = on_stop_signal(lambda: handled.append("listener"))
        done.append(signal.getsignal(signal.SIGTERM) is server)
        stop_listening()

    thread = threading.Thread(target=elsewhere)
    thread.start()
    thread.join()
    told_to_stop()

    assert done == [True]
    assert handled == ["server"]
