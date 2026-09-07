"""Evidence-based build ecosystem and project-root detection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from dprauto.adapters.multilang.detector import LANGUAGE_SUFFIXES
from dprauto.inspection.scanner import ScannedProject


_MAX_BUILD_ROOT_DEPTH = 2
_LOW_SIGNAL_DIRECTORIES = {
    "benchmark",
    "benchmarks",
    "demo",
    "demos",
    "doc",
    "docs",
    "example",
    "examples",
    "fixture",
    "fixtures",
    "test",
    "tests",
    "third_party",
    "vendor",
}

_MARKER_WEIGHTS: dict[str, dict[str, int]] = {
    "jvm-rules-v1": {
        "pom.xml": 180,
        "build.gradle": 180,
        "build.gradle.kts": 180,
        "settings.gradle": 120,
        "settings.gradle.kts": 120,
        "gradlew": 100,
        "mvnw": 100,
    },
    "native-rules-v1": {
        "cmakelists.txt": 180,
        "meson.build": 180,
        "configure": 170,
        "configure.ac": 170,
        "configure.in": 170,
        "makefile": 140,
    },
    "python-rules-v1": {
        "pyproject.toml": 170,
        "setup.py": 170,
        "setup.cfg": 150,
        "poetry.lock": 130,
        "pdm.lock": 130,
        "uv.lock": 130,
        "pipfile": 110,
        "pipfile.lock": 110,
        "environment.yml": 100,
        "environment.yaml": 100,
        "requirements.txt": 90,
    },
}

_LANGUAGE_FAMILIES = {
    "jvm-rules-v1": frozenset({"Java", "Kotlin", "Groovy"}),
    "native-rules-v1": frozenset({"C", "C++"}),
    "python-rules-v1": frozenset({"Python"}),
}


@dataclass(frozen=True, slots=True)
class EcosystemCandidate:
    """One auditable parser/build-root candidate."""

    parser_name: str
    build_root: str
    score: int
    confidence: float
    evidence: tuple[str, ...]

    def as_metadata(self) -> dict[str, object]:
        return {
            "parser": self.parser_name,
            "build_root": self.build_root,
            "score": self.score,
            "confidence": self.confidence,
            "evidence": self.evidence,
        }


class BuildEcosystemDetector:
    """Rank build ecosystems using manifests before incidental source files."""

    def candidates(self, scanned: ScannedProject) -> tuple[EcosystemCandidate, ...]:
        grouped: dict[tuple[str, str], list[tuple[str, int]]] = {}
        for relative_path in scanned.files:
            path = PurePosixPath(relative_path)
            build_root = path.parent.as_posix()
            build_root = "." if build_root == "." else build_root
            if len(path.parent.parts) > _MAX_BUILD_ROOT_DEPTH:
                continue
            name = path.name.casefold()
            for parser_name, markers in _MARKER_WEIGHTS.items():
                weight = markers.get(name)
                if parser_name == "python-rules-v1" and weight is not None:
                    weight = self._python_marker_weight(scanned, relative_path, name, weight)
                if weight is None and parser_name == "python-rules-v1":
                    if name.startswith("requirements") and name.endswith((".txt", ".in")):
                        weight = 80
                if weight is not None:
                    grouped.setdefault((parser_name, build_root), []).append(
                        (relative_path, weight)
                    )

        candidates = [
            self._candidate(scanned, parser_name, build_root, markers)
            for (parser_name, build_root), markers in grouped.items()
        ]
        if not any(item.parser_name == "python-rules-v1" for item in candidates):
            python_files = tuple(path for path in scanned.files if path.endswith(".py"))
            if python_files:
                affinity = min(40, len(python_files) * 2)
                candidates.append(
                    EcosystemCandidate(
                        "python-rules-v1",
                        ".",
                        20 + affinity,
                        0.45,
                        (f"python-source-files:{len(python_files)}",),
                    )
                )
        return tuple(
            sorted(
                candidates,
                key=lambda item: (-item.score, item.parser_name, item.build_root),
            )
        )

    @staticmethod
    def _python_marker_weight(
        scanned: ScannedProject,
        relative_path: str,
        name: str,
        default: int,
    ) -> int:
        """Demote formatter/linter-only files that are not Python package manifests."""

        content = scanned.read_text(relative_path)
        if name == "pyproject.toml" and not any(
            section in content
            for section in (
                "[build-system]",
                "[project]",
                "[tool.poetry]",
                "[tool.pdm]",
                "[tool.hatch]",
            )
        ):
            return 50
        if name == "setup.cfg" and not any(
            section in content for section in ("[metadata]", "[options]")
        ):
            return 50
        return default

    def _candidate(
        self,
        scanned: ScannedProject,
        parser_name: str,
        build_root: str,
        markers: list[tuple[str, int]],
    ) -> EcosystemCandidate:
        root = PurePosixPath(build_root)
        expected_languages = _LANGUAGE_FAMILIES[parser_name]
        source_count = 0
        prefix = "" if build_root == "." else build_root + "/"
        for relative_path in scanned.files:
            if prefix and not relative_path.startswith(prefix):
                continue
            language = LANGUAGE_SUFFIXES.get(PurePosixPath(relative_path).suffix.casefold())
            if language in expected_languages:
                source_count += 1

        marker_score = sum(weight for _, weight in markers)
        strongest_marker = max(weight for _, weight in markers)
        # A real root-level build manifest defines the selected repository.
        # The large bonus prevents bundled libraries with several build systems
        # from outranking it. Tool-only Python configuration is deliberately
        # below the threshold and receives only a small location preference.
        root_bonus = (
            400 if build_root == "." and strongest_marker >= 80
            else 30 if build_root == "."
            else 0
        )
        depth_penalty = 15 * (0 if build_root == "." else len(root.parts))
        low_signal_penalty = 500 if any(
            part.casefold().replace("-", "_") in _LOW_SIGNAL_DIRECTORIES
            for part in root.parts
        ) else 0
        affinity = min(60, source_count * 3)
        score = max(1, marker_score + root_bonus + affinity - depth_penalty - low_signal_penalty)
        confidence = min(
            0.99,
            0.60
            + min(marker_score, 300) / 1_000
            + min(affinity, 60) / 600
            + (0.05 if build_root == "." else 0.0),
        )
        evidence = tuple(path for path, _ in markers) + (
            f"matching-source-files:{source_count}",
            f"build-root-depth:{0 if build_root == '.' else len(root.parts)}",
        )
        if low_signal_penalty:
            evidence += ("low-signal-directory-penalty",)
        return EcosystemCandidate(
            parser_name,
            build_root,
            score,
            round(confidence, 3),
            evidence,
        )
