#!/usr/bin/env python3
"""Sink interface and factory for the Microsoft integrations of ober watch."""

import logging
from typing import Protocol

from ober.config import MDEConfig, OberConfig, SentinelConfig
from ober.watch.models import SEVERITIES, Finding, KeySummary

logger = logging.getLogger(__name__)

MDE_ACTIONS: tuple[str, ...] = ("Block", "Audit", "Alert", "AlertAndBlock", "BlockAndRemediate")


class FindingSink(Protocol):
    """Destination for findings and key summaries.

    Implementations may block (they run on a worker thread) but must never
    raise on network failure: they log and spool instead.
    """

    def send_findings(self, findings: list[Finding]) -> None:
        """Deliver findings."""
        ...

    def send_summaries(self, summaries: list[KeySummary]) -> None:
        """Deliver per-access-key summaries."""
        ...

    def close(self) -> None:
        """Flush and release resources."""
        ...


def sentinel_problems(cfg: SentinelConfig, secrets: dict[str, str]) -> list[str]:
    """List configuration problems that prevent the Sentinel sink from working."""
    problems = []
    for name in ("tenant_id", "client_id", "dce_endpoint", "dcr_immutable_id"):
        if not getattr(cfg, name):
            problems.append(f"sentinel.{name} is empty")
    if cfg.dce_endpoint and not cfg.dce_endpoint.startswith("https://"):
        problems.append("sentinel.dce_endpoint must start with https://")
    if not cfg.findings_stream or not cfg.summary_stream:
        problems.append("sentinel.findings_stream/summary_stream is empty")
    if not secrets.get("SENTINEL_CLIENT_SECRET"):
        problems.append("SENTINEL_CLIENT_SECRET missing in ~/.ober/login")
    return problems


def mde_problems(cfg: MDEConfig, secrets: dict[str, str]) -> list[str]:
    """List configuration problems that prevent the MDE sink from working."""
    problems = []
    for name in ("tenant_id", "client_id", "api_base"):
        if not getattr(cfg, name):
            problems.append(f"mde.{name} is empty")
    if cfg.api_base and not cfg.api_base.startswith("https://"):
        problems.append("mde.api_base must start with https://")
    if cfg.action not in MDE_ACTIONS:
        problems.append(f"mde.action must be one of {', '.join(MDE_ACTIONS)}")
    if cfg.min_severity not in SEVERITIES:
        problems.append(f"mde.min_severity must be one of {', '.join(SEVERITIES)}")
    if cfg.expiry_days < 1:
        problems.append("mde.expiry_days must be >= 1")
    if not secrets.get("MDE_CLIENT_SECRET"):
        problems.append("MDE_CLIENT_SECRET missing in ~/.ober/login")
    return problems


def build_sinks(config: OberConfig, secrets: dict[str, str]) -> list[FindingSink]:
    """Build the enabled and correctly configured sinks.

    A sink that is enabled but misconfigured is skipped with a warning; this
    function never raises for configuration problems.

    Args:
        config: Loaded ober configuration.
        secrets: Contents of ``~/.ober/login`` (see ``load_secrets``).

    Returns:
        The list of ready sinks (possibly empty).
    """
    from ober.watch.mde import MDESink
    from ober.watch.sentinel import SentinelSink

    sinks: list[FindingSink] = []

    if config.sentinel.enabled:
        problems = sentinel_problems(config.sentinel, secrets)
        if problems:
            logger.warning("Sentinel sink disabled: %s", "; ".join(problems))
        else:
            sinks.append(
                SentinelSink(
                    config.sentinel,
                    secrets["SENTINEL_CLIENT_SECRET"],
                    config.watch_data_dir,
                )
            )

    if config.mde.enabled:
        problems = mde_problems(config.mde, secrets)
        if problems:
            logger.warning("MDE sink disabled: %s", "; ".join(problems))
        else:
            sinks.append(MDESink(config.mde, secrets["MDE_CLIENT_SECRET"], config.watch_data_dir))

    return sinks
