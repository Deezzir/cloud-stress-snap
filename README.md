# cloud-stress

A stress-testing tool for Prodstack infrastructure, distributed as a snap.
Run coordinated network, storage, and compute workloads to measure performance
under load, identify shared-resource bottlenecks, and evaluate isolation and QoS
before production.

Install the snap on a bastion or infra-management node inside Prodstack. Connect
once, then select a stress profile and measurement duration:

```sh
cloud-stress connect
cloud-stress run --profile cross-plane --duration 8h
```

Campaigns combine workload preparation, load calibration, phased execution,
metrics collection, reporting, and cleanup. Infrastructure topology informs
target selection and interpretation of results. Compare hardware and
configuration choices using achieved load, workload impact, and retained
evidence rather than nominal capacity alone.

## Development

```sh
3tox -e format
tox -e lint
tox -e unit
tox -e func
tox -e typecheck
```

- [Security](docs/security-reference.md)
- [Contributing](CONTRIBUTING.md)

## License

cloud-stress is released under the [BSD 3-Clause license](LICENSE).
Third-party components retain their own licenses and attribution requirements.
