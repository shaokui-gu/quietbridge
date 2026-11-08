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
                                     "QuietBridgeAuthorizationPlugin")],
                check=False, capture_output=True, text=True,
                env=dict(os.environ, CLANG_MODULE_CACHE_PATH=directory))
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("PASS", run.stdout)

    def _build(self, directory):
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
        return host, server

    def _sign(self, binary, identifier):
        identity = os.environ.get(
            "AUTH_SIGNING_IDENTITY", "Developer ID Application: shaokui gu (62RNP54C47)")
        sign = subprocess.run([
            "/usr/bin/codesign", "--force", "--options", "runtime",
            "--timestamp=none", "--sign", identity,
            "--identifier", identifier, str(binary),
        ], check=False, capture_output=True, text=True)
        self.assertEqual(sign.returncode, 0, sign.stderr)

    def _run_against_server(self, directory, identifier, expectation):
        host, server = self._build(directory)
        self._sign(server, identifier)
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
                            "QuietBridgeAuthorizationPlugin"), expectation],
            check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result, running

    def test_signed_server_allows_one_authorization(self):
        """protocol 3 round trip: identity matches, daemon answers Allow."""
        with tempfile.TemporaryDirectory() as directory:
            result, running = self._run_against_server(
                directory, "com.quietbridge.helper", "allow")
            self.assertIn("PASS: trusted server allowed", result.stdout)
            self.assertIn("ALLOWED session=", running.stdout.readline())

    def test_server_with_foreign_identifier_is_denied(self):
        """Same team, wrong identifier: the identity comparison must reject it."""
        with tempfile.TemporaryDirectory() as directory:
            result, running = self._run_against_server(
                directory, "com.quietbridge.impostor", "deny")
            self.assertIn("PASS: bundle failed closed", result.stdout)
            self.assertIn("PEER CLOSED", running.stdout.readline())


if __name__ == "__main__":
    unittest.main()
