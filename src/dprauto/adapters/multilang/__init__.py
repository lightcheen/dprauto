"""Language-neutral project inspection adapters."""

from dprauto.adapters.multilang.detector import RepositoryLanguageDetector
from dprauto.adapters.multilang.jvm import JVMProjectParser
from dprauto.adapters.multilang.native import NativeProjectParser
from dprauto.adapters.multilang.parser import (
    MultiLanguageProjectParser,
    ParserRegistration,
    ProjectParserRegistry,
    PythonRepositoryScanParser,
)

__all__ = [
    "JVMProjectParser",
    "MultiLanguageProjectParser",
    "NativeProjectParser",
    "ParserRegistration",
    "ProjectParserRegistry",
    "PythonRepositoryScanParser",
    "RepositoryLanguageDetector",
]
