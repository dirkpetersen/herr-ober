# Operations

This page is for a running cluster: change the configuration, take a node or an RGW server out
of rotation, push one `ober.yaml` to all nodes, renew certificates, monitor and upgrade. The
options of each command are in the [CLI reference](cli.md).

## Change the configuration: apply and rollback

Edit `ober.yaml` (or run `sudo ober config`), then apply it on the running node:

```bash
sudo ober apply              # diff, check, reload, health gate
sudo ober rollback --list    # the stored states, newest last
sudo ober rollback           # go back to the state before the last apply
```

`ober apply` renders `haproxy.cfg` and the ExaBGP or keepalived configuration from `ober.yaml`,
shows the diff, runs `haproxy -c` (and `keepalived -t`), reloads, and waits for 3 good
`/health` checks in a row (`--timeout`, default 30 s). If a step fails, it writes the old files
back and reloads again; `ober.yaml` keeps your edit. `ober apply -f FILE` (or `-f -` for stdin)
installs a new `ober.yaml` first, and a failure restores the old one too.

Each successful apply stores the applied files of `<install_path>/etc` (not the certificates
and not the blocklist) in `<install_path>/var/history/<timestamp>`. The 20 newest are kept.
`ober rollback [SNAPSHOT]` restores one of them with the same checks and health gate. The
`lo-vip` network files are not covered. In BGP mode, both commands send SIGHUP to the health
process, so it reads the new VIP list.

## Take a node out of rotation

```bash
sudo ober maintenance on       # BGP: withdraw all routes. keepalived: priority -100, VIPs move
sudo ober maintenance status   # what this node still announces or owns
sudo ober maintenance off
```

HAProxy keeps serving during maintenance, so open connections finish. `ober stop` does the same
first: it turns maintenance on, waits until the node is out of rotation, waits up to
`drain_timeout` seconds (default 30) for open connections to close, and then stops HAProxy.

## Take an RGW server out of rotation

```bash
sudo ober backend list                     # state, check result and sessions per server
sudo ober backend drain rgw07:7480         # no new requests, running ones finish
sudo ober backend maint rgw07:7480         # out at once: no checks, no new connections
sudo ober backend ready rgw07:7480         # back in service
sudo ober backend drain rgw07:7480 --all-nodes  # the same on every node in `nodes:`
```

A server is named by `host:port` or by its HAProxy name (`rgw07:7480` is `rgw07_7480`). The
state is saved to the HAProxy server-state file after each change, so a drain survives a
reload. `--all-nodes` runs the command on each other node over SSH (as `ober push` does),
tries all nodes and exits 1 if one of them failed.

## Cluster, metrics, certificates and upgrades

<!-- The rest of this page is the "Usage" part of README.md between the
     "operations" markers (pymdownx.snippets). Edit README.md. -->

--8<-- "README.md:operations"
