"""Execute deployment shell routing with fake Python/sudo in a temporary tree."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
GIT = shutil.which("git")
BASH = shutil.which("bash")
if not BASH and GIT:
    candidate = Path(GIT).resolve().parent.parent / "bin" / "bash.exe"
    if candidate.is_file():
        BASH = str(candidate)


@unittest.skipUnless(BASH, "Bash is needed to validate Linux deployment routing")
class PhoneDeployTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="caremate phone deploy ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "deploy").mkdir()
        (self.root / "code").mkdir()
        for name in ("start.sh", "install_services.sh"):
            shutil.copyfile(ROOT / "deploy" / name, self.root / "deploy" / name)
        python = self.root / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n', encoding="utf-8", newline="\n")
        python.chmod(0o755)
        self.env = dict(os.environ)
        self.env["SUDO_USER"] = "mock_operator"
        for key in ("PHONE_URL", "PHONE_ARGS", "PHONE_STREAM_URL", "PHONE_USERNAME", "PHONE_PASSWORD"):
            self.env.pop(key, None)

    def run_script(self, name, arg, configuration):
        (self.root / "deploy" / "x5.env").write_text(configuration, encoding="utf-8", newline="\n")
        return subprocess.run([BASH, str(self.root / "deploy" / name), arg],
                              env=self.env, capture_output=True, encoding="utf-8", timeout=15)

    def test_phone_routes_pass_full_mjpeg_address_and_never_an_arm_or_master_argument(self):
        configuration = 'PHONE_STREAM_URL="http://127.0.0.1:8081/camera/stream"\nPHONE_USERNAME="test_user"\nPHONE_PASSWORD="test_secret"\n'
        for role, module in (("phone", "vision.fall_bridge"), ("phone-demo", "vision.integrated_demo")):
            with self.subTest(role=role):
                result = self.run_script("start.sh", role, configuration)
                self.assertEqual(result.returncode, 0, result.stderr)
                args = result.stdout.splitlines()
                self.assertEqual(args[:2], ["-m", module])
                self.assertIn("http://127.0.0.1:8081/camera/stream", args)
                for forbidden in ("--arm", "--master", "--server", "--thresh", "test_secret"):
                    self.assertNotIn(forbidden, args)
                self.assertEqual("--headless" in args, role == "phone-demo")

    def test_old_or_missing_phone_configuration_fails_before_any_python(self):
        for configuration in ('PHONE_URL="http://invalid/"\nPHONE_ARGS="--thresh 38 --arm 1"\n',
                              'PHONE_STREAM_URL="http://invalid/"\nPHONE_ARGS="--arm 1"\n', ''):
            with self.subTest(configuration=configuration):
                result = self.run_script("start.sh", "phone", configuration)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("-m", result.stdout)

    def test_service_installer_selects_only_phone_and_rejects_legacy_before_install(self):
        tools = self.root / "fake-bin"
        tools.mkdir()
        sudo = tools / "sudo"
        sudo.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$TASK_COMMANDS"\nif [ "$1" = tee ]; then cat >> "$TASK_UNIT"; fi\n', encoding="utf-8", newline="\n")
        sudo.chmod(0o755)
        self.env.update(PATH=str(tools) + os.pathsep + self.env.get("PATH", ""),
                        TASK_COMMANDS=str(self.root / "commands.txt"), TASK_UNIT=str(self.root / "unit.txt"))
        result = self.run_script("install_services.sh", "a", 'PHONE_STREAM_URL="http://127.0.0.1:8081/"\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = (self.root / "commands.txt").read_text(encoding="utf-8")
        self.assertIn("caremate-phone.service", commands)
        self.assertNotIn("caremate-vision.service", commands)
        (self.root / "commands.txt").unlink()
        result = self.run_script("install_services.sh", "a", 'PHONE_URL="http://invalid/"\n')
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / "commands.txt").exists())


if __name__ == "__main__":
    unittest.main()
