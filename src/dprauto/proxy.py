"""Bounded proxy forwarding helpers for build and verification subprocesses."""

from __future__ import annotations

from dprauto.config import BuildConfig


PROXY_ENVIRONMENT_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


def docker_proxy_build_arguments(config: BuildConfig) -> tuple[str, ...]:
    """Forward proxy variables by name without recording their values."""

    if not config.allow_network or not config.forward_proxy_environment:
        return ()
    return tuple(
        argument
        for name in PROXY_ENVIRONMENT_NAMES
        for argument in ("--build-arg", name)
    )


def docker_proxy_environment_arguments(config: BuildConfig) -> tuple[str, ...]:
    """Forward host proxy variables into an ephemeral container by name."""

    if not config.allow_network or not config.forward_proxy_environment:
        return ()
    return tuple(
        argument
        for name in PROXY_ENVIRONMENT_NAMES
        for argument in ("--env", name)
    )


def cleared_proxy_environment(config: BuildConfig) -> dict[str, str]:
    """Prevent Pack's implicit host proxy forwarding when policy disables it."""

    if config.allow_network and config.forward_proxy_environment:
        return {}
    return {name: "" for name in PROXY_ENVIRONMENT_NAMES}
