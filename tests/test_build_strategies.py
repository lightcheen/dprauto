import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.config import BuildConfig
from dprauto.domain.enums import BuildStatus, CommandPurpose, ProjectType
from dprauto.domain.models import (
    CommandResult,
    CommandSpec,
    ProjectCommand,
    ProjectProfile,
    SourceReference,
)
from dprauto.ports import BuildStrategy
from dprauto.strategies import (
    CNBStrategy,
    DockerStrategy,
    JVMTemplateStrategy,
    NativeTemplateStrategy,
    RecordedBuildRunner,
    StrategyRegistry,
    TemplateStrategy,
)


class SuccessfulExecutor:
    def __init__(self, storage):
        self.storage = storage
        self.commands = []

    def execute(self, command, workspace):
        self.commands.append(command)
        log = self.storage.save("fake/build.log", b"build complete", media_type="text/plain")
        return CommandResult(command, 0, stdout=log, duration_seconds=0.01)


def python_profile(
    *,
    dockerfiles=(),
    managers=("pip",),
    dependencies=("requirements.txt",),
    build_files=(),
    python_constraint=">=3.11",
    commands=(),
    metadata=None,
    project_type=ProjectType.SCRIPT,
):
    return ProjectProfile(
        "example-python",
        SourceReference("fixture", revision="abc123"),
        languages=("Python",),
        project_type=project_type,
        runtime_constraints={"python": python_constraint},
        package_managers=managers,
        dependency_files=dependencies,
        build_files=build_files,
        dockerfiles=dockerfiles,
        commands=commands,
        metadata=metadata or {},
    )


def jvm_profile(*, system="maven", wrapper=True, java="17", commands=(), metadata=None):
    wrapper_file = "mvnw" if system == "maven" else "gradlew"
    root_file = "pom.xml" if system == "maven" else "build.gradle"
    return ProjectProfile(
        "example-jvm",
        SourceReference("fixture", revision="def456"),
        languages=("Java",),
        project_type=ProjectType.LIBRARY,
        runtime_constraints={"java": java},
        package_managers=(system,),
        dependency_files=(root_file,),
        build_files=((wrapper_file, root_file) if wrapper else (root_file,)),
        commands=commands,
        metadata={"primary_build_system": system, **(metadata or {})},
    )


def native_profile(
    *,
    system="cmake",
    languages=("C++",),
    build_files=(),
    metadata=None,
    project_type=ProjectType.LIBRARY,
    commands=(),
):
    defaults = {
        "cmake": ("CMakeLists.txt",),
        "meson": ("meson.build",),
        "autotools": ("configure.ac",),
        "make": ("Makefile",),
    }
    return ProjectProfile(
        "example-native",
        SourceReference("fixture", revision="789abc"),
        languages=languages,
        project_type=project_type,
        package_managers=(system,),
        build_files=build_files or defaults[system],
        commands=commands,
        metadata={"primary_build_system": system, **(metadata or {})},
    )


class BuildStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.storage = LocalArtifactStorage(self.root / "artifacts")
        self.executor = SuccessfulExecutor(self.storage)
        self.runner = RecordedBuildRunner(self.executor, self.storage)
        self.config = BuildConfig(image_repository="test-builds", timeout_seconds=30)

    def test_template_plan_is_stable_and_records_inputs_and_result(self) -> None:
        strategy = TemplateStrategy(self.runner, self.config)
        profile = python_profile()
        first = strategy.create_plan(profile)
        second = strategy.create_plan(profile)

        self.assertIsInstance(strategy, BuildStrategy)
        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual({item.path for item in first.generated_files}, {"Dockerfile", "setup.sh"})
        self.assertIn("python:3.11-slim", first.generated_files[0].content)

        result = strategy.build(first, self.root)
        self.assertEqual(result.status, BuildStatus.SUCCEEDED)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.image_reference.startswith("test-builds/"))
        self.assertEqual(len(result.logs), 1)
        self.assertTrue((self.root / "artifacts" / result.metadata["result_artifact"]).is_file())
        self.assertFalse(any("__DPRAUTO_GENERATED__" in arg for arg in self.executor.commands[0].argv))

    def test_jvm_maven_plan_uses_wrapper_and_retains_dependency_state(self) -> None:
        untrusted = ProjectCommand(
            "download only",
            CommandSpec(
                ("pip download -r cryptography.txt",),
                purpose=CommandPurpose.BUILD,
                shell=True,
            ),
            ".github/workflows/special.yml",
            1.0,
        )
        strategy = JVMTemplateStrategy(self.runner, self.config)
        first = strategy.create_plan(jvm_profile(commands=(untrusted,)))
        second = strategy.create_plan(jvm_profile(commands=(untrusted,)))
        dockerfile = first.generated_files[0].content

        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual(first.strategy, "jvm-template")
        self.assertEqual(first.metadata["base_image"], "maven:3.9.9-eclipse-temurin-17")
        self.assertEqual(first.metadata["build_command"], "./mvnw -B -DskipTests package")
        self.assertEqual(first.metadata["build_command_source"], "deterministic:maven-root-marker")
        self.assertTrue(first.metadata["dependency_state_retained_in_image"])
        self.assertIn("RUN chmod +x ./mvnw", dockerfile)
        self.assertIn('ENV MAVEN_CONFIG=""', dockerfile)
        self.assertIn("RUN ./mvnw -B -DskipTests package", dockerfile)
        self.assertIn("DPRAUTO_JVM_ARTIFACT_OK", dockerfile)
        self.assertNotIn("pip download", dockerfile)
        self.assertNotIn("type=cache,id=dprauto-maven", dockerfile)

    def test_jvm_gradle_plan_uses_fixed_tool_image_and_no_daemon(self) -> None:
        plan = JVMTemplateStrategy(self.runner, self.config).create_plan(
            jvm_profile(system="gradle", wrapper=False, java="11")
        )

        self.assertEqual(plan.metadata["base_image"], "gradle:8.12.1-jdk11")
        self.assertEqual(plan.metadata["build_command"], "gradle --no-daemon assemble")
        self.assertIn("*/build/libs/*.jar", plan.metadata["runtime_probe_command"])

    def test_jvm_maven_uses_repository_evidenced_license_skip(self) -> None:
        ci_command = ProjectCommand(
            "ci-test",
            CommandSpec(
                ("./mvnw test -B -Dlicense.skip=true",),
                purpose=CommandPurpose.TEST,
                shell=True,
            ),
            ".github/workflows/ci.yaml",
            0.95,
        )
        plan = JVMTemplateStrategy(self.runner, self.config).create_plan(
            jvm_profile(system="maven", wrapper=True, commands=(ci_command,))
        )

        self.assertEqual(
            plan.metadata["build_command"],
            "./mvnw -B -DskipTests -Dlicense.skip=true package",
        )
        self.assertEqual(
            plan.metadata["maven_license_skip_evidence"],
            ".github/workflows/ci.yaml",
        )

    def test_jvm_gradle_wrapper_prefetches_with_bounded_timeout_and_retry(self) -> None:
        plan = JVMTemplateStrategy(self.runner, self.config).create_plan(
            jvm_profile(system="gradle", wrapper=True, java="17")
        )
        dockerfile = plan.generated_files[0].content

        self.assertIn("ENV GRADLE_USER_HOME=/opt/dprauto-gradle", dockerfile)
        self.assertIn("/usr/local/bin/dprauto-gradle-proxy", dockerfile)
        self.assertIn("JAVA_TOOL_OPTIONS", dockerfile)
        self.assertIn("type=cache,id=dprauto-gradle-wrapper", dockerfile)
        self.assertIn("networkTimeout=120000", dockerfile)
        self.assertIn("DPRAUTO_GRADLE_PREFETCH_ATTEMPT=$attempt", dockerfile)
        self.assertIn("./gradlew --no-daemon --version", dockerfile)
        self.assertLess(
            dockerfile.index("./gradlew --no-daemon --version"),
            dockerfile.index("./gradlew --no-daemon assemble"),
        )
        self.assertEqual(plan.metadata["wrapper_network_timeout_milliseconds"], 120000)
        self.assertEqual(plan.metadata["wrapper_prefetch_attempts"], 3)
        self.assertTrue(plan.metadata["wrapper_distribution_prefetch_command"])

    def test_jvm_gradle_uses_separate_launcher_and_compilation_toolchain(self) -> None:
        plan = JVMTemplateStrategy(self.runner, self.config).create_plan(
            jvm_profile(
                system="gradle",
                wrapper=True,
                java="11",
                metadata={
                    "java_target_version": "8",
                    "java_target_version_evidence": "build.gradle:toolchain",
                },
            )
        )
        dockerfile = plan.generated_files[0].content

        self.assertEqual(plan.metadata["base_image"], "gradle:8.12.1-jdk11")
        self.assertEqual(plan.metadata["java_version"], "11")
        self.assertEqual(plan.metadata["java_toolchain_version"], "8")
        self.assertEqual(
            plan.metadata["java_toolchain_base_image"],
            "maven:3.9.9-eclipse-temurin-8",
        )
        self.assertIn(
            "FROM maven:3.9.9-eclipse-temurin-8 AS dprauto-jvm-toolchain",
            dockerfile,
        )
        self.assertIn("FROM gradle:8.12.1-jdk11", dockerfile)
        self.assertIn(
            "COPY --from=dprauto-jvm-toolchain /opt/java/openjdk "
            "/opt/dprauto-jdks/temurin-8",
            dockerfile,
        )
        self.assertIn(
            "printf '\\norg.gradle.java.installations.paths="
            "/opt/dprauto-jdks/temurin-8\\n' >> gradle.properties",
            dockerfile,
        )

    def test_generated_context_excludes_dataset_sentinel(self) -> None:
        plan = JVMTemplateStrategy(self.runner, self.config).create_plan(
            jvm_profile(system="maven", wrapper=False)
        )

        self.assertIn(".cnb-benchmark-source-ready", plan.generated_files[1].content)

    def test_native_cmake_plan_installs_bounded_toolchain_and_pipeline(self) -> None:
        plan = NativeTemplateStrategy(self.runner, self.config).create_plan(native_profile())
        dockerfile = plan.generated_files[0].content

        self.assertEqual(plan.strategy, "native-template")
        self.assertEqual(plan.metadata["build_system"], "cmake")
        self.assertEqual(
            {item.path for item in plan.generated_files},
            {"Dockerfile", "Dockerfile.dockerignore"},
        )
        self.assertIn(".git", plan.generated_files[1].content)
        self.assertEqual(
            plan.metadata["build_commands"],
            ("cmake -S . -B build", "cmake --build build --parallel 4"),
        )
        self.assertEqual(
            plan.metadata["system_packages"],
            ("ca-certificates", "g++", "make", "pkg-config", "cmake"),
        )
        self.assertIn("apt-get", dockerfile)
        self.assertIn("USER root", dockerfile)
        self.assertIn("DPRAUTO_NATIVE_BUILD_OK", dockerfile)
        self.assertIn('test "$count" -gt 0', plan.metadata["runtime_probe_command"])

    def test_native_autotools_bootstraps_only_when_root_requires_it(self) -> None:
        generated = NativeTemplateStrategy(self.runner, self.config).create_plan(
            native_profile(system="autotools", languages=("C",), build_files=("configure.ac",))
        )
        configured = NativeTemplateStrategy(self.runner, self.config).create_plan(
            native_profile(system="autotools", languages=("C",), build_files=("configure",))
        )

        self.assertIn("autoconf", generated.metadata["system_packages"])
        self.assertEqual(generated.metadata["build_commands"][0], "autoreconf -fi && ./configure")
        self.assertNotIn("autoconf", configured.metadata["system_packages"])
        self.assertEqual(
            configured.metadata["build_commands"][0],
            "chmod +x ./configure && ./configure",
        )

        bootstrapped = NativeTemplateStrategy(self.runner, self.config).create_plan(
            native_profile(
                system="autotools",
                languages=("C",),
                build_files=("autogen.sh", "configure.ac"),
            )
        )
        self.assertIn("./autogen.sh", bootstrapped.metadata["build_commands"][0])
        self.assertIn("if [ ! -f Makefile ]", bootstrapped.metadata["build_commands"][0])
        self.assertIn("./configure", bootstrapped.metadata["build_commands"][0])

    def test_native_cmake_never_executes_a_test_target_during_image_build(self) -> None:
        plan = NativeTemplateStrategy(self.runner, self.config).create_plan(
            native_profile(metadata={"cmake_test_build_target": "all_tests"})
        )

        self.assertEqual(
            plan.metadata["build_commands"][1],
            "cmake --build build --parallel 4",
        )

    def test_python_web_image_uses_only_a_persistent_server_command(self) -> None:
        check = ProjectCommand(
            "check",
            CommandSpec(
                ("python manage.py check",),
                purpose=CommandPurpose.RUN,
                shell=True,
            ),
            "framework:manage.py",
            0.99,
        )
        server = ProjectCommand(
            "server",
            CommandSpec(
                ("python manage.py runserver",),
                purpose=CommandPurpose.RUN,
                shell=True,
            ),
            "README.md",
            0.8,
        )
        migration = ProjectCommand(
            "migration",
            CommandSpec(
                ("python manage.py migrate",),
                purpose=CommandPurpose.INSTALL,
                shell=True,
            ),
            "README.md",
            0.8,
        )

        plan = TemplateStrategy(self.runner, self.config).create_plan(
            python_profile(
                commands=(check, server, migration),
                project_type=ProjectType.WEB,
            )
        )
        dockerfile = plan.generated_files[0].content

        self.assertIn(
            "python manage.py migrate --noinput && python manage.py runserver 0.0.0.0:8000",
            dockerfile,
        )
        self.assertNotIn("python manage.py check", dockerfile)

    def test_native_cli_runnability_executes_the_root_binary(self) -> None:
        run = ProjectCommand(
            "run-1",
            CommandSpec(("build/demo --version",), purpose=CommandPurpose.RUN, shell=True),
            "inferred:root-executable-target",
            0.9,
        )
        plan = NativeTemplateStrategy(self.runner, self.config).create_plan(
            native_profile(project_type=ProjectType.CLI, commands=(run,))
        )

        self.assertEqual(plan.metadata["runtime_probe_type"], "native-cli-command")
        self.assertIn("build/demo --version", plan.metadata["runtime_probe_command"])

    def test_runner_clamps_command_timeout_to_remaining_budget(self) -> None:
        strategy = TemplateStrategy(self.runner, self.config)
        plan = strategy.create_plan(python_profile())
        deadline_at = datetime.now(timezone.utc) + timedelta(seconds=5)

        result = strategy.build(plan, self.root, deadline_at=deadline_at)

        self.assertEqual(result.status, BuildStatus.SUCCEEDED)
        self.assertGreaterEqual(self.executor.commands[0].timeout_seconds, 1)
        self.assertLessEqual(self.executor.commands[0].timeout_seconds, 5)

    def test_docker_strategy_prefers_canonical_dockerfile(self) -> None:
        strategy = DockerStrategy(self.runner, self.config)
        plan = strategy.create_plan(
            python_profile(dockerfiles=("docker/Dockerfile.dev", "Dockerfile"))
        )

        self.assertEqual(plan.metadata["dockerfile"], "Dockerfile")
        self.assertIn("Dockerfile", plan.steps[0].command.argv)

    def test_template_installs_requirements_and_project_package(self) -> None:
        plan = TemplateStrategy(self.runner, self.config).create_plan(
            python_profile(build_files=("setup.py",))
        )
        setup = next(item.content for item in plan.generated_files if item.path == "setup.sh")

        self.assertIn("pip install -r requirements.txt", setup)
        self.assertIn("pip install .", setup)
        self.assertNotIn("--no-cache-dir", setup)
        self.assertIn("--mount=type=cache,id=dprauto-python-packages", plan.generated_files[0].content)

    def test_template_keeps_default_python_when_it_satisfies_a_lower_bound(self) -> None:
        strategy = TemplateStrategy(self.runner, self.config)

        broad = strategy.create_plan(python_profile(python_constraint=">=3.7"))
        bounded = strategy.create_plan(python_profile(python_constraint=">=3.7,<3.11"))

        self.assertEqual(broad.metadata["base_image"], "python:3.11-slim")
        self.assertEqual(bounded.metadata["base_image"], "python:3.7-slim")

    def test_template_resolves_upper_bounds_and_compatible_release_constraints(self) -> None:
        strategy = TemplateStrategy(self.runner, self.config)

        upper_bound = strategy.create_plan(python_profile(python_constraint="<3.10"))
        compatible_release = strategy.create_plan(
            python_profile(python_constraint="~=3.8.1")
        )

        self.assertEqual(upper_bound.metadata["base_image"], "python:3.9-slim")
        self.assertEqual(compatible_release.metadata["base_image"], "python:3.8-slim")

    def test_template_excludes_declared_test_dependencies_and_tools_by_default(self) -> None:
        test_command = ProjectCommand(
            "project tests",
            CommandSpec(("python -m pytest -q",), purpose=CommandPurpose.TEST, shell=True),
            "pyproject.toml",
            0.9,
        )
        plan = TemplateStrategy(self.runner, self.config).create_plan(
            python_profile(
                dependencies=("requirements.txt", "requirements-test.txt", "pyproject.toml"),
                build_files=("pyproject.toml",),
                commands=(test_command,),
                metadata={"test_dependency_groups": ("test", "dev")},
            )
        )
        setup = next(item.content for item in plan.generated_files if item.path == "setup.sh")

        self.assertIn("-r requirements.txt", setup)
        self.assertIn("pip install .", setup)
        self.assertNotIn("-r requirements-test.txt", setup)
        self.assertNotIn("'.[test,dev]'", setup)
        self.assertNotIn("pip install pytest", setup)

    def test_template_can_opt_into_test_dependencies_for_legacy_builds(self) -> None:
        test_command = ProjectCommand(
            "project tests",
            CommandSpec(("python -m pytest -q",), purpose=CommandPurpose.TEST, shell=True),
            "pyproject.toml",
            0.9,
        )
        plan = TemplateStrategy(self.runner, self.config).create_plan(
            python_profile(
                dependencies=("requirements.txt", "requirements-test.txt", "pyproject.toml"),
                build_files=("pyproject.toml",),
                commands=(test_command,),
                metadata={
                    "test_dependency_groups": ("test", "dev"),
                    "include_test_dependencies_in_build": True,
                },
            )
        )
        setup = next(item.content for item in plan.generated_files if item.path == "setup.sh")

        self.assertIn("-r requirements-test.txt", setup)
        self.assertIn("'.[test,dev]'", setup)
        self.assertIn("pip install pytest", setup)

        poetry_plan = TemplateStrategy(self.runner, self.config).create_plan(
            python_profile(
                managers=("poetry",),
                dependencies=("pyproject.toml",),
                metadata={"include_test_dependencies_in_build": True},
            )
        )
        poetry_setup = next(
            item.content for item in poetry_plan.generated_files if item.path == "setup.sh"
        )
        self.assertIn("poetry install --no-interaction --no-ansi", poetry_setup)
        self.assertNotIn("--only main", poetry_setup)

    def test_template_package_managers_install_only_runtime_dependencies(self) -> None:
        poetry = TemplateStrategy(self.runner, self.config).create_plan(
            python_profile(managers=("poetry",), dependencies=("pyproject.toml",))
        )
        setup = next(item.content for item in poetry.generated_files if item.path == "setup.sh")
        dockerfile = next(
            item.content for item in poetry.generated_files if item.path == "Dockerfile"
        )

        self.assertIn("poetry install --only main --no-interaction --no-ansi", setup)
        self.assertIn("poetry==1.8.5", setup)
        self.assertIn("POETRY_VIRTUALENVS_CREATE=false", dockerfile)

        uv = TemplateStrategy(self.runner, self.config).create_plan(
            python_profile(managers=("uv",), dependencies=("pyproject.toml", "uv.lock"))
        )
        uv_setup = next(item.content for item in uv.generated_files if item.path == "setup.sh")
        uv_dockerfile = next(
            item.content for item in uv.generated_files if item.path == "Dockerfile"
        )
        self.assertIn("uv sync --frozen --no-default-groups --no-editable", uv_setup)
        self.assertIn('/workspace/.venv/bin:$PATH', uv_dockerfile)

        pdm = TemplateStrategy(self.runner, self.config).create_plan(
            python_profile(managers=("pdm",), dependencies=("pyproject.toml", "pdm.lock"))
        )
        pdm_setup = next(item.content for item in pdm.generated_files if item.path == "setup.sh")
        self.assertIn("pdm sync --prod --no-editable", pdm_setup)

        unlocked_pdm = TemplateStrategy(self.runner, self.config).create_plan(
            python_profile(managers=("pdm",), dependencies=("pyproject.toml",))
        )
        unlocked_setup = next(
            item.content
            for item in unlocked_pdm.generated_files
            if item.path == "setup.sh"
        )
        self.assertIn("pdm install --prod --no-editable", unlocked_setup)
        self.assertNotIn("pdm sync", unlocked_setup)

    def test_poetry_tool_image_is_versioned_and_skips_per_project_bootstrap(self) -> None:
        config = BuildConfig(
            poetry_version="1.8.5",
            poetry_tool_image=(
                "dprauto-tools/python-poetry:python-{version}-poetry-{poetry_version}"
            ),
        )
        plan = TemplateStrategy(self.runner, config).create_plan(
            python_profile(managers=("poetry",), dependencies=("pyproject.toml",))
        )
        dockerfile = next(
            item.content for item in plan.generated_files if item.path == "Dockerfile"
        )
        setup = next(item.content for item in plan.generated_files if item.path == "setup.sh")

        self.assertIn(
            "FROM dprauto-tools/python-poetry:python-3.11-poetry-1.8.5",
            dockerfile,
        )
        self.assertNotIn("pip install", setup)
        self.assertIn("poetry check --lock", setup)
        self.assertIn("poetry lock --no-update", setup)
        self.assertIn("poetry install --only main --no-interaction --no-ansi", setup)
        self.assertEqual(plan.metadata["runtime_base_image"], "python:3.11-slim")

        pip_plan = TemplateStrategy(self.runner, config).create_plan(python_profile())
        pip_dockerfile = next(
            item.content for item in pip_plan.generated_files if item.path == "Dockerfile"
        )
        self.assertIn("FROM python:3.11-slim", pip_dockerfile)
        self.assertNotIn("dprauto-tools/python-poetry", pip_dockerfile)

    def test_disabling_build_cache_omits_buildkit_cache_mount(self) -> None:
        plan = TemplateStrategy(
            self.runner,
            BuildConfig(image_repository="test-builds", use_cache=False),
        ).create_plan(python_profile(build_files=("setup.py",)))
        dockerfile = next(
            item.content for item in plan.generated_files if item.path == "Dockerfile"
        )

        self.assertNotIn("--mount=type=cache", dockerfile)
        self.assertIn("--no-cache", plan.steps[0].command.argv)

    def test_high_confidence_system_dependency_hints_add_bounded_packages(self) -> None:
        plan = TemplateStrategy(self.runner, self.config).create_plan(
            python_profile(
                build_files=("pyproject.toml",),
                metadata={
                    "system_dependency_hints": ("git-vcs", "pyscard-native")
                },
            )
        )
        setup = next(item.content for item in plan.generated_files if item.path == "setup.sh")
        dockerfile = next(
            item.content for item in plan.generated_files if item.path == "Dockerfile"
        )

        self.assertIn(
            "install -y --no-install-recommends ca-certificates git gcc libc6-dev",
            setup,
        )
        self.assertIn("libpcsclite-dev swig", setup)
        self.assertIn("apk add --no-cache ca-certificates git gcc musl-dev", setup)
        self.assertIn("pcsc-lite-dev swig", setup)
        self.assertIn("id=dprauto-apt-cache", dockerfile)
        self.assertIn("id=dprauto-apt-lists", dockerfile)
        self.assertEqual(
            plan.metadata["deterministic_fixes"],
            (
                "system-dependency:git-vcs",
                "system-dependency:pyscard-native",
            ),
        )

    def test_tmux_executable_hint_installs_bounded_system_package(self) -> None:
        # The executable is a project test prerequisite, not a Python import.
        tmux_plan = TemplateStrategy(self.runner, self.config).create_plan(
            python_profile(
                build_files=("pyproject.toml",),
                metadata={"system_dependency_hints": ("tmux-executable",)},
            )
        )
        tmux_setup = next(
            item.content for item in tmux_plan.generated_files if item.path == "setup.sh"
        )
        self.assertIn("install -y --no-install-recommends tmux", tmux_setup)
        self.assertIn("apk add --no-cache tmux", tmux_setup)

    def test_setuptools_scm_uses_project_specific_revision_version(self) -> None:
        profile = ProjectProfile(
            "scm-project",
            SourceReference("fixture", revision="0123456789ABCDEF0123456789ABCDEF01234567"),
            languages=("Python",),
            project_type=ProjectType.LIBRARY,
            package_managers=("pip",),
            dependency_files=("pyproject.toml",),
            build_files=("pyproject.toml",),
            metadata={
                "project_name": "demo-lib",
                "project_name_declared": True,
                "scm_versioning": {"provider": "setuptools-scm"},
            },
        )

        plan = TemplateStrategy(self.runner, self.config).create_plan(profile)
        dockerfile = next(
            item.content for item in plan.generated_files if item.path == "Dockerfile"
        )
        setup = next(item.content for item in plan.generated_files if item.path == "setup.sh")
        assignment = "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_DEMO_LIB=0.0+g0123456789ab"

        self.assertIn(f"ENV {assignment}", dockerfile)
        self.assertIn(f"export {assignment}", setup)
        self.assertNotIn("ENV SETUPTOOLS_SCM_PRETEND_VERSION=", dockerfile)
        self.assertEqual(plan.metadata["scm_pretend_version"], {
            "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_DEMO_LIB": "0.0+g0123456789ab"
        })
        self.assertIn(
            "setuptools-scm:pretend-version-from-revision",
            plan.metadata["deterministic_fixes"],
        )

    def test_setuptools_scm_uses_unreleased_changelog_version_baseline(self) -> None:
        profile = ProjectProfile(
            "scm-project",
            SourceReference("fixture", revision="0123456789abcdef0123456789abcdef"),
            languages=("Python",),
            project_type=ProjectType.LIBRARY,
            package_managers=("pip",),
            dependency_files=("pyproject.toml",),
            build_files=("pyproject.toml",),
            metadata={
                "project_name": "demo-lib",
                "project_name_declared": True,
                "scm_versioning": {
                    "provider": "setuptools-scm",
                    "version": "3.3.0",
                    "version_kind": "unreleased-changelog",
                    "version_source": "CHANGES.rst:first-version-heading",
                },
            },
        )

        plan = TemplateStrategy(self.runner, self.config).create_plan(profile)
        dockerfile = next(
            item.content for item in plan.generated_files if item.path == "Dockerfile"
        )
        assignment = (
            "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_DEMO_LIB="
            "3.3.0.dev0+g0123456789ab"
        )

        self.assertIn(f"ENV {assignment}", dockerfile)
        self.assertEqual(
            plan.metadata["scm_pretend_version_source"],
            "CHANGES.rst:first-version-heading",
        )
        self.assertIn(
            "setuptools-scm:pretend-version-from-project-evidence",
            plan.metadata["deterministic_fixes"],
        )

    def test_setuptools_scm_preserves_generated_exact_version(self) -> None:
        profile = ProjectProfile(
            "scm-project",
            SourceReference("fixture", revision="0123456789abcdef"),
            languages=("Python",),
            metadata={
                "project_name": "demo-lib",
                "project_name_declared": True,
                "scm_versioning": {
                    "provider": "setuptools-scm",
                    "version": "2.4.1.dev3+gabcdef0",
                    "version_kind": "generated-file",
                    "version_source": "src/demo_lib/_version.py",
                },
            },
        )

        plan = TemplateStrategy(self.runner, self.config).create_plan(profile)

        self.assertEqual(
            plan.metadata["scm_pretend_version"],
            {
                "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_DEMO_LIB": (
                    "2.4.1.dev3+gabcdef0"
                )
            },
        )

    def test_setuptools_scm_version_is_not_injected_without_safe_inputs(self) -> None:
        strategy = TemplateStrategy(self.runner, self.config)
        cases = (
            ("not-a-commit", "demo-lib", {"provider": "setuptools-scm"}),
            ("0123456789abcdef", "demo lib", {"provider": "setuptools-scm"}),
            ("0123456789abcdef", "a" * 129, {"provider": "setuptools-scm"}),
            ("0123456789abcdef", "demo-lib", {}),
        )
        for revision, project_name, scm_versioning in cases:
            with self.subTest(revision=revision, project_name=project_name):
                profile = ProjectProfile(
                    "scm-project",
                    SourceReference("fixture", revision=revision),
                    languages=("Python",),
                    project_type=ProjectType.LIBRARY,
                    build_files=("pyproject.toml",),
                    metadata={
                        "project_name": project_name,
                        "project_name_declared": True,
                        "scm_versioning": scm_versioning,
                    },
                )
                plan = strategy.create_plan(profile)
                dockerfile = next(
                    item.content for item in plan.generated_files if item.path == "Dockerfile"
                )
                self.assertNotIn("SETUPTOOLS_SCM_PRETEND_VERSION", dockerfile)
                self.assertEqual(plan.metadata["scm_pretend_version"], {})

    def test_setuptools_scm_does_not_use_workspace_name_as_package_name(self) -> None:
        profile = ProjectProfile(
            "scm-project",
            SourceReference("fixture", revision="0123456789abcdef"),
            languages=("Python",),
            project_type=ProjectType.LIBRARY,
            build_files=("pyproject.toml",),
            metadata={
                "project_name": "workspace-fallback",
                "project_name_declared": False,
                "scm_versioning": {"provider": "setuptools-scm"},
            },
        )

        plan = TemplateStrategy(self.runner, self.config).create_plan(profile)
        self.assertEqual(plan.metadata["scm_pretend_version"], {})

    def test_poetry_two_lock_refresh_uses_current_lock_semantics(self) -> None:
        plan = TemplateStrategy(
            self.runner,
            BuildConfig(poetry_version="2.1.4"),
        ).create_plan(
            python_profile(managers=("poetry",), dependencies=("pyproject.toml",))
        )
        setup = next(item.content for item in plan.generated_files if item.path == "setup.sh")

        self.assertIn("poetry check --lock", setup)
        self.assertIn("|| poetry lock --no-interaction", setup)
        self.assertNotIn("poetry lock --no-update", setup)

    def test_cnb_strategy_records_fixed_builder(self) -> None:
        config = BuildConfig(
            image_repository="test-builds",
            pack_binary="/opt/pack",
            cnb_builder="example/builder@sha256:abc",
        )
        strategy = CNBStrategy(self.runner, config)
        plan = strategy.create_plan(python_profile())

        self.assertTrue(strategy.supports(python_profile()))
        self.assertEqual(plan.metadata["metric"], "installability")
        self.assertIn("example/builder@sha256:abc", plan.steps[0].command.argv)

    def test_configured_network_is_used_by_all_build_strategies(self) -> None:
        config = BuildConfig(
            image_repository="test-builds",
            docker_network="host",
        )
        profile = python_profile(dockerfiles=("Dockerfile",))

        for strategy in (
            DockerStrategy(self.runner, config),
            TemplateStrategy(self.runner, config),
            CNBStrategy(self.runner, config),
        ):
            with self.subTest(strategy=strategy.name):
                argv = strategy.create_plan(profile).steps[0].command.argv
                self.assertIn("--network", argv)
                self.assertEqual(argv[argv.index("--network") + 1], "host")

    def test_docker_builds_forward_proxy_names_without_recording_values(self) -> None:
        profile = python_profile(dockerfiles=("Dockerfile",))
        for strategy in (
            DockerStrategy(self.runner, self.config),
            TemplateStrategy(self.runner, self.config),
        ):
            with self.subTest(strategy=strategy.name):
                plan = strategy.create_plan(profile)
                argv = plan.steps[0].command.argv
                self.assertEqual(argv.count("--build-arg"), 6)
                self.assertIn("HTTP_PROXY", argv)
                self.assertIn("https_proxy", argv)
                self.assertFalse(any("http://" in argument for argument in argv))
                self.assertTrue(plan.metadata["proxy_environment_forwarded"])

    def test_proxy_forwarding_can_be_disabled_for_every_strategy(self) -> None:
        config = BuildConfig(forward_proxy_environment=False)
        profile = python_profile(dockerfiles=("Dockerfile",))

        docker_plans = (
            DockerStrategy(self.runner, config).create_plan(profile),
            TemplateStrategy(self.runner, config).create_plan(profile),
        )
        for plan in docker_plans:
            self.assertNotIn("--build-arg", plan.steps[0].command.argv)
            self.assertFalse(plan.metadata["proxy_environment_forwarded"])

        cnb = CNBStrategy(self.runner, config).create_plan(profile)
        self.assertEqual(len(cnb.steps[0].command.environment), 6)
        self.assertTrue(
            all(value == "" for value in cnb.steps[0].command.environment.values())
        )
        self.assertFalse(cnb.metadata["proxy_environment_forwarded"])

    def test_disabled_network_overrides_configured_network(self) -> None:
        config = BuildConfig(allow_network=False, docker_network="dprauto-eval")

        for strategy in (
            DockerStrategy(self.runner, config),
            TemplateStrategy(self.runner, config),
            CNBStrategy(self.runner, config),
        ):
            with self.subTest(strategy=strategy.name):
                argv = strategy.create_plan(
                    python_profile(dockerfiles=("Dockerfile",))
                ).steps[0].command.argv
                self.assertEqual(argv[argv.index("--network") + 1], "none")
                self.assertNotIn("--build-arg", argv)

    def test_registry_preserves_explicit_priority(self) -> None:
        docker = DockerStrategy(self.runner, self.config)
        template = TemplateStrategy(self.runner, self.config)
        registry = StrategyRegistry((docker, template))

        self.assertIs(registry.select(python_profile(dockerfiles=("Dockerfile",))), docker)
        self.assertIs(registry.select(python_profile()), template)

    def test_registry_returns_ordered_portfolio_and_skips_unavailable_pack(self) -> None:
        docker = DockerStrategy(self.runner, self.config)
        template = TemplateStrategy(self.runner, self.config)
        cnb = CNBStrategy(self.runner, self.config)
        registry = StrategyRegistry((docker, template, cnb))

        with patch("dprauto.strategies.cnb.shutil.which", return_value=None):
            candidates = registry.candidates(
                python_profile(dockerfiles=("Dockerfile",))
            )

        self.assertEqual(candidates, (docker, template))


if __name__ == "__main__":
    unittest.main()
