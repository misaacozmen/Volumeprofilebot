from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))


class MonkeyPatch:
    def __init__(self) -> None:
        self._undo: list[tuple[object, str, object]] = []

    def setattr(self, target: object, name: str, value: object) -> None:
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self) -> None:
        for target, name, value in reversed(self._undo):
            setattr(target, name, value)


def run_test(function: object) -> None:
    parameters = inspect.signature(function).parameters
    unsupported = set(parameters) - {"tmp_path", "monkeypatch"}
    if unsupported:
        raise RuntimeError(f"unsupported fixtures: {sorted(unsupported)}")
    monkeypatch = MonkeyPatch()
    try:
        with tempfile.TemporaryDirectory(prefix="otobt-test-") as directory:
            fixtures = {
                "tmp_path": Path(directory),
                "monkeypatch": monkeypatch,
            }
            function(**{name: fixtures[name] for name in parameters})
    finally:
        monkeypatch.undo()


def main() -> None:
    failures: list[str] = []
    count = 0
    for path in sorted(TESTS.glob("test_*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            failures.append(f"{path.name}: import spec unavailable")
            continue
        module = importlib.util.module_from_spec(spec)
        # Spawned child processes must be able to import top-level test helpers by
        # module name (not only through this runner's private module object).
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        for name, function in inspect.getmembers(module, inspect.isfunction):
            if not name.startswith("test_") or function.__module__ != module.__name__:
                continue
            count += 1
            try:
                run_test(function)
            except Exception as exc:  # noqa: BLE001 - test runner must collect all failures
                failures.append(f"{path.name}::{name}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"{count - len(failures)}/{count} tests passed")
        for failure in failures:
            print(f"FAIL {failure}")
        raise SystemExit(1)
    print(f"{count}/{count} tests passed")


if __name__ == "__main__":
    main()
