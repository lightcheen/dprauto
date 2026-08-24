# DPRAuto Handoff Summary

Last updated: 2026-08-13

## 1. Project status

This repository has already moved well past architecture-only work. The core pipeline exists and has been exercised on real projects.

Current implemented flow:

`Project Parse -> Standard Build -> Failure Classification -> Agent Repair -> Rebuild -> Verification -> Regression Check -> Environment Diff -> Result`

The implementation is Python-first, with adapters and interfaces separated so other languages can be added later.

Important environment constraint already handled:

- Do not create or use `/home/master/.git`
- Use `/home/master/dprauto/.git` only

## 2. What has been built

### Core architecture and models

Already implemented:

- domain data models such as `ProjectProfile`, `BuildPlan`, `BuildResult`, `FailureInfo`, `VerificationResult`, `EnvironmentDiff`
- agent state and persistence model
- centralized config and error types
- separated ports/adapters
- tests stored independently under `tests/`

Main code areas:

- `/home/master/dprauto/src/dprauto/domain`
- `/home/master/dprauto/src/dprauto/ports`
- `/home/master/dprauto/src/dprauto/adapters`
- `/home/master/dprauto/src/dprauto/agent`
- `/home/master/dprauto/src/dprauto/application`

### Project parsing

Implemented Python project parser with rules-first extraction for:

- Python version
- dependency files
- package manager
- Dockerfile
- README
- CI files
- install/test/run commands
- project type inference

### Deterministic build

Implemented build strategy framework with current practical focus on runnable strategies.

Available strategy shape includes:

- `DockerStrategy`
- `CNBStrategy`
- `TemplateStrategy`

Actual evaluation behavior currently relies heavily on deterministic Docker/template build paths.

### Failure classification

Rule-based classification exists for:

- network
- docker
- system dependency
- Python version mismatch
- Python dependency missing/conflict
- build tool error
- compile/test/run failure
- external service
- unknown

### Agent workflow

LangGraph-based orchestration exists.

Workflow shape is already split into nodes, not a single loop:

- analyze failure
- plan fix
- apply fix
- execute
- verify
- evaluate

It already supports:

- max attempts
- repeated-failure tracking
- diff persistence
- policy against editing business source by default

### State, checkpoint, and context control

Implemented:

- persistent checkpointing
- repair history
- context summary
- external storage for full artifacts
- resume support

### Verification and regression

Implemented verification layers:

- Installability
- Testability
- Runnability

Implemented regression checker and environment diff reporting.

## 3. Important fixes already made in this conversation

These are the most important recent code changes because they directly affected real-project evaluation quality.

### 3.1 LLM timeout and failover behavior

Problem before:

- some projects stopped after a single model timeout
- timeout handling was too weak for long-running repair loops

What was added:

- `LLMTimeoutError`
- model pool parsing from `/home/master/dprauto/myapi.json`
- failover client: retry same model once, then switch to next configured model
- output token cap wiring

Relevant files:

- `/home/master/dprauto/src/dprauto/errors.py`
- `/home/master/dprauto/src/dprauto/adapters/llm/api.py`
- `/home/master/dprauto/src/dprauto/application/agent.py`
- `/home/master/dprauto/src/dprauto/config.py`

Configured model order in `myapi.json` at the time of this handoff:

1. `DeepSeek-V4-Flash`
2. `DeepSeek-V4-Pro`
3. `GLM-5.1`

Behavior now:

- timeout no longer immediately aborts the whole project
- the workflow retries and can fall through to another model

### 3.2 Tool argument contract enforcement

Problem before:

- LLM plans could produce weak or invalid tool arguments
- this caused preventable repair failures

What was added:

- explicit tool schemas
- schema exposure to repair planning prompts
- one correction retry for malformed tool output

Relevant files:

- `/home/master/dprauto/src/dprauto/agent/tools/schema.py`
- `/home/master/dprauto/src/dprauto/adapters/llm/repair.py`

### 3.3 Verification evidence sent to the LLM

Problem before:

- test/run failures were not being presented to the LLM in a strong enough structured form
- the agent often repaired the wrong thing

What changed:

- verification failure evidence is injected into `FailureInfo`
- for test/run failures, the agent now uses verification evidence instead of blindly leaning on build logs

Relevant file:

- `/home/master/dprauto/src/dprauto/agent/workflow.py`

### 3.4 Build tool exposure cleanup

Problem before:

- the LLM had access to tools that should have remained orchestration-controlled

What changed:

- `build_image` was removed from LLM repair planning tools
- rebuild remains handled by workflow orchestration nodes

## 4. Test status before the real-project run

Before the long evaluation run, the main automated suite passed:

- `108 passed`
- `42 subtests passed`

This means the repository was in a reasonably stable pre-evaluation state, even though real-project success remained low.

## 5. Real-project evaluation status

### Datasets / paths used

Primary real-project source base:

- `/home/master/auto-build/CNB/cnb-benchmark/work/full-run/repos`

Evaluation harness:

- `/home/master/dprauto/evaluations/prompt12/run_evaluation.py`

Manifest:

- `/home/master/dprauto/evaluations/prompt12/manifest.json`

### Run outputs

First partial run (projects 1-3):

- `/home/master/dprauto/evaluations/prompt14/runs/third-round-20260812`

