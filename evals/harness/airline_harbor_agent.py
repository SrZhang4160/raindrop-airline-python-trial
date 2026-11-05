"""Harbor agent for SkyServe: the reference EvalsAgent plus a live upload of the current source.

Every trial uploads pyproject.toml, README.md and src/ from EVALS_SOURCE_ROOT (default: the
checkout holding this file) and installs them editable without dependencies, so edits to
src/airline_support/** (evals.toml live_paths) reach the very next trial and nothing is baked
into an image. The editable install is what gives the project's recorder its package version.

  HARBOR_TELEMETRY=0 PYTHONPATH=evals/harness:evals/bin/reference \
    uv tool run --python 3.12 --from harbor==0.23.0 harbor run -p <absolute task path> \
    -a airline_harbor_agent:AirlineAgent --ae EVALS_ADAPTER_COMMAND="python3 /app/adapter.py" \
    --ae EVALS_PASS_ENV=OPENAI_API_KEY -k <attempts>

See evals/harness/run.sh.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from harbor.environments.base import BaseEnvironment
from harbor_agent import EvalsAgent

HERE = Path(__file__).resolve().parent
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", ".env", ".env.*")


class AirlineAgent(EvalsAgent):
    @staticmethod
    def name() -> str:
        return "skyserve-airline-evals"

    async def setup(self, environment: BaseEnvironment) -> None:
        await super().setup(environment)
        root = Path(self._option("EVALS_SOURCE_ROOT") or HERE.parents[1]).resolve()
        with tempfile.TemporaryDirectory(prefix="airline-source-") as tmp:
            staged = Path(tmp) / "source"
            shutil.copytree(root / "src", staged / "src", ignore=SKIP)
            for name in ("pyproject.toml", "README.md"):
                shutil.copy2(root / name, staged / name)
            await environment.exec(command="rm -rf /app/source && mkdir -p /app", user="root")
            await environment.upload_dir(source_dir=staged, target_dir="/app/source")
        await environment.upload_file(source_path=HERE / "adapter.py", target_path="/app/adapter.py")
        installed = await environment.exec(
            command="pip install -q --no-deps --no-build-isolation -e /app/source", user="root")
        if installed.return_code != 0:
            raise RuntimeError(f"could not install the current source: {(installed.stderr or '')[-2000:]}")
