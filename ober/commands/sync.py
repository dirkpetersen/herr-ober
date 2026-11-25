#!/usr/bin/env python3
"""Ober sync command - deprecated.

It wrote routers.lst, frontend-http.lst and backend-http.lst, but no generated config ever
read them, and its reload did nothing. The lists it meant to manage are fields of ober.yaml.
The name is kept free for a later cluster-wide config sync.
"""

import click

MESSAGE = """ober sync is deprecated and changes nothing.
The files it wrote were never read by HAProxy. Set these fields in ober.yaml instead,
then run 'sudo ober config --non-interactive':
  --routers        bgp.neighbors (BGP mode) or keepalived.peers (keepalived mode)
  --frontend-http  security.internal_networks (internal VIPs) or
                   security.allowlist (internet VIPs)
  --backend-http   backends[].servers"""


@click.command(hidden=True, context_settings={"ignore_unknown_options": True})
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def sync(ctx: click.Context, args: tuple[str, ...]) -> None:  # noqa: ARG001
    """Deprecated: does nothing. The lists live in ober.yaml."""
    click.echo(MESSAGE, err=True)
    ctx.exit(1)
