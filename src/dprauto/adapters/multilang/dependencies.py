"""Evidence-backed dependency contracts for supported build ecosystems."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Mapping

from dprauto.domain.models import ProjectProfile
from dprauto.inspection.scanner import ScannedProject


# Only packages in this table may cross the parser/build boundary.  Keys are
# build-system capabilities, not repository names, so the rules remain useful
# for repositories that were not part of the evaluation corpus.
NATIVE_CAPABILITY_PACKAGES: Mapping[str, tuple[str, ...]] = {
    "bzip2": ("libbz2-dev",),
    "cairo": ("libcairo2-dev",),
    "curl": ("libcurl4-openssl-dev",),
    "eigen3": ("libeigen3-dev",),
    "freetype": ("libfreetype6-dev",),
    "gif": ("libgif-dev",),
    "gflags": ("libgflags-dev",),
    "glog": ("libgoogle-glog-dev",),
    "harfbuzz": ("libharfbuzz-dev",),
    "icu": ("libicu-dev",),
    "jpeg": ("libjpeg-dev",),
    "leptonica": ("libleptonica-dev",),
    "libarchive": ("libarchive-dev",),
    "liblzma": ("liblzma-dev",),
    "lz4": ("liblz4-dev",),
    "nasm": ("nasm",),
    "numa": ("libnuma-dev",),
    "openssl": ("libssl-dev",),
    "pango": ("libpango1.0-dev",),
    "png": ("libpng-dev",),
    "popt": ("libpopt-dev",),
    "protobuf": ("libprotobuf-dev", "protobuf-compiler"),
    "snappy": ("libsnappy-dev",),
    "sqlite3": ("libsqlite3-dev",),
    "tiff": ("libtiff-dev",),
    "uring": ("liburing-dev",),
    "uuid": ("uuid-dev",),
    "x11": ("libx11-dev",),
    "xcb": ("libxcb1-dev",),
    "xcursor": ("libxcursor-dev",),
    "xi": ("libxi-dev",),
    "xinerama": ("libxinerama-dev",),
    "xrandr": ("libxrandr-dev",),
    "yasm": ("yasm",),
    "zlib": ("zlib1g-dev",),
    "zstd": ("libzstd-dev",),
}

SAFE_NATIVE_SYSTEM_PACKAGES = frozenset(
    package
    for packages in NATIVE_CAPABILITY_PACKAGES.values()
    for package in packages
) | frozenset({"python3"})

_CMAKE_PACKAGE_ALIASES = {
    "bzip2": "bzip2",
    "cairo": "cairo",
    "curl": "curl",
    "eigen3": "eigen3",
    "freetype": "freetype",
    "gif": "gif",
    "gflags": "gflags",
    "glog": "glog",
    "harfbuzz": "harfbuzz",
    "jpeg": "jpeg",
    "jpegturbo": "jpeg",
    "leptonica": "leptonica",
    "libarchive": "libarchive",
    "liblzma": "liblzma",
    "lz4": "lz4",
    "numa": "numa",
    "openssl": "openssl",
    "pango": "pango",
    "png": "png",
    "protobuf": "protobuf",
    "snappy": "snappy",
    "sqlite3": "sqlite3",
    "tiff": "tiff",
    "uring": "uring",
    "uuid": "uuid",
    "x11": "x11",
    "xcb": "xcb",
    "zlib": "zlib",
    "zstd": "zstd",
}

_PKG_CONFIG_ALIASES = {
    "cairo": "cairo",
    "freetype2": "freetype",
    "gflags": "gflags",
    "harfbuzz": "harfbuzz",
    "icu-i18n": "icu",
    "icu-uc": "icu",
    "lept": "leptonica",
    "leptonica": "leptonica",
    "libarchive": "libarchive",
    "libcurl": "curl",
    "liblz4": "lz4",
    "liblzma": "liblzma",
    "libpng": "png",
    "libssl": "openssl",
    "libtiff-4": "tiff",
    "liburing": "uring",
    "libzstd": "zstd",
    "openssl": "openssl",
    "pango": "pango",
    "pangocairo": "pango",
    "pangoft2": "pango",
    "popt": "popt",
    "protobuf": "protobuf",
    "snappy": "snappy",
    "sqlite3": "sqlite3",
    "xcb": "xcb",
    "xcursor": "xcursor",
    "xi": "xi",
    "xinerama": "xinerama",
    "xrandr": "xrandr",
    "zlib": "zlib",
}

_HEADER_ALIASES = {
    "xcb/xcb.h": "xcb",
    "x11/xlib.h": "x11",
    "x11/extensions/xinerama.h": "xinerama",
    "x11/extensions/xrandr.h": "xrandr",
    "x11/extensions/xinput2.h": "xi",
    "x11/xcursor/xcursor.h": "xcursor",
}

_TOOL_ALIASES = {"nasm": "nasm", "yasm": "yasm"}
_IGNORED_BUILD_DIRECTORIES = frozenset(
    {
        "examples",
        "example",
        "tests",
        "test",
        "third_party",
        "third-party",
        "vendor",
        "vendors",
    }
)
_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")


@dataclass(frozen=True, slots=True)
class DependencyEvidence:
    """One package decision tied to an exact repository construct."""

    package: str
    capability: str
    source: str
    detector: str
    evidence: str
    confidence: float

    def as_metadata(self) -> dict[str, object]:
        return {
            "package": self.package,
            "capability": self.capability,
            "source": self.source,
            "detector": self.detector,
            "evidence": self.evidence,
            "confidence": self.confidence,
        }


class NativeDependencyResolver:
    """Resolve explicit CMake/pkg-config/header/tool checks to safe apt packages."""

    max_evidence = 48
    max_depth = 4

    def resolve(
        self,
        scanned: ScannedProject,
        build_systems: tuple[str, ...],
    ) -> tuple[DependencyEvidence, ...]:
        evidence: list[DependencyEvidence] = []
        for path in self._build_inputs(scanned, build_systems):
            raw = scanned.read_text(path)
            if not raw:
                continue
            if self._is_cmake(path):
                evidence.extend(self._cmake_evidence(path, self._strip_cmake_comments(raw)))
            if self._is_autoconf(path):
                evidence.extend(
                    self._pkg_config_evidence(path, raw, "autoconf.pkg_check_modules")
                )
                evidence.extend(self._autoconf_tool_evidence(path, raw))
                if re.search(
                    r"\bAM_PATH_PYTHON\b|\bAC_PATH_PROG\s*\([^)]*\bPYTHON\b",
                    raw,
                    re.I,
                ):
                    evidence.append(
                        DependencyEvidence(
                            "python3",
                            "python3",
                            path,
                            "autoconf.program_check",
                            "Python interpreter check",
                            0.95,
                        )
                    )
            if PurePosixPath(path).name.casefold() in {"configure", "makefile", "gnumakefile"}:
                evidence.extend(self._script_tool_evidence(path, raw))
        return self._deduplicate(evidence)[: self.max_evidence]

    def packages(self, evidence: tuple[DependencyEvidence, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.package for item in evidence))

    def _build_inputs(
        self,
        scanned: ScannedProject,
        build_systems: tuple[str, ...],
    ) -> tuple[str, ...]:
        selected: list[str] = []
        for path in scanned.files:
            pure = PurePosixPath(path)
            if len(pure.parts) > self.max_depth:
                continue
            if any(part.casefold() in _IGNORED_BUILD_DIRECTORIES for part in pure.parts[:-1]):
                continue
            name = pure.name.casefold()
            if "cmake" in build_systems and (name == "cmakelists.txt" or name.endswith(".cmake")):
                selected.append(path)
            elif "autotools" in build_systems and (
                name in {"configure", "configure.ac", "configure.in"} or name.endswith(".m4")
            ):
                selected.append(path)
            elif "make" in build_systems and name in {"makefile", "gnumakefile"}:
                selected.append(path)
        return tuple(selected)

    @staticmethod
    def _is_cmake(path: str) -> bool:
        name = PurePosixPath(path).name.casefold()
        return name == "cmakelists.txt" or name.endswith(".cmake")

    @staticmethod
    def _is_autoconf(path: str) -> bool:
        name = PurePosixPath(path).name.casefold()
        return name in {"configure.ac", "configure.in"} or name.endswith(".m4")

    @staticmethod
    def _strip_cmake_comments(text: str) -> str:
        text = re.sub(r"#\[(?:=*)\[.*?\](?:=*)\]", "", text, flags=re.S)
        return re.sub(r"(?m)#.*$", "", text)

    def _cmake_evidence(self, path: str, text: str) -> list[DependencyEvidence]:
        selected: list[DependencyEvidence] = []
        for match in re.finditer(
            r"(?is)\bfind_package\s*\(\s*([A-Za-z0-9_+.-]+)([^)]*)\)",
            text,
        ):
            token = match.group(1)
            capability = _CMAKE_PACKAGE_ALIASES.get(token.casefold())
            if capability:
                suffix = match.group(2).strip()
                rendered = f"{token} {suffix}".strip()
                required = bool(re.search(r"(?i)\bREQUIRED\b", suffix))
                selected.extend(
                    self._records(
                        capability,
                        path,
                        "cmake.find_package",
                        f"find_package({rendered})",
                        0.99 if required else 0.9,
                    )
                )
        selected.extend(self._pkg_config_evidence(path, text, "cmake.pkg_check_modules"))
        for match in re.finditer(
            r"(?is)\b(?:check_include_file|check_include_files|check_include_file_cxx)"
            r"\s*\(\s*[\"']?([^\"'\s)]+)",
            text,
        ):
            header = match.group(1).casefold()
            capability = _HEADER_ALIASES.get(header)
            if capability:
                selected.extend(
                    self._records(
                        capability,
                        path,
                        "cmake.header_check",
                        f"check_include_file({match.group(1)})",
                        0.92,
                    )
                )
        for match in re.finditer(r"(?is)\bfind_program\s*\(([^)]*)\)", text):
            selected.extend(
                self._tool_records(path, match.group(1), "cmake.find_program", 0.95)
            )
        return selected

    def _pkg_config_evidence(
        self,
        path: str,
        text: str,
        detector: str,
    ) -> list[DependencyEvidence]:
        selected: list[DependencyEvidence] = []
        for match in re.finditer(r"(?is)\bPKG_CHECK_MODULES\s*\(([^)]*)\)", text):
            body = match.group(1)
            # The first argument is the result variable. Search the remaining
            # declaration for known module names and ignore version operators.
            parts = re.split(r"\s*,\s*", body, maxsplit=1)
            modules = parts[1] if len(parts) == 2 else " ".join(body.split()[1:])
            normalized = re.sub(r"[\[\]'\"]", " ", modules).casefold()
            for token, capability in _PKG_CONFIG_ALIASES.items():
                if re.search(rf"(?<![a-z0-9_.+-]){re.escape(token)}(?![a-z0-9_.+-])", normalized):
                    selected.extend(
                        self._records(
                            capability,
                            path,
                            detector,
                            f"PKG_CHECK_MODULES({token})",
                            0.96,
                        )
                    )
        return selected

    def _autoconf_tool_evidence(self, path: str, text: str) -> list[DependencyEvidence]:
        selected: list[DependencyEvidence] = []
        for match in re.finditer(
            r"(?is)\b(?:AC_PATH_PROG|AC_CHECK_PROG|AC_CHECK_PROGS)\s*\(([^)]*)\)", text
        ):
            selected.extend(
                self._tool_records(path, match.group(1), "autoconf.program_check", 0.95)
            )
        return selected

    def _script_tool_evidence(self, path: str, text: str) -> list[DependencyEvidence]:
        selected: list[DependencyEvidence] = []
        for tool, capability in _TOOL_ALIASES.items():
            missing = re.search(
                rf"(?i)\b{tool}\b[^\n]{{0,40}}\b(?:not found|too old|required)\b",
                text,
            )
            assignment = re.search(
                rf"(?im)^\s*[A-Za-z0-9_]*{tool}[A-Za-z0-9_]*"
                rf"\s*[:?]?=\s*[\"']?{tool}\b",
                text,
            )
            if missing or assignment:
                selected.extend(
                    self._records(
                        capability,
                        path,
                        "build-script.program_check",
                        (missing or assignment).group(0).strip(),
                        0.92,
                    )
                )
        return selected

    def _tool_records(
        self,
        path: str,
        body: str,
        detector: str,
        confidence: float,
    ) -> list[DependencyEvidence]:
        selected: list[DependencyEvidence] = []
        for tool, capability in _TOOL_ALIASES.items():
            if re.search(rf"(?i)(?<![A-Za-z0-9_.+-]){tool}(?![A-Za-z0-9_.+-])", body):
                selected.extend(
                    self._records(capability, path, detector, f"program:{tool}", confidence)
                )
        return selected

    @staticmethod
    def _records(
        capability: str,
        path: str,
        detector: str,
        evidence: str,
        confidence: float,
    ) -> list[DependencyEvidence]:
        return [
            DependencyEvidence(package, capability, path, detector, evidence[:240], confidence)
            for package in NATIVE_CAPABILITY_PACKAGES[capability]
        ]

    @staticmethod
    def _deduplicate(evidence: list[DependencyEvidence]) -> tuple[DependencyEvidence, ...]:
        selected: dict[str, DependencyEvidence] = {}
        for item in evidence:
            current = selected.get(item.package)
            if current is None or item.confidence > current.confidence:
                selected[item.package] = item
        return tuple(selected.values())


def validated_native_system_packages(metadata: Mapping[str, object]) -> tuple[str, ...]:
    """Return only high-confidence, allow-listed packages with structured evidence."""

    raw = metadata.get("system_dependency_evidence", ())
    if not isinstance(raw, (tuple, list)):
        return ()
    selected: list[str] = []
    for item in raw[:48]:
        if not isinstance(item, Mapping):
            continue
        package = item.get("package")
        confidence = item.get("confidence")
        if (
            isinstance(package, str)
            and _PACKAGE_NAME.fullmatch(package)
            and package in SAFE_NATIVE_SYSTEM_PACKAGES
            and isinstance(confidence, (int, float))
            and confidence >= 0.85
            and isinstance(item.get("source"), str)
            and bool(item.get("source"))
        ):
            selected.append(package)
    return tuple(dict.fromkeys(selected))


def dependency_contract(
    profile: ProjectProfile,
    *,
    parser_name: str,
    build_root: str,
) -> dict[str, object]:
    """Normalize parser facts without inventing dependencies absent from evidence."""

    version_sources = {
        "python": profile.metadata.get("python_version_evidence", ""),
        "java": profile.metadata.get("java_version_evidence", ""),
        "java_target": profile.metadata.get("java_target_version_evidence", ""),
        "cmake": profile.metadata.get("cmake_version_evidence", ""),
    }
    constraints = tuple(
        {
            "name": name,
            "specifier": value,
            "source": str(version_sources.get(name, "")),
            "confidence": 0.95 if version_sources.get(name) else 0.7,
        }
        for name, value in profile.runtime_constraints.items()
    )
    tool_versions = profile.metadata.get("build_tool_constraints", {})
    tool_sources = profile.metadata.get("build_tool_version_evidence", {})
    if not isinstance(tool_versions, Mapping):
        tool_versions = {}
    build_tool_constraints = tuple(
        {
            "name": name,
            "specifier": value,
            "source": str(tool_sources.get(name, ""))
            if isinstance(tool_sources, Mapping)
            else "",
            "confidence": 0.99,
        }
        for name, value in tool_versions.items()
        if isinstance(name, str) and isinstance(value, str)
    )
    manifests = tuple(
        {"path": path, "kind": "dependency", "confidence": 0.95}
        for path in profile.dependency_files
    ) + tuple(
        {"path": path, "kind": "build", "confidence": 0.99}
        for path in profile.build_files
        if path not in profile.dependency_files
    )

    runtime_names = {
        value
        for value in profile.metadata.get("runtime_dependency_names", ())
        if isinstance(value, str)
    }
    all_names = {
        value
        for value in profile.metadata.get("dependency_names", ())
        if isinstance(value, str)
    }
    language_dependencies = tuple(
        {
            "name": name,
            "scope": "runtime" if name in runtime_names else "declared",
            "sources": profile.dependency_files,
            "detector": "language-manifest",
            "confidence": 0.9,
        }
        for name in sorted(all_names | runtime_names)
    )

    system_dependencies: list[dict[str, object]] = []
    raw_system = profile.metadata.get("system_dependency_evidence", ())
    if isinstance(raw_system, (tuple, list)):
        system_dependencies.extend(dict(item) for item in raw_system if isinstance(item, Mapping))
    for hint in profile.metadata.get("system_dependency_hints", ()):
        if isinstance(hint, str):
            system_dependencies.append(
                {
                    "package": "",
                    "capability": hint,
                    "source": profile.dependency_files[0] if profile.dependency_files else "",
                    "sources": profile.dependency_files,
                    "detector": "python-manifest",
                    "evidence": hint,
                    "confidence": 0.9,
                }
            )

    tests: list[dict[str, object]] = []
    for key, kind in (
        ("test_dependency_extras", "extra"),
        ("test_dependency_manager_groups", "manager-group"),
        ("test_required_executables", "executable"),
    ):
        for value in profile.metadata.get(key, ()):
            if isinstance(value, str):
                tests.append(
                    {
                        "name": value,
                        "kind": kind,
                        "sources": tuple(
                            dict.fromkeys((*profile.dependency_files, *profile.ci_files))
                        ),
                        "confidence": 0.9,
                    }
                )

    return {
        "schema_version": 1,
        "parser": parser_name,
        "build_root": build_root,
        "package_managers": profile.package_managers,
        "manifests": manifests,
        "runtime_constraints": constraints,
        "build_tool_constraints": build_tool_constraints,
        "language_dependencies": language_dependencies,
        "system_dependencies": tuple(system_dependencies),
        "test_dependencies": tuple(tests),
    }
