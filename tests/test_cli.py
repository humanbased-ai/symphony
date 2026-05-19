import asyncio
import urllib.request
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from unittest.mock import patch

from symphony.cli import (
    RuntimeWorkflowReloader,
    StartupError,
    _check_claude_login,
    _check_gh_auth,
    _check_github_token_scopes,
    _check_linear_key_valid,
    _detect_github_from_remote,
    create_runtime,
    create_status_api,
    create_status_http_server,
    doctor_checks,
    load_startup_context,
    main,
    print_setup_checks,
    run_once,
    setup_environment_checks,
)
from symphony import __version__
from symphony.orchestrator import OrchestratorState


class CLITests(unittest.TestCase):
    def test_help_exits_successfully_without_workflow_file(self):
        with redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["--help"])

        self.assertEqual(0, raised.exception.code)

    def test_version_exits_successfully_without_workflow_file(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                main(["--version"])

        self.assertEqual(0, raised.exception.code)
        self.assertIn(__version__, stdout.getvalue())

    def test_load_startup_context_validates_workflow_and_resolves_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workflow_path = root / "WORKFLOW.md"
            workflow_path.write_text(
                """---
tracker:
  kind: linear
  api_key: $LINEAR_KEY
  project_slug: symphony-ai-agent-orchestration
codex:
  command: codex app-server
---
Work on {{ issue.identifier }}.
""",
                encoding="utf-8",
            )

            context = load_startup_context(
                workflow_path,
                logs_root="runtime-logs",
                port=7337,
                environ={"LINEAR_KEY": "lin_secret"},
            )

            self.assertEqual(workflow_path.resolve(), context.workflow_path)
            self.assertEqual((root / "runtime-logs").resolve(), context.logs_root)
            self.assertEqual(7337, context.port)
            self.assertEqual("Work on {{ issue.identifier }}.", context.workflow.prompt_template)
            self.assertEqual("symphony-ai-agent-orchestration", context.config.tracker.project_slug)

    def test_load_startup_context_rejects_missing_linear_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "WORKFLOW.md"
            workflow_path.write_text(
                """---
tracker:
  kind: linear
  api_key: $LINEAR_KEY
  project_slug: symphony-ai-agent-orchestration
---
Body
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(StartupError, "missing_tracker_api_key"):
                load_startup_context(
                    workflow_path,
                    logs_root="log",
                    port=7337,
                    environ={"XDG_CONFIG_HOME": temp_dir},
                )

    def test_check_mode_reports_valid_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "WORKFLOW.md"
            workflow_path.write_text(
                """---
tracker:
  kind: linear
  api_key: literal-token
  project_slug: symphony-ai-agent-orchestration
---
Body
""",
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()):
                result = main([str(workflow_path), "--check", "--log-level", "WARNING"])

            self.assertEqual(0, result)

    def test_run_subcommand_preserves_check_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "WORKFLOW.md"
            workflow_path.write_text(
                """---
tracker:
  kind: linear
  api_key: literal-token
  project_slug: symphony-ai-agent-orchestration
---
Body
""",
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()):
                result = main(["run", str(workflow_path), "--check", "--log-level", "WARNING"])

            self.assertEqual(0, result)

    def test_onboard_skips_existing_valid_workflow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "WORKFLOW.md"
            workflow_path.write_text(
                """---
tracker:
  kind: linear
  api_key: literal-token
  project_slug: symphony-ai-agent-orchestration
workspace:
  root: workspaces
agent:
  runner: codex
codex:
  command: python --version
---
Body
""",
                encoding="utf-8",
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                with patch("symphony.cli._check_linear_key_valid", return_value=(True, "valid (mocked)")):
                    result = main(
                        [
                            "onboard",
                            "--mode",
                            "automated",
                            "--workflow-path",
                            str(workflow_path),
                            "--runner",
                            "codex",
                        ]
                    )

            self.assertEqual(0, result)
            self.assertIn("Onboarding already complete", stdout.getvalue())
            self.assertIn("Skipped init", stdout.getvalue())

    def test_init_subcommand_writes_workflow_and_local_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workflow_path = root / "WORKFLOW.md"
            credentials_path = root / "credentials.json"

            with redirect_stdout(StringIO()):
                result = main(
                    [
                        "init",
                        "--yes",
                        "--project-slug",
                        "symphony-ai-agent-orchestration",
                        "--workflow-path",
                        str(workflow_path),
                        "--credentials-path",
                        str(credentials_path),
                        "--linear-api-key",
                        "lin_secret",
                        "--runner",
                        "codex",
                        "--codex-command",
                        "python --version",
                    ]
                )

            self.assertEqual(0, result)
            self.assertIn("project_slug: symphony-ai-agent-orchestration", workflow_path.read_text(encoding="utf-8"))
            self.assertIn("lin_secret", credentials_path.read_text(encoding="utf-8"))

    def test_init_yes_requires_project_slug_without_prompting(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["init", "--yes"])

        self.assertEqual(2, raised.exception.code)

    def test_init_automated_reports_all_missing_inputs_without_prompting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "WORKFLOW.md"
            # Use an isolated credentials path so the test is not affected by any
            # real credentials on the machine.
            credentials_path = str(Path(temp_dir) / "credentials.json")
            stderr = StringIO()

            with redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    main([
                        "init", "--mode", "automated",
                        "--workflow-path", str(workflow_path),
                        "--credentials-path", credentials_path,
                    ])

            self.assertEqual(2, raised.exception.code)
            message = stderr.getvalue()
            self.assertIn("automated setup failed", message)
            self.assertIn("--project-slug", message)
            self.assertIn("linear auth", message)
            self.assertFalse(workflow_path.exists())

    def test_setup_environment_checks_reports_configured_auth_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_path = Path(temp_dir) / "credentials.json"
            args = type(
                "Args",
                (),
                {
                    "workflow_path": str(Path(temp_dir) / "WORKFLOW.md"),
                    "linear_api_key": "lin_secret",
                    "github_token": None,
                    "credentials_path": str(credentials_path),
                    "runner": "codex",
                    "codex_command": "python --version",
                    "github_org": None,
                    "github_repo": None,
                },
            )()

            checks = setup_environment_checks(args, environ={})

            self.assertIn((True, "linear auth", "--linear-api-key"), checks)
            self.assertTrue(any(label == "codex command" and ok for ok, label, _ in checks))

    def test_doctor_checks_validate_command_and_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workflow_path = root / "WORKFLOW.md"
            workflow_path.write_text(
                """---
tracker:
  kind: linear
  api_key: literal-token
  project_slug: symphony-ai-agent-orchestration
workspace:
  root: workspaces
codex:
  command: python --version
---
Body
""",
                encoding="utf-8",
            )

            import socket as _socket
            with _socket.socket() as _s:
                _s.bind(("127.0.0.1", 0))
                free_port = _s.getsockname()[1]
            with patch("symphony.cli._check_linear_key_valid", return_value=(True, "valid (mocked)")):
                checks = doctor_checks(workflow_path, logs_root="log", port=free_port)

            self.assertTrue(all(ok for ok, _, _ in checks))

    def test_status_api_uses_runtime_snapshot_and_refresh_callback(self):
        class FakeRuntime:
            def __init__(self):
                self.config = None
                self.state = OrchestratorState(
                    poll_interval_ms=30_000,
                    max_concurrent_agents=1,
                    active_states=("Todo",),
                    terminal_states=("Done",),
                )
                self.ticks = 0

            def snapshot(self):
                return self.state

            async def run_tick(self):
                self.ticks += 1
                return {"queued": True, "operations": ["poll"]}

        runtime = FakeRuntime()
        api = create_status_api(runtime)

        health = api.handle_request("GET", "/api/v1/health")
        refresh = asyncio.run(api.async_handle_request("POST", "/api/v1/refresh"))

        self.assertEqual(200, health.status_code)
        self.assertEqual(202, refresh.status_code)
        self.assertEqual(1, runtime.ticks)

    def test_status_http_server_serves_health_endpoint(self):
        async def exercise():
            state = OrchestratorState(
                poll_interval_ms=30_000,
                max_concurrent_agents=1,
                active_states=("Todo",),
                terminal_states=("Done",),
            )
            api = create_status_api(type("Runtime", (), {"snapshot": lambda _self: state, "run_tick": lambda _self: None})())
            server = create_status_http_server(api, 0, loop=asyncio.get_running_loop())
            task = asyncio.create_task(asyncio.to_thread(server.serve_forever, 0.05))
            try:
                port = server.server_address[1]

                def fetch():
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=2) as response:
                        return response.status, response.read()

                return await asyncio.to_thread(fetch)
            finally:
                server.shutdown()
                server.server_close()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        status, body = asyncio.run(exercise())

        self.assertEqual(200, status)
        self.assertIn(b'"status":"ok"', body)

    def test_runtime_workflow_reloader_applies_changed_config_and_prompt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workflow_path = root / "WORKFLOW.md"
            workflow_path.write_text(
                """---
tracker:
  kind: linear
  api_key: literal-token
  project_slug: symphony-ai-agent-orchestration
polling:
  interval_ms: 5000
workspace:
  root: workspaces-a
---
First prompt {{ issue.identifier }}.
""",
                encoding="utf-8",
            )
            context = load_startup_context(workflow_path, logs_root="log", port=7337)
            runtime = create_runtime(context)
            reloader = RuntimeWorkflowReloader.from_context(runtime, context)

            workflow_path.write_text(
                """---
tracker:
  kind: linear
  api_key: literal-token
  project_slug: symphony-ai-agent-orchestration
  active_states: [Reviewing]
polling:
  interval_ms: 1234
workspace:
  root: workspaces-b
codex:
  command: codex app-server --profile changed
---
Changed prompt {{ issue.identifier }}.
""",
                encoding="utf-8",
            )

            changed = reloader.reload_now()

            self.assertTrue(changed)
            self.assertEqual("Changed prompt {{ issue.identifier }}.", runtime.prompt_template)
            self.assertEqual(1234, runtime.state.poll_interval_ms)
            self.assertEqual(("Reviewing",), runtime.state.active_states)
            self.assertEqual((root / "workspaces-b").resolve(), runtime.config.workspace.root)

    def test_runtime_workflow_reloader_keeps_last_good_config_on_invalid_reload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "WORKFLOW.md"
            workflow_path.write_text(
                """---
tracker:
  kind: linear
  api_key: literal-token
  project_slug: symphony-ai-agent-orchestration
polling:
  interval_ms: 5000
---
First prompt.
""",
                encoding="utf-8",
            )
            context = load_startup_context(workflow_path, logs_root="log", port=7337)
            runtime = create_runtime(context)
            reloader = RuntimeWorkflowReloader.from_context(
                runtime,
                context,
                environ={"XDG_CONFIG_HOME": temp_dir},
            )

            workflow_path.write_text(
                """---
tracker:
  kind: linear
  api_key: $MISSING_LINEAR_KEY
  project_slug: symphony-ai-agent-orchestration
polling:
  interval_ms: 1234
---
Invalid prompt.
""",
                encoding="utf-8",
            )

            changed = reloader.reload_now()

            self.assertFalse(changed)
            self.assertEqual("First prompt.", runtime.prompt_template)
            self.assertEqual(5000, runtime.state.poll_interval_ms)
            self.assertIsNotNone(reloader.reloader.last_error)

    def test_run_once_delegates_to_runtime_tick(self):
        class FakeRuntime:
            def __init__(self):
                self.ticks = 0

            async def run_tick(self):
                self.ticks += 1
                return "tick-result"

        runtime = FakeRuntime()

        result = asyncio.run(run_once(runtime))

        self.assertEqual("tick-result", result)
        self.assertEqual(1, runtime.ticks)


class DetectGithubFromRemoteTests(unittest.TestCase):
    def _run(self, stdout: str, returncode: int = 0) -> tuple[str | None, str | None]:
        import subprocess
        fake = type("R", (), {"returncode": returncode, "stdout": stdout})()
        with patch("subprocess.run", return_value=fake):
            return _detect_github_from_remote()

    def test_parses_ssh_url(self):
        org, repo = self._run("git@github.com:codatta/symphony.git\n")
        self.assertEqual("codatta", org)
        self.assertEqual("symphony", repo)

    def test_parses_https_url(self):
        org, repo = self._run("https://github.com/codatta/symphony.git\n")
        self.assertEqual("codatta", org)
        self.assertEqual("symphony", repo)

    def test_parses_https_url_without_git_suffix(self):
        org, repo = self._run("https://github.com/acme/my-repo\n")
        self.assertEqual("acme", org)
        self.assertEqual("my-repo", repo)

    def test_returns_none_on_non_github_remote(self):
        org, repo = self._run("https://gitlab.com/acme/repo.git\n")
        self.assertIsNone(org)
        self.assertIsNone(repo)

    def test_returns_none_on_nonzero_exit(self):
        org, repo = self._run("", returncode=128)
        self.assertIsNone(org)
        self.assertIsNone(repo)


class CheckGhAuthTests(unittest.TestCase):
    def _run(self, stdout: str, stderr: str = "", returncode: int = 0) -> tuple[bool, str]:
        fake = type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()
        with patch("subprocess.run", return_value=fake):
            return _check_gh_auth()

    def test_returns_authenticated_with_account_name(self):
        ok, detail = self._run(
            stdout="",
            stderr="github.com\n  ✓ Logged in to github.com account codatta (keyring)\n",
        )
        self.assertTrue(ok)
        self.assertIn("codatta", detail)

    def test_returns_authenticated_without_parseable_account(self):
        ok, detail = self._run(stdout="ok", stderr="")
        self.assertTrue(ok)
        self.assertIn("authenticated", detail)

    def test_returns_false_on_nonzero_exit(self):
        ok, detail = self._run(stdout="", returncode=1)
        self.assertFalse(ok)
        self.assertIn("gh auth login", detail)


class PrintSetupChecksTests(unittest.TestCase):
    def test_uses_check_and_cross_markers(self):
        checks = [(True, "linear auth", "LINEAR_API_KEY"), (False, "gh command", "not found")]
        out = StringIO()
        with redirect_stdout(out):
            print_setup_checks("Environment scan", checks)
        rendered = out.getvalue()
        self.assertIn("✓", rendered)
        self.assertIn("✗", rendered)
        self.assertIn("linear auth", rendered)
        self.assertIn("gh command", rendered)

    def test_title_is_present(self):
        out = StringIO()
        with redirect_stdout(out):
            print_setup_checks("My Title", [])
        self.assertIn("My Title", out.getvalue())


class CheckClaudeLoginTests(unittest.TestCase):
    def test_returns_true_when_config_dir_exists_and_non_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / ".config" / "claude"
            config_dir.mkdir(parents=True)
            (config_dir / "settings.json").write_text("{}", encoding="utf-8")
            with patch("symphony.cli.Path.home", return_value=Path(tmp)):
                ok, detail = _check_claude_login()
        self.assertTrue(ok)
        self.assertIn("config dir found", detail)

    def test_returns_true_when_dot_claude_json_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".claude.json").write_text("{}", encoding="utf-8")
            with patch("symphony.cli.Path.home", return_value=Path(tmp)):
                ok, detail = _check_claude_login()
        self.assertTrue(ok)
        self.assertIn("~/.claude.json", detail)

    def test_returns_false_when_no_config_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("symphony.cli.Path.home", return_value=Path(tmp)):
                ok, detail = _check_claude_login()
        self.assertFalse(ok)
        self.assertIn("claude login", detail)


class CheckLinearKeyValidTests(unittest.TestCase):
    def _make_response(self, body: dict, status: int = 200):
        import json as _json
        import io
        raw = _json.dumps(body).encode()
        resp = type("R", (), {
            "read": lambda self: raw,
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: False,
        })()
        return resp

    def test_returns_true_with_viewer_name(self):
        resp = self._make_response({"data": {"viewer": {"id": "u1", "name": "Alice"}}})
        with patch("urllib.request.urlopen", return_value=resp):
            ok, detail = _check_linear_key_valid("lin_api_key")
        self.assertTrue(ok)
        self.assertIn("Alice", detail)

    def test_returns_false_on_401(self):
        import urllib.error
        exc = urllib.error.HTTPError(url="", code=401, msg="Unauthorized", hdrs=None, fp=None)
        with patch("urllib.request.urlopen", side_effect=exc):
            ok, detail = _check_linear_key_valid("bad_key")
        self.assertFalse(ok)
        self.assertIn("invalid key", detail)

    def test_returns_true_on_network_error(self):
        import urllib.error
        exc = urllib.error.URLError("connection refused")
        with patch("urllib.request.urlopen", side_effect=exc):
            ok, detail = _check_linear_key_valid("lin_key")
        self.assertTrue(ok)
        self.assertIn("network check skipped", detail)

    def test_returns_true_on_non_auth_http_error(self):
        import urllib.error
        exc = urllib.error.HTTPError(url="", code=500, msg="Server Error", hdrs=None, fp=None)
        with patch("urllib.request.urlopen", side_effect=exc):
            ok, detail = _check_linear_key_valid("lin_key")
        self.assertTrue(ok)
        self.assertIn("http 500", detail)


class CheckGithubTokenScopesTests(unittest.TestCase):
    def _make_response(self, scopes_header: str):
        resp = type("R", (), {
            "headers": {"X-OAuth-Scopes": scopes_header},
            "read": lambda self: b"{}",
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: False,
        })()
        return resp

    def test_returns_true_when_repo_scope_present(self):
        with patch("urllib.request.urlopen", return_value=self._make_response("repo, read:user")):
            ok, detail = _check_github_token_scopes("ghp_token")
        self.assertTrue(ok)
        self.assertIn("write scopes confirmed", detail)

    def test_returns_true_for_fine_grained_pat_empty_scopes(self):
        with patch("urllib.request.urlopen", return_value=self._make_response("")):
            ok, detail = _check_github_token_scopes("github_pat_token")
        self.assertTrue(ok)
        self.assertIn("fine-grained PAT", detail)

    def test_returns_false_when_repo_scope_missing(self):
        with patch("urllib.request.urlopen", return_value=self._make_response("read:user, gist")):
            ok, detail = _check_github_token_scopes("ghp_token")
        self.assertFalse(ok)
        self.assertIn("missing repo write scope", detail)

    def test_returns_false_on_401(self):
        import urllib.error
        exc = urllib.error.HTTPError(url="", code=401, msg="Unauthorized", hdrs=None, fp=None)
        with patch("urllib.request.urlopen", side_effect=exc):
            ok, detail = _check_github_token_scopes("bad_token")
        self.assertFalse(ok)
        self.assertIn("invalid token", detail)

    def test_returns_true_on_network_error(self):
        import urllib.error
        exc = urllib.error.URLError("timeout")
        with patch("urllib.request.urlopen", side_effect=exc):
            ok, detail = _check_github_token_scopes("ghp_token")
        self.assertTrue(ok)
        self.assertIn("network check skipped", detail)


class SetupEnvironmentChecksAuthTests(unittest.TestCase):
    def _make_args(self, tmp_dir: str, **kwargs):
        defaults = {
            "workflow_path": str(Path(tmp_dir) / "WORKFLOW.md"),
            "linear_api_key": None,
            "github_token": None,
            "credentials_path": None,
            "runner": "codex",
            "codex_command": "python --version",
            "github_org": None,
            "github_repo": None,
        }
        defaults.update(kwargs)
        return type("Args", (), defaults)()

    def test_includes_linear_key_validity_when_token_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self._make_args(tmp, linear_api_key="lin_key")
            with patch("symphony.cli._check_linear_key_valid", return_value=(True, "valid (mocked)")) as mock_valid:
                checks = setup_environment_checks(args, environ={})
        mock_valid.assert_called_once_with("lin_key")
        self.assertTrue(any(label == "linear key validity" for _, label, _ in checks))

    def test_includes_claude_login_check_when_claude_command_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self._make_args(tmp, runner="claude_code", github_token="ghp_tok")
            with (
                patch("symphony.cli._check_command", return_value=(True, "/usr/bin/claude")),
                patch("symphony.cli._check_gh_auth", return_value=(True, "authenticated")),
                patch("symphony.cli._check_claude_login", return_value=(True, "config found")) as mock_login,
                patch("symphony.cli._check_github_token_scopes", return_value=(True, "ok")),
                patch("symphony.cli._check_linear_key_valid", return_value=(True, "ok")),
            ):
                setup_environment_checks(args, environ={})
        mock_login.assert_called_once()

    def test_includes_github_scope_check_when_token_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self._make_args(tmp, runner="claude_code", github_token="ghp_tok")
            with (
                patch("symphony.cli._check_command", return_value=(True, "/usr/bin/claude")),
                patch("symphony.cli._check_gh_auth", return_value=(True, "authenticated")),
                patch("symphony.cli._check_claude_login", return_value=(True, "ok")),
                patch("symphony.cli._check_github_token_scopes", return_value=(True, "scopes ok")) as mock_scope,
                patch("symphony.cli._check_linear_key_valid", return_value=(True, "ok")),
            ):
                checks = setup_environment_checks(args, environ={"GITHUB_TOKEN": "ghp_tok"})
        mock_scope.assert_called_once_with("ghp_tok")
        self.assertTrue(any(label == "github token scopes" for _, label, _ in checks))


class OnboardAutoDetectTests(unittest.TestCase):
    def test_onboard_auto_fills_github_from_remote(self):
        """onboard pre-populates --github-org/repo from git remote when absent."""
        fake_remote = type("R", (), {"returncode": 0, "stdout": "git@github.com:myorg/myrepo.git\n"})()
        fake_gh_auth = type("R", (), {"returncode": 0, "stdout": "", "stderr": "Logged in to github.com account myorg (keyring)\n"})()

        with tempfile.TemporaryDirectory() as tmp:
            workflow_path = Path(tmp) / "WORKFLOW.md"
            credentials_path = str(Path(tmp) / "credentials.json")

            def fake_run(cmd, **_kw):
                if "get-url" in cmd:
                    return fake_remote
                if cmd[0] == "gh":
                    return fake_gh_auth
                return fake_remote

            with patch("subprocess.run", side_effect=fake_run), \
                 patch("shutil.which", return_value="/usr/bin/claude"), \
                 patch("sys.stdin.isatty", return_value=False):
                out = StringIO()
                with redirect_stdout(out):
                    result = main([
                        "onboard",
                        "--workflow-path", str(workflow_path),
                        "--project-slug", "my-project",
                        "--mode", "automated",
                        "--linear-api-key", "lin_api_test",
                        "--credentials-path", credentials_path,
                    ])
            self.assertEqual(0, result)
            content = workflow_path.read_text()
            self.assertIn("my-project", content)


if __name__ == "__main__":
    unittest.main()
