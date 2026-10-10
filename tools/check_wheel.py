"""Verify an installed wheel from outside the source checkout."""

import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    wheel = parser.parse_args().wheel.resolve()
    with tempfile.TemporaryDirectory(prefix="synthermia-wheel-") as folder:
        environment = Path(folder) / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = str(environment / "bin" / "python")
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["XDG_CONFIG_HOME"] = str(Path(folder) / "config")
        commands = [
            [python, "-m", "pip", "install", "--no-deps", str(wheel)],
            [python, "-I", "-m", "synthermia", "--help"],
            [str(environment / "bin" / "synthermia"), "--help"],
            [python, "-I", "-c", "from synthermia import midi, packed; assert packed.native_packed_available(); print('Native backend:', packed.native_runtime_info())"],
        ]
        for command in commands:
            result = subprocess.run(command, cwd=folder, env=env, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
        print(f"Installed wheel checks passed: {wheel.name}")


if __name__ == "__main__":
    main()
