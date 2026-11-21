# SPDX-License-Identifier: GPL-3.0-or-later
"""Structured COLLADA diagnostics."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    message: str
    context: str = ""

    def __str__(self):
        suffix = f" ({self.context})" if self.context else ""
        return f"{self.code}: {self.message}{suffix}"


class ColladaError(ValueError):
    """Fatal document or decoding error with stable diagnostic metadata."""

    def __init__(self, code, message, context=""):
        self.diagnostic = Diagnostic("ERROR", code, message, context)
        super().__init__(str(self.diagnostic))


class ColladaCancelled(RuntimeError):
    """Raised when a caller-provided cancellation token requests a stop."""


class DiagnosticSink:
    """Collect recoverable warnings and raise structured fatal errors."""

    def __init__(self):
        self.warnings = []

    def warn(self, code, message, context=""):
        diagnostic = Diagnostic("WARNING", code, message, context)
        self.warnings.append(diagnostic)
        return diagnostic

    @staticmethod
    def error(code, message, context=""):
        raise ColladaError(code, message, context)
