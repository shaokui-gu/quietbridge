import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "build" / "WorkBuddyCUAAuthorizationPlugin.bundle"


class PluginABITests(unittest.TestCase):
    def test_bundle_loads_and_fails_closed_without_server(self):
        self.assertTrue(BUNDLE.is_dir(), "run make before the plug-in ABI test")
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory) / "PluginHost"
            compile = subprocess.run([
                "/usr/bin/xcrun", "--sdk", "macosx", "clang",
                "-framework", "Security",
                "-o", str(host), str(ROOT / "tests" / "PluginHost.c"),
            ], check=False, capture_output=True, text=True)
            self.assertEqual(compile.returncode, 0, compile.stderr)
            run = subprocess.run(
                [str(host), str(BUNDLE / "Contents" / "MacOS" /
                                     "WorkBuddyCUAAuthorizationPlugin")],
                check=False, capture_output=True, text=True,
                env=dict(os.environ, CLANG_MODULE_CACHE_PATH=directory))
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("PASS", run.stdout)

    @unittest.skip("旧版模拟服务仅支持协议 1；不能作为协议 2 与真实授权宿主的验证")
    def test_signed_server_allows_one_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory) / "PluginHost"
            server = Path(directory) / "LoginAuthorizationTestServer"
            common = ["/usr/bin/xcrun", "--sdk", "macosx", "clang"]
            compile_host = subprocess.run(common + [
                "-framework", "Security", "-o", str(host),
                str(ROOT / "tests" / "PluginHost.c"),
            ], check=False, capture_output=True, text=True)
            self.assertEqual(compile_host.returncode, 0, compile_host.stderr)
            compile_server = subprocess.run(common + [
                "-o", str(server),
                str(ROOT / "tests" / "LoginAuthorizationTestServer.c"),
            ], check=False, capture_output=True, text=True)
            self.assertEqual(compile_server.returncode, 0, compile_server.stderr)

            identity = os.environ.get(
                "AUTH_SIGNING_IDENTITY", "Developer ID Application: shaokui gu (62RNP54C47)")
            sign = subprocess.run([
                "/usr/bin/codesign", "--force", "--options", "runtime",
                "--timestamp=none", "--sign", identity,
                "--identifier", "com.workbuddy.cua.helper", str(server),
            ], check=False, capture_output=True, text=True)
            self.assertEqual(sign.returncode, 0, sign.stderr)
            running = subprocess.Popen(
                [str(server)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            def cleanup_server():
                if running.poll() is None:
                    running.kill()
                running.wait(timeout=5)
                running.stdout.close()
                running.stderr.close()

            self.addCleanup(cleanup_server)
            self.assertEqual(running.stdout.readline().strip(), "READY")
            result = subprocess.run(
                [str(host), str(BUNDLE / "Contents" / "MacOS" /
                                "WorkBuddyCUAAuthorizationPlugin"), "allow"],
                check=False, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("trusted server allowed", result.stdout)


if __name__ == "__main__":
    unittest.main()
