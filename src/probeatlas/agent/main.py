from __future__ import annotations

import signal

from probeatlas.common.logs import setup_logging

from .config import AgentConfig
from .runner import Agent


def main() -> None:
    setup_logging("agent")
    agent = Agent(AgentConfig.from_env())

    def _stop(signum, _frame):
        agent.stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    agent.run()


if __name__ == "__main__":
    main()
