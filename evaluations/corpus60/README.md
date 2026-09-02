# DPRAuto cross-language 60-project corpus

This directory freezes a new, small-project capability corpus for DPRAuto. It contains 20 C++
projects from CXXCrafter's Top100 catalog, 20 Python projects from the EnvBench-Python catalog used
by HerAgent, and 20 Java projects from EnvBench's JVM catalog. All 60 repositories are absent from
the 38 repositories found in DPRAuto's earlier frozen evaluation JSON.

The word "previously" therefore means **previously evaluated by DPRAuto**. It cannot mean
"previously evaluated by CXXCrafter/HerAgent/EnvBench", because membership in those source
benchmarks is the requested provenance.

## Frozen result

- Manifest: `manifest.json`
- Historical exclusion evidence: `exclusions.json`
- Local source snapshots: `sources/` (60 directories, about 253 MiB; intentionally git-ignored)
- DPRAuto static probe: `probe-results.json` (generated and git-ignored)
- Immutable corpus identity: `freeze-lock.json`
- Preserved pre-refactor probe: `baselines/static-probe-a31a05847e4a.json`
- Evaluator-only component oracle: `hidden-oracles/workspaces.json`
- DPRAuto revision: `a31a05847e4a62cbb58c16913de05dd9a7a98fe7`

Python and Java use the exact revisions supplied by the benchmark catalogs. CXXCrafter's Top100
CSV has repository URLs but no revisions, so the manifest pins the reachable repository HEAD seen
on 2026-09-02. The downloaded snapshots contain no Git history. The safety extractor preserves
safe links but omits absolute or root-escaping links; DPRAuto's current `FileScanner` does not
follow or inspect symlinks.

## Selection rules

Python and Java candidates had to have the requested primary language, be neither archived nor
disabled in EnvBench's frozen repository metadata, contain 500–30,000 code lines, and have a
repository size of at most 20,000 KiB. The chosen Python set is 833–1,918 KiB and the Java set is
1,144–3,046 KiB in that metadata. Their exact snapshot archives are only 25–361 KiB.

C++ candidates had to occur in `top100_dataset.csv`, contain at least one C++ translation unit, be
reachable at a full 40-character commit, and have an exact compressed snapshot no larger than
20,000 KiB. The selected range is 86–16,044 KiB. Seventeen have a root build marker; Vireo,
Stockfish, and OpenALPR intentionally retain nested-only build entrypoints to test workspace
discovery.

## Projects

| Language | 20 repositories |
|---|---|
| C++ | skypjack/entt; google/guetzli; sqlitebrowser/sqlitebrowser; strukturag/libde265; treefrogframework/treefrog-framework; ipkn/crow; drogonframework/drogon; oatpp/oatpp; wjakob/nanogui; mikke89/RmlUi; flameshot-org/flameshot; polybar/polybar; twitter/vireo; pistacheio/pistache; exaloop/seq; aseprite/aseprite; official-stockfish/Stockfish; jacobdufault/cquery; cppit/jucipp; openalpr/openalpr |
| Python | eylles/pywal16; rstcheck/rstcheck; astropy/extension-helpers; cpcloud/protoletariat; zostera/django-bootstrap4; jaraco/inflect; asdf-format/asdf-standard; rollbar/pyrollbar; smarr/rebench; python/importlib_metadata; wolph/python-progressbar; m-o-a-t/moat-mqtt; pypa/twine; mollie/mollie-api-python; mov-cli/mov-cli; piskvorky/smart_open; microsoftgraph/msgraph-sdk-python-core; psf/pyperf; fatal1ty/mashumaro; ubernostrum/django-registration |
| Java | prisma-capacity/cryptoshred; liquibase/liquibase-hibernate; camunda-community-hub/zeebe-test-container; zalando/problem-spring-web; AxonIQ/axonserver-connector-java; dunctebot/skybot-lavalink-plugin; openrewrite/rewrite-maven-plugin; eclipse/microprofile-fault-tolerance; sigstore/sigstore-java; jenkinsci/plugin-compat-tester; mrniko/netty-socketio; micronaut-projects/micronaut-platform; jenkinsci/docker-plugin; avaje/avaje-inject; grapheneos/camera; scribejava/scribejava; braisdom/objectivesql; fortnoxab/reactive-wizard; grapheneos/attestationserver; gradle/native-platform |

## Reproduce and inspect

Run from `/home/master/dprauto`:

```bash
python3 evaluations/corpus60/build_manifest.py
python3 evaluations/corpus60/validate_corpus.py
python3 evaluations/corpus60/fetch_sources.py --workers 6
python3 evaluations/corpus60/validate_corpus.py --require-sources
python3 evaluations/corpus60/validate_corpus.py --require-sources --require-freeze
python3 evaluations/corpus60/freeze_corpus.py --check --require-sources
python3 evaluations/corpus60/validate_workspace_ground_truth.py
PYTHONPATH=src python3 evaluations/corpus60/probe_dprauto.py
```

The probe calls the same application planning service used by a production build, but deliberately
does not run 60 Docker builds. The manifest's language label is output metadata only; it is never
used to select a parser or strategy. A `planned` record means only that static plan creation
succeeded. It does not establish correct workspace selection, installability, testability,
runnability, or a successful Docker build. Those are scored and verified separately. Full
build/test/run execution is the next baseline stage and should write immutable run records rather
than overwrite `probe-results.json`.
