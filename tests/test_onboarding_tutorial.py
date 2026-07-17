import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jazzband.onboarding_tutorial import (
    INIT_TUTORIAL_ID,
    INIT_TUTORIAL_VERSION,
    default_tutorial_history_path,
    print_init_tutorial,
    prompt_tutorial_language,
    record_tutorial_seen,
    run_init_tutorial_once,
    should_show_tutorial,
)


class OnboardingTutorialTests(unittest.TestCase):
    def test_default_history_path_honors_xdg_config_home(self):
        path = default_tutorial_history_path({"XDG_CONFIG_HOME": "/tmp/jazzband-config"})

        self.assertEqual(Path("/tmp/jazzband-config/jazzband/tutorials.json"), path)

    def test_print_init_tutorial_explains_purpose_and_next_steps(self):
        lines: list[str] = []
        completed = print_init_tutorial(input_func=lambda _prompt: "", output_func=lines.append)
        text = "\n".join(lines)

        self.assertTrue(completed)
        self.assertIn("[1/6] What is Jazzband?", text)
        self.assertIn("[5/6] What should I expect next?", text)
        self.assertIn("[6/6] One project per WORKFLOW.md", text)
        self.assertIn("WORKFLOW.md", text)
        self.assertIn("3-5 Codex sessions", text)
        self.assertIn("500% in the first three weeks", text)
        self.assertIn("https://openai.com/index/open-source-codex-orchestration-jazzband/", text)
        self.assertIn("jazzband doctor WORKFLOW.md", text)
        self.assertIn("jazzband run WORKFLOW.md --once", text)
        self.assertIn("pkill -f 'jazzband run'", text)

    def test_print_init_tutorial_supports_simplified_chinese(self):
        lines: list[str] = []
        completed = print_init_tutorial("zh-cn", input_func=lambda _prompt: "", output_func=lines.append)
        text = "\n".join(lines)

        self.assertTrue(completed)
        self.assertIn("欢迎使用 Jazzband", text)
        self.assertIn("[1/6] Jazzband 是什么?", text)
        self.assertIn("[6/6] 一个 WORKFLOW.md 对应一个项目", text)
        self.assertIn("WORKFLOW.md", text)
        self.assertIn("3-5 个 Codex session", text)
        self.assertIn("提升了 500%", text)
        self.assertIn("https://openai.com/index/open-source-codex-orchestration-jazzband/", text)
        self.assertIn("jazzband doctor WORKFLOW.md", text)
        self.assertIn("jazzband run WORKFLOW.md --once", text)
        self.assertIn("pkill -f 'jazzband run'", text)

    def test_print_init_tutorial_skip_does_not_complete(self):
        lines: list[str] = []
        prompts: list[str] = []

        completed = print_init_tutorial(
            input_func=lambda prompt: prompts.append(prompt) or "s",
            output_func=lines.append,
        )

        self.assertFalse(completed)
        self.assertIn("Orientation skipped", "\n".join(lines))
        self.assertEqual(["Press Enter for next, or type s to skip: "], prompts)

    def test_language_picker_displays_version_and_defaults_to_english(self):
        lines: list[str] = []
        prompts: list[str] = []

        language = prompt_tutorial_language(
            version=INIT_TUTORIAL_VERSION,
            input_func=lambda prompt: prompts.append(prompt) or "",
            output_func=lines.append,
        )

        self.assertEqual("en", language)
        self.assertIn(f"tutorial v{INIT_TUTORIAL_VERSION}", "\n".join(lines))
        self.assertEqual(["Language [1]: "], prompts)

    def test_language_picker_accepts_chinese(self):
        language = prompt_tutorial_language(
            version=INIT_TUTORIAL_VERSION,
            input_func=lambda _prompt: "2",
            output_func=lambda _line: None,
        )

        self.assertEqual("zh-cn", language)

    def test_language_picker_acknowledges_unrecognized_input(self):
        lines: list[str] = []

        language = prompt_tutorial_language(
            version=INIT_TUTORIAL_VERSION,
            input_func=lambda _prompt: "fr",
            output_func=lines.append,
        )

        self.assertEqual("en", language)
        self.assertIn("Unrecognized input", "\n".join(lines))

    def test_tutorial_history_records_seen_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            history_path = Path(temp_dir) / "tutorials.json"

            self.assertTrue(should_show_tutorial(INIT_TUTORIAL_ID, INIT_TUTORIAL_VERSION, path=history_path))
            record_tutorial_seen(INIT_TUTORIAL_ID, INIT_TUTORIAL_VERSION, language="en", path=history_path)

            payload = json.loads(history_path.read_text(encoding="utf-8"))
            self.assertEqual(INIT_TUTORIAL_VERSION, payload["tutorials"][INIT_TUTORIAL_ID]["version"])
            self.assertEqual("en", payload["tutorials"][INIT_TUTORIAL_ID]["language"])
            self.assertFalse(should_show_tutorial(INIT_TUTORIAL_ID, INIT_TUTORIAL_VERSION, path=history_path))
            self.assertTrue(should_show_tutorial(INIT_TUTORIAL_ID, "999", path=history_path))

    def test_tutorial_history_write_failure_is_non_fatal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            history_path = Path(temp_dir) / "tutorials.json"

            with patch("pathlib.Path.write_text", side_effect=OSError("read-only")):
                returned = record_tutorial_seen(
                    INIT_TUTORIAL_ID,
                    INIT_TUTORIAL_VERSION,
                    language="en",
                    path=history_path,
                )

            self.assertEqual(history_path, returned)

    def test_tutorial_history_directory_failure_is_non_fatal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            history_path = Path(temp_dir) / "missing" / "tutorials.json"

            with patch("pathlib.Path.mkdir", side_effect=OSError("permission denied")):
                returned = record_tutorial_seen(
                    INIT_TUTORIAL_ID,
                    INIT_TUTORIAL_VERSION,
                    language="en",
                    path=history_path,
                )

            self.assertEqual(history_path, returned)

    def test_run_init_tutorial_once_skips_after_seen_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            history_path = Path(temp_dir) / "tutorials.json"
            first_lines: list[str] = []
            second_lines: list[str] = []

            first = run_init_tutorial_once(
                history_path=history_path,
                input_func=lambda _prompt: "",
                output_func=first_lines.append,
            )
            second = run_init_tutorial_once(
                history_path=history_path,
                input_func=lambda _prompt: "2",
                output_func=second_lines.append,
            )

            self.assertTrue(first)
            self.assertFalse(second)
            self.assertIn("Welcome to Jazzband", "\n".join(first_lines))
            self.assertEqual([], second_lines)

    def test_run_init_tutorial_once_does_not_record_skipped_tutorial(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            history_path = Path(temp_dir) / "tutorials.json"

            shown = run_init_tutorial_once(
                history_path=history_path,
                input_func=lambda prompt: "" if prompt == "Language [1]: " else "s",
                output_func=lambda _line: None,
            )

            self.assertTrue(shown)
            self.assertTrue(should_show_tutorial(INIT_TUTORIAL_ID, INIT_TUTORIAL_VERSION, path=history_path))

    def test_run_init_tutorial_once_skips_when_stdin_is_not_tty(self):
        class NonTTY:
            def isatty(self):
                return False

        with tempfile.TemporaryDirectory() as temp_dir:
            history_path = Path(temp_dir) / "tutorials.json"
            lines: list[str] = []

            shown = run_init_tutorial_once(
                history_path=history_path,
                input_stream=NonTTY(),
                output_func=lines.append,
            )

            self.assertFalse(shown)
            self.assertEqual([], lines)
            self.assertFalse(history_path.exists())

    def test_run_init_tutorial_once_allows_injected_input_without_tty(self):
        class NonTTY:
            def isatty(self):
                return False

        with tempfile.TemporaryDirectory() as temp_dir:
            history_path = Path(temp_dir) / "tutorials.json"

            shown = run_init_tutorial_once(
                history_path=history_path,
                input_func=lambda _prompt: "",
                input_stream=NonTTY(),
                output_func=lambda _line: None,
            )

            self.assertTrue(shown)


if __name__ == "__main__":
    unittest.main()
