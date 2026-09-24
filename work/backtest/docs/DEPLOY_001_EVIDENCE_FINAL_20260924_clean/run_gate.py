import datetime
import json
from pathlib import Path
import subprocess
import sys

BASE = Path(__file__).resolve().parent
project = BASE / "checkout/work/backtest"
name, *args = sys.argv[1:]
out = BASE / "runs"
out.mkdir(exist_ok=True)
if (out / (name + ".json")).exists():
    raise SystemExit("Refusing to overwrite an existing run")
commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()
command = [str(BASE / "venv/Scripts/python.exe"), *args]
started = datetime.datetime.now(datetime.timezone.utc).isoformat()
with (out / (name + ".log")).open("wb") as stdout, (out / (name + ".stderr.log")).open("wb") as stderr:
    result = subprocess.run(command, cwd=project, stdout=stdout, stderr=stderr)
record = {"name": name, "command": command, "cwd": str(project), "tested_commit": commit,
          "started_at": started, "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "exit_code": result.returncode,
          "stdout": name + ".log", "stderr": name + ".stderr.log"}
(out / (name + ".json")).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"name": name, "exit_code": result.returncode}))
raise SystemExit(result.returncode)
