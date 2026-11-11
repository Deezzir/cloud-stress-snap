# cloud-stress

A stress-testing tool for Prodstack infrastructure, distributed as a snap.
Run TRex network traffic and fio storage workloads on VMs, and view network,
storage and host measurements.

Usage is in the snap description in `snap/snapcraft.yaml`. Runs are configured
with a YAML file; [`config.yaml`](config.yaml) is the example.

## Layout

| Path | Contents |
| --- | --- |
| `src/cloud_stress.py` | CLI |
| `src/probe/` | Read-only discovery, fleet placement, readiness and `check` output |
| `src/campaign/model/` | Run configuration, schedule, state and journal |
| `src/campaign/transport/` | `juju exec` and guest SSH |
| `src/campaign/workloads/` | TRex, fio, receive counters and samplers |
| `src/campaign/lifecycle/` | Worker, preparation, measurement, finalization |
| `src/campaign/report/` | Result model, graphs, text and HTML |

- [Runs](docs/campaigns.md)
- [Configuration](docs/configuration.md)
- [Security](docs/security-reference.md)
- [Contributing](CONTRIBUTING.md)

## License

cloud-stress is released under the [BSD 3-Clause license](LICENSE).
Third-party components retain their own licenses and attribution requirements.
