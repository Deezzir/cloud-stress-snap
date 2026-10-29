# cloud-stress

A stress-testing tool for Prodstack infrastructure, distributed as a snap.
Run coordinated network, storage, and compute workloads to measure performance
under load, identify shared-resource bottlenecks, and evaluate isolation and QoS
before production.

Infrastructure discovery informs workload placement and reporting. Each run
reports achieved load and its effect on the tested deployment.

Usage is in the snap description in `snap/snapcraft.yaml`.

## Layout

| Path | Contents |
| --- | --- |
| `src/cloud_stress.py` | CLI |
| `src/probe/` | Read-only discovery for `check` |
| `src/helpers.py` | Readiness and run plan |
| `src/campaign/model/` | Run state, journal, profiles, target |
| `src/campaign/transport/` | `juju exec` and guest SSH |
| `src/campaign/workloads/` | TRex, fio and samplers |
| `src/campaign/lifecycle/` | Worker, preparation, measurement, finalization |
| `src/campaign/report/` | Result model, text and HTML |

- [Campaigns](docs/campaigns.md)
- [Security](docs/security-reference.md)
- [Contributing](CONTRIBUTING.md)

## License

cloud-stress is released under the [BSD 3-Clause license](LICENSE).
Third-party components retain their own licenses and attribution requirements.
