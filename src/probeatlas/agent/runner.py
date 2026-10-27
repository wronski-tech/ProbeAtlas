"""Agent main loop: enroll, heartbeat, claim jobs, execute, submit results."""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from probeatlas import __version__
from probeatlas.common.logs import log_fields
from probeatlas.common.netpolicy import NetworkPolicy
from probeatlas.common.schemas import AgentHeartbeat, AgentRegistration, JobSpec, MeasurementResult

from .checks import detect_capabilities, execute
from .checks.base import CheckContext
from .client import AuthError, ControlPlaneClient, TransientError
from .config import AgentConfig

log = logging.getLogger("probeatlas.agent")

MAX_BUFFERED_RESULTS = 2000


class Backoff:
    def __init__(self, base: float = 1.0, cap: float = 30.0):
        self.base, self.cap, self.attempt = base, cap, 0

    def next_delay(self) -> float:
        delay = min(self.cap, self.base * 2**self.attempt)
        self.attempt += 1
        return delay * (0.5 + random.random() / 2)

    def reset(self) -> None:
        self.attempt = 0


class Agent:
    def __init__(self, config: AgentConfig, client: ControlPlaneClient | None = None):
        self.config = config
        self.client = client or ControlPlaneClient(config.api_url)
        self.stop_event = threading.Event()
        self.capabilities = detect_capabilities()
        policy = NetworkPolicy.from_cidrs(
            config.allowed_private_cidrs, allow_private_hostnames=bool(config.allowed_private_cidrs)
        )
        self.ctx = CheckContext(
            policy=policy,
            capabilities=self.capabilities,
            user_agent=f"ProbeAtlas-Agent/{__version__} (+https://probeatlas.dev/agent)",
            ca_file=config.ca_file,
        )
        self.pool = ThreadPoolExecutor(max_workers=config.concurrency, thread_name_prefix="check")
        self.inflight: dict[str, Future[MeasurementResult]] = {}
        self.outbox: deque[MeasurementResult] = deque(maxlen=MAX_BUFFERED_RESULTS)
        self.started = time.monotonic()
        self._backoff = Backoff()

    # -- credentials -----------------------------------------------------

    def _load_state(self) -> dict | None:
        path = Path(self.config.state_file)
        if path.exists():
            return json.loads(path.read_text())
        return None

    def _save_state(self, agent_id: str, token: str) -> None:
        path = Path(self.config.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"agent_id": agent_id, "agent_token": token, "api_url": self.config.api_url}, fh)
        tmp.replace(path)

    def ensure_registered(self) -> None:
        state = self._load_state()
        if state and state.get("api_url") == self.config.api_url:
            self.client.set_credentials(state["agent_id"], state["agent_token"])
            return
        if not self.config.enrollment_token:
            raise SystemExit("agent is not enrolled and PROBEATLAS_AGENT_ENROLLMENT_TOKEN is not set")
        reg = AgentRegistration(
            name=self.config.name,
            region=self.config.region,
            country_code=self.config.country_code,
            city=self.config.city,
            provider=self.config.provider,
            version=__version__,
            capabilities=self.capabilities,
        )
        while not self.stop_event.is_set():
            try:
                agent_id, token = self.client.register(self.config.enrollment_token, reg)
                break
            except TransientError as exc:
                self._sleep_backoff("registration failed", exc)
        else:
            return
        self._save_state(agent_id, token)
        self.client.set_credentials(agent_id, token)
        self._backoff.reset()
        log.info("agent enrolled", extra=log_fields(agent_id=agent_id, region=self.config.region))

    def _reenroll(self) -> None:
        log.warning("agent credentials rejected; re-enrolling", extra=log_fields(agent_id=self.client.agent_id))
        Path(self.config.state_file).unlink(missing_ok=True)
        self.ensure_registered()

    # -- loops -----------------------------------------------------------

    def _sleep_backoff(self, what: str, exc: Exception) -> None:
        delay = self._backoff.next_delay()
        log.warning(f"{what}; retrying", extra=log_fields(error=str(exc), retry_in_s=round(delay, 2)))
        self.stop_event.wait(delay)

    def heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.client.heartbeat(
                    AgentHeartbeat(
                        version=__version__,
                        capabilities=self.capabilities,
                        running_jobs=len(self.inflight),
                        uptime_s=round(time.monotonic() - self.started, 1),
                    )
                )
            except (TransientError, AuthError, ValueError) as exc:
                log.warning("heartbeat failed", extra=log_fields(error=str(exc)))
            self.stop_event.wait(self.config.heartbeat_interval_s)

    def _collect(self) -> None:
        for job_id, fut in list(self.inflight.items()):
            if fut.done():
                del self.inflight[job_id]
                result = fut.result()
                self.outbox.append(result)
                log.info(
                    "check finished",
                    extra=log_fields(
                        job_id=job_id,
                        check_type=result.check_type.value,
                        success=result.success,
                        failure_category=result.failure_category,
                        latency_ms=result.latency_ms,
                    ),
                )

    def _flush(self) -> bool:
        """Submit buffered results. Returns False if the control plane is unreachable."""
        while self.outbox:
            batch = [self.outbox[i] for i in range(min(len(self.outbox), 100))]
            try:
                resp = self.client.submit(batch)
            except TransientError as exc:
                log.warning("result submission failed", extra=log_fields(error=str(exc), buffered=len(self.outbox)))
                return False
            for _ in batch:
                self.outbox.popleft()
            for ack in resp.acks:
                if ack.status == "rejected":
                    log.warning("result rejected", extra=log_fields(job_id=str(ack.job_id), reason=ack.reason))
        return True

    def step(self) -> int:
        """One iteration of the main loop. Returns the number of jobs claimed."""
        self._collect()
        if not self._flush():
            raise TransientError("result submission failed")
        free = self.config.concurrency - len(self.inflight)
        if free <= 0:
            return 0
        jobs: list[JobSpec] = self.client.claim(min(free, 50))
        for job in jobs:
            self.inflight[str(job.job_id)] = self.pool.submit(execute, job, self.ctx)
        return len(jobs)

    def run(self) -> None:
        self.ensure_registered()
        if self.stop_event.is_set():
            return
        log.info(
            "agent started",
            extra=log_fields(
                agent_id=self.client.agent_id,
                version=__version__,
                region=self.config.region,
                capabilities=[c.value for c in self.capabilities],
                private_cidrs=self.config.allowed_private_cidrs or None,
            ),
        )
        hb = threading.Thread(target=self.heartbeat_loop, name="heartbeat", daemon=True)
        hb.start()
        while not self.stop_event.is_set():
            try:
                claimed = self.step()
                self._backoff.reset()
                self.stop_event.wait(0.2 if claimed else self.config.poll_interval_s)
            except TransientError as exc:
                self._sleep_backoff("control plane unavailable", exc)
            except AuthError:
                self._reenroll()
            except ValueError as exc:
                self._sleep_backoff("request rejected", exc)
        self.shutdown()

    def shutdown(self, grace_s: float = 30.0) -> None:
        """Finish in-flight checks (bounded by their timeouts) and flush results."""
        log.info("agent shutting down", extra=log_fields(inflight=len(self.inflight)))
        self.pool.shutdown(wait=True, cancel_futures=False)
        self._collect()
        deadline = time.monotonic() + grace_s
        while self.outbox and time.monotonic() < deadline:
            try:
                if self._flush():
                    break
            except AuthError:
                break
            time.sleep(1)
        if self.outbox:
            log.warning("unsubmitted results dropped at shutdown", extra=log_fields(count=len(self.outbox)))
        self.client.close()
