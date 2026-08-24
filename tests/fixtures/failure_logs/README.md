# Failure log fixture provenance

The CNB excerpts are copied from these completed benchmark logs under
`/home/master/auto-build/CNB/cnb-benchmark/results`:

- `network_ursina.log`: `full-run/logs/envbench-python-paper-pokepetter-ursina-22b580f50944.build.log`
- `docker_lavague.log`: `full-run/logs/repo2run-lavague-ai-lavague-b3557f.build.attempt-3.log`
- `system_dependency_yubikey.log`: `full-run/logs/envbench-python-paper-yubico-yubikey-manager-fbdae2bc12ba.build.attempt-3.log`
- `python_version_oadoi.log`: `full-run/logs/envbench-python-paper-ourresearch-oadoi-81654794d070.build.attempt-3.log`
- `dependency_conflict_pennylane.log`: `full-run/logs/installamatic-pennylaneai-pennylane-b78565c.build.log`
- `build_tool_cpython.log`: `full-run/logs/executionagent-python-cpython-1eddef81930a.build.attempt-2.log`
- `compilation_openinterpreter.log`: `full-run/logs/installamatic-openinterpreter-open-interpreter-dbc5259.build.attempt-3.log`
- `test_tool_openlane.log`: `heragent-same-experiment/logs/envbench-python-paper-the-openroad-project-openlane-e0d2e618a883.test.1.log`

The Python dependency, test, run, and external-service files are captured from real local
commands with stable paths and volatile stack details shortened. `unknown.log` is deliberately
unrecognized to exercise the fallback boundary.