Continuation run (projects 4-24):

- `/home/master/dprauto/evaluations/prompt14/runs/third-round-20260813-part2`

Important note:

- project 4 was intentionally resumed in a fresh output directory to avoid checkpoint pollution from an interrupted earlier run

### Full 24-project result

Combined final numbers:

- total projects: 24
- standard build success: 12
- standard build failure: 12
- final success: 1
- agent participated: 22
- agent repair success: 0
- total LLM calls: 107
- LLM error/timeout records: 19
- total elapsed time: 17784.8s
- average elapsed per project: 741.0s

Final status distribution:

- `succeeded`: 1
- `verification_failed`: 4
- `max_attempts`: 15
- `project_failed`: 2
- `regression`: 1
- `infrastructure_failed`: 1

Only final success:

- `karpathy/minbpe`

## 6. What the evaluation proved

### Good news

1. Empty-content failures did not remain the dominant issue.
2. Single model timeout no longer terminates the whole workflow immediately.
3. Infrastructure failures can be isolated as infrastructure failures instead of being miscounted as project failures.
4. Regression detection is actually active.
5. Verification failures are now reaching the repair loop in structured form.

### Bad news

1. The overall real-project success rate is still extremely low.
2. Most failures collapse into `max_attempts`.
3. A large fraction of projects run into build timeout loops.
4. Standard build success does not translate into final environment success.
5. Agent repair quality is still not strong enough to convert real failures into stable final success.

## 7. Main problems identified

These are the highest-priority problems based on the 24-project run.

### Problem 1: `agent_max_total_seconds=900` is not acting as a hard stop

This is confirmed by runs such as:

- `artesiawater/hydropandas`: `1512.0s`
- `gamesdonequick/donation-tracker`: `1381.8s`

This is a workflow bug, not just a slow-project issue.

### Problem 2: Timeout-oriented repair strategy is weak

Many projects fell into patterns like:

- initial failure repaired by adding tools/dependencies
- resulting build became heavier
- build then timed out again

The system needs a stronger dedicated strategy for `build_command timed_out`.

### Problem 3: Test-failure repair quality is still poor

Even when the standard build succeeded, many projects still ended as:

- `verification_failed`
- `max_attempts`
- `regression`

The evidence is reaching the LLM better than before, but plan quality is still not enough.

### Problem 4: Verification-benefit vs dependency-cost tradeoff is not managed well

The agent often solves “missing test tool” by installing more dependencies, but this can make the build exceed timeout budget.

This needs a more explicit optimization strategy:

- minimum viable environment
- runtime vs test dependency slicing
- narrower test command selection

### Problem 5: LLM failover works, but still inflates runtime on hard cases

This is no longer the primary failure mode, but it is still a cost multiplier.

Worst examples:

- `robotframework/robotframework`: `10 calls / 6 errors`
- `artesiawater/hydropandas`: `8 / 4`
- `compserv/hknweb`: `7 / 2`
- `gamesdonequick/donation-tracker`: `7 / 2`

## 8. Recommended next steps

If work continues in a new conversation, the best order is:

1. Fix hard enforcement of `agent_max_total_seconds`
2. Add dedicated timeout-repair heuristics for build-timeout cases
3. Improve test-failure repair strategy so it does not blindly expand dependency installation
4. Add stricter pre-acceptance checks for repair plans that increase install scope
5. Re-run the same 24-project benchmark for comparison

## 9. Suggested immediate coding targets

If the next conversation continues directly into implementation, the best first target is:

- make total workflow timeout a real hard stop and add tests proving no run can exceed the configured cap except for minimal cleanup overhead

After that:

- add timeout-aware repair planning and evaluation rules

Examples of likely useful rules:

- detect when a proposed fix increases dependency surface dramatically
- prefer minimal packages over full dev groups
- distinguish “test tool missing” from “must reproduce full CI environment”
- downgrade overly expensive fixes before rebuild

## 10. Files most worth opening first in a new conversation

- `/home/master/dprauto/HANDOFF_SUMMARY_2026-08-13.md`
- `/home/master/dprauto/README.md`
- `/home/master/dprauto/src/dprauto/agent/workflow.py`
- `/home/master/dprauto/src/dprauto/adapters/llm/api.py`
- `/home/master/dprauto/src/dprauto/adapters/llm/repair.py`
- `/home/master/dprauto/src/dprauto/agent/tools/schema.py`
- `/home/master/dprauto/evaluations/prompt12/run_evaluation.py`
- `/home/master/dprauto/evaluations/prompt14/runs/third-round-20260813-part2/summary.json`

## 11. One-paragraph restart prompt

If you reopen a new conversation, this summary is enough:

“Continue work on `/home/master/dprauto`. The repository already has the full Python-first environment-build pipeline implemented: parser, deterministic build, failure classification, LangGraph repair workflow, verification, regression check, environment diff, checkpointing, and LLM failover/tool-schema enforcement. Real-project evaluation on 24 projects has been completed and stored under `evaluations/prompt14/runs/third-round-20260812` and `.../third-round-20260813-part2`. Current top issues are: `agent_max_total_seconds=900` is not a hard stop, many repairs fall into build timeout loops, test-failure repair quality is low, and final success is only 1/24. Start by fixing hard total-time stop and then improve timeout-aware repair strategy.”
