"""Language-neutral project inspection adapters."""

from dprauto.adapters.multilang.detector import RepositoryLanguageDetector
from dprauto.adapters.multilang.dependencies import (
    DependencyEvidence,
    NativeDependencyResolver,
    SAFE_NATIVE_SYSTEM_PACKAGES,
    dependency_contract,
    validated_native_system_packages,
)
from dprauto.adapters.multilang.ecosystem import BuildEcosystemDetector, EcosystemCandidate
from dprauto.adapters.multilang.jvm import JVMProjectParser
from dprauto.adapters.multilang.native import NativeProjectParser
from dprauto.adapters.multilang.parser import (
    MultiLanguageProjectParser,
    ParserRegistration,
    ProjectParserRegistry,
    PythonScannedProjectParser,
)

__all__ = [
    "BuildEcosystemDetector",
    "DependencyEvidence",
    "EcosystemCandidate",
    "JVMProjectParser",
    "MultiLanguageProjectParser",
    "NativeDependencyResolver",
    "NativeProjectParser",
    "ParserRegistration",
    "ProjectParserRegistry",
    "PythonScannedProjectParser",
    "RepositoryLanguageDetector",
    "SAFE_NATIVE_SYSTEM_PACKAGES",
    "dependency_contract",
    "validated_native_system_packages",
]
