from __future__ import annotations

import ast
import re
import sys

from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = Path("bd_to_avp")
SWIFT_ROOT = Path("macos/BluRayToVisionPro")

DIRECT_PROCESS_CALLS = {
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "ffmpeg.run",
    "ffmpeg.run_async",
    "ffmpeg.probe",
    "os.execl",
    "os.execle",
    "os.execlp",
    "os.execlpe",
    "os.execv",
    "os.execve",
    "os.execvp",
    "os.execvpe",
    "os.popen",
    "os.posix_spawn",
    "os.posix_spawnp",
    "os.spawnl",
    "os.spawnle",
    "os.spawnlp",
    "os.spawnlpe",
    "os.spawnv",
    "os.spawnve",
    "os.spawnvp",
    "os.spawnvpe",
    "os.system",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.getoutput",
    "subprocess.getstatusoutput",
    "subprocess.Popen",
    "subprocess.run",
}
ALLOWED_DIRECT_PROCESS_CALLS = {
    (Path("bd_to_avp/process_runner.py"), "subprocess.Popen"),
}
GLOBAL_CLEANUP_CALLS = {"atexit.register"}
ALLOWED_PRESENTATION_CALLS = {
    (Path("bd_to_avp/__main__.py"), "main", "print"),
    (Path("bd_to_avp/modules/command.py"), "Spinner._update_spinner", "sys.stdout.write"),
    (Path("bd_to_avp/modules/command.py"), "Spinner.start", "print"),
    (Path("bd_to_avp/modules/command.py"), "Spinner.stop", "print"),
    (Path("bd_to_avp/modules/command.py"), "run_process_capture", "print"),
    (Path("bd_to_avp/presentation.py"), "cli_message", "print"),
    (Path("bd_to_avp/gui/util.py"), "OutputHandler.write", "sys.__stdout__.write"),
}
FORBIDDEN_PYTHON_SYMBOLS = {
    "BoundedEventSink",
    "CompositeEventSink",
    "EventBufferSnapshot",
    "PROCESS_NAMES_TO_KILL",
    "RotatingJSONLEventSink",
    "cleanup_process",
    "kill_child_processes",
    "kill_process_by_name",
    "kill_processes_by_name",
    "mounted_image",
    "redirect_stdout",
    "run_command",
    "terminate_process",
}
FORBIDDEN_SWIFT_SYMBOLS = {"diagnosticLog"}


@dataclass(frozen=True, order=True)
class Violation:
    path: Path
    line: int
    code: str
    detail: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.code}: {self.detail}"


class PythonMigrationVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.aliases: dict[str, str] = {}
        self.scope: list[str] = []
        self.violations: list[Violation] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        previous_aliases = self.aliases.copy()
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()
        self.aliases = previous_aliases

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        previous_aliases = self.aliases.copy()
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()
        self.aliases = previous_aliases

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.aliases[alias.asname or alias.name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is not None:
            for alias in node.names:
                self.aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._bind_alias(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._bind_alias(node.target, node.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        call_name = self._call_name(node.func)
        resolved_name = self._resolve_alias(call_name)
        if resolved_name in DIRECT_PROCESS_CALLS and (self.path, resolved_name) not in ALLOWED_DIRECT_PROCESS_CALLS:
            self.violations.append(
                Violation(
                    self.path,
                    node.lineno,
                    "direct-process-call",
                    f"route {resolved_name} through bd_to_avp.process_runner",
                )
            )
        if resolved_name in GLOBAL_CLEANUP_CALLS:
            self.violations.append(
                Violation(
                    self.path,
                    node.lineno,
                    "global-cleanup-hook",
                    f"remove process-wide cleanup hook {resolved_name}",
                )
            )
        if (
            self._is_direct_output_call(resolved_name, node)
            and (
                self.path,
                ".".join(self.scope),
                resolved_name,
            )
            not in ALLOWED_PRESENTATION_CALLS
        ):
            self.violations.append(
                Violation(
                    self.path,
                    node.lineno,
                    "non-presentation-output",
                    "conversion/runtime output must use structured activity or an intentional presentation sink",
                )
            )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and re.search(r"(?:^|[/\\])[^/\\]+\.log(?:\.\d+)?$", node.value):
            self.violations.append(
                Violation(
                    self.path,
                    node.lineno,
                    "ad-hoc-log-file",
                    f"remove orphan log path {node.value!r} or document an explicitly reviewed sink",
                )
            )
        self.generic_visit(node)

    @staticmethod
    def _call_name(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = PythonMigrationVisitor._call_name(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return ""

    def _resolve_alias(self, name: str) -> str:
        first, separator, remainder = name.partition(".")
        resolved = self.aliases.get(first, first)
        return f"{resolved}.{remainder}" if separator else resolved

    def _bind_alias(self, target: ast.expr, value: ast.expr) -> None:
        if not isinstance(target, ast.Name):
            return
        value_name = self._call_name(value)
        if not value_name:
            self.aliases.pop(target.id, None)
            return
        self.aliases[target.id] = self._resolve_alias(value_name)

    @staticmethod
    def _is_direct_output_call(name: str, node: ast.Call) -> bool:
        if name in {"print", "builtins.print"}:
            return True
        if re.fullmatch(
            r"sys\.(?:__(?:stdout|stderr)__|stdout|stderr)(?:\.buffer)?\.(?:write|writelines)",
            name,
        ):
            return True
        return (
            name == "os.write"
            and bool(node.args)
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in {1, 2}
        )


def collect_violations(root: Path = ROOT) -> list[Violation]:
    violations: list[Violation] = []
    python_root = root / PYTHON_ROOT
    for source_path in sorted(python_root.rglob("*.py")):
        relative_path = source_path.relative_to(root)
        source = source_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(relative_path))
        except SyntaxError as error:
            violations.append(Violation(relative_path, error.lineno or 1, "syntax-error", error.msg))
            continue
        visitor = PythonMigrationVisitor(relative_path)
        visitor.visit(tree)
        violations.extend(visitor.violations)
        violations.extend(_forbidden_symbol_violations(relative_path, source, FORBIDDEN_PYTHON_SYMBOLS))

    swift_root = root / SWIFT_ROOT
    for source_path in sorted(swift_root.rglob("*.swift")):
        relative_path = source_path.relative_to(root)
        source = source_path.read_text(encoding="utf-8")
        violations.extend(_forbidden_symbol_violations(relative_path, source, FORBIDDEN_SWIFT_SYMBOLS))

    return sorted(set(violations))


def _forbidden_symbol_violations(
    path: Path,
    source: str,
    symbols: set[str],
) -> list[Violation]:
    violations: list[Violation] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        for symbol in symbols:
            if re.search(rf"\b{re.escape(symbol)}\b", line):
                violations.append(Violation(path, line_number, "legacy-symbol", f"remove superseded symbol {symbol}"))
    return violations


def main() -> int:
    violations = collect_violations()
    if not violations:
        print("Observability migration validation passed.")
        return 0
    for violation in violations:
        print(violation.render(), file=sys.stderr)
    print(f"Found {len(violations)} observability migration violation(s).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
