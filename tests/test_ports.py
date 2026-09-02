import unittest
from datetime import datetime, timezone
from pathlib import Path

from dprauto.domain.enums import BuildStage, BuildStatus, VerificationLevel, VerificationStatus
from dprauto.domain.models import (
    ArtifactRef,
    BuildPlan,
    BuildResult,
    BuildStep,
    CommandResult,
    CommandSpec,
    ProjectProfile,
    SourceReference,
    VerificationResult,
)
from dprauto.ports import (
    BuildPlanner,
    BuildStrategy,
    CommandExecutor,
    EnvironmentDiffer,
    FailureClassifier,
    FailureFallbackClassifier,
    LLMClient,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    ProjectParser,
    RegressionChecker,
    Storage,
    Verifier,
)


COMMAND = CommandSpec(("tool", "build"))
PROFILE = ProjectProfile("project", SourceReference("source"))
PLAN = BuildPlan(
    "plan",
    PROFILE.project_id,
    "dummy",
    (BuildStep("build", BuildStage.BUILD, COMMAND),),
)
NOW = datetime.now(timezone.utc)
RESULT = BuildResult("attempt", "plan", BuildStatus.SUCCEEDED, NOW, NOW, exit_code=0)


class DummyParser:
    def parse(self, source: SourceReference, workspace: Path) -> ProjectProfile:
        return PROFILE


class DummyBuildStrategy:
    def supports(self, profile: ProjectProfile) -> bool:
        return True

    def create_plan(self, profile: ProjectProfile) -> BuildPlan:
        return PLAN

    def build(self, plan: BuildPlan, workspace: Path, *, deadline_at=None) -> BuildResult:
        return RESULT


class DummyBuildPlanner:
    def plan(self, profile: ProjectProfile) -> tuple[BuildPlan, ...]:
        return (PLAN,)


class DummyExecutor:
    def execute(self, command: CommandSpec, workspace: Path) -> CommandResult:
        return CommandResult(command, 0)


class DummyEnvironmentDiffer:
    def snapshot(self, profile, workspace):
        return None

    def compare(self, before, after):
        return None


class DummyClassifier:
    def classify(self, profile, plan, result):
        return None


class DummyFailureFallback:
    def refine(self, profile, plan, preliminary):
        return preliminary


class DummyVerifier:
    def supports(self, profile: ProjectProfile, level: VerificationLevel) -> bool:
        return True

    def verify(self, context):
        return VerificationResult("verify", VerificationLevel.INSTALLABILITY, VerificationStatus.PASSED)


class DummyRegressionChecker:
    def create_baseline(self, profile, report):
        return None

    def check(self, baseline, current):
        return None


class DummyLLMClient:
    def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(content="ok")


class DummyStorage:
    def save(self, key: str, content: bytes, *, media_type="application/octet-stream"):
        return ArtifactRef(key, size_bytes=len(content), media_type=media_type)

    def load(self, artifact: ArtifactRef) -> bytes:
        return b"content"

    def exists(self, artifact: ArtifactRef) -> bool:
        return True


class PortContractTests(unittest.TestCase):
    def test_structural_implementations_satisfy_runtime_protocols(self) -> None:
        implementations = (
            (DummyParser(), ProjectParser),
            (DummyBuildPlanner(), BuildPlanner),
            (DummyBuildStrategy(), BuildStrategy),
            (DummyExecutor(), CommandExecutor),
            (DummyEnvironmentDiffer(), EnvironmentDiffer),
            (DummyClassifier(), FailureClassifier),
            (DummyFailureFallback(), FailureFallbackClassifier),
            (DummyVerifier(), Verifier),
            (DummyRegressionChecker(), RegressionChecker),
            (DummyLLMClient(), LLMClient),
            (DummyStorage(), Storage),
        )
        for implementation, protocol in implementations:
            with self.subTest(protocol=protocol.__name__):
                self.assertIsInstance(implementation, protocol)

    def test_port_dtos_and_dummy_calls_work(self) -> None:
        request = LLMRequest((LLMMessage("user", "diagnose this failure"),))
        self.assertEqual(DummyLLMClient().complete(request).content, "ok")
        artifact = DummyStorage().save("logs/build.log", b"content", media_type="text/plain")
        self.assertTrue(DummyStorage().exists(artifact))
        self.assertEqual(DummyStorage().load(artifact), b"content")


if __name__ == "__main__":
    unittest.main()
