"""Entry point of the LaunchAgent (`python -m dailyread.agent_main`), kept light on purpose.

launchd starts this for two reasons. A click (a report page connecting to the localhost port) only needs the helper,
so nothing heavy is imported (no HTTP client, article extractor or pipeline): start-up is fast and small. A timer tick
imports the pipeline (`schedule`) as before. launch_activate_socket can be called once per process, so the socket is
fetched here and handed on."""
from __future__ import annotations

import sys


def _drain(sock) -> None:
    """Accept and close every waiting connection. Used when the helper cannot start: left in the queue, a waiting
    connection could make launchd start this job again and again."""
    sock.setblocking(False)
    while True:
        try:
            sock.accept()[0].close()
        except OSError:
            return


def main() -> int:
    from . import library, library_helper

    sock = library_helper.inherited_socket()
    try:
        from .config import load_config
        cfg = load_config()
        if sock is not None and library_helper.connection_pending(sock):
            token = library.read_token(library.token_path(cfg))
            if token is None:
                print("library helper: no token (run `bin/dailyread schedule install`)", file=sys.stderr)
                _drain(sock)
                return 1
            library_helper.make_server(library.library_path(cfg), token, library.helper_settings(cfg)["helper_idle_minutes"],
                                       sock=sock).run()
            return 0
        from . import schedule      # timer tick: the heavy part (pipeline) is only needed now
        return schedule.run_agent(cfg, sock)
    except Exception:
        if sock is not None:
            _drain(sock)
        raise


if __name__ == "__main__":
    sys.exit(main())
