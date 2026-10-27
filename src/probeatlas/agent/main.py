"""Agent entry point (``probeatlas-agent``): sets up logging and signal handling, then runs the loop."""

from __future__ import annotations

import signal

from probeatlas.common.logs import setup_logging

from .config import AgentConfig
from .runner import Agent


def main() -> None:
    """Run the agent until SIGTERM or SIGINT, then shut down gracefully."""
    setup_logging("agent")
    agent = Agent(AgentConfig.from_env())

    def _stop(signum, _frame):
        """Signal handler: ask the run loop to stop after in-flight checks finish."""
        agent.stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    agent.run()


if __name__ == "__main__":
    main()
