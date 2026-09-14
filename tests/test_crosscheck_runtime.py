import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

from jazzband.config import ReviewConfig
from jazzband.crosscheck_runtime import ReviewDispatcher
import test_runtime as helpers


def test_reviewer_pair_and_singleflight():
    async def run():
        dispatcher = ReviewDispatcher()
        proc = MagicMock(returncode=0)
        proc.wait = AsyncMock()
        with patch("jazzband.crosscheck_runtime.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc) as spawn:
            args = dict(branch="feature", sha="a" * 40, reviewer="claude_code", url="https://github.com/acme/demo/pull/1", token="fixture-token")
            dispatcher.schedule(**args)
            dispatcher.schedule(**args)
            await dispatcher.tasks["feature"]
            dispatcher.schedule(**args)
            assert spawn.await_count == 1
            assert spawn.call_args.args[:4] == ("crosscheck", "review", "--reviewer", "claude")
            assert spawn.call_args.kwargs["env"]["GITHUB_TOKEN"] == "fixture-token"
            dispatcher.schedule(**{**args, "sha": "b" * 40, "reviewer": "codex"})
            await dispatcher.tasks["feature"]
            assert spawn.await_count == 2
            assert spawn.call_args.args[3] == "codex"
            await dispatcher.close()
    asyncio.run(run())


def test_failed_review_does_not_assume_verdict(caplog):
    async def run():
        dispatcher = ReviewDispatcher()
        with patch("jazzband.crosscheck_runtime.asyncio.create_subprocess_exec", new_callable=AsyncMock, side_effect=FileNotFoundError("secret-token")):
            dispatcher.schedule(branch="feature", sha="a" * 40, reviewer="codex", url="https://github.com/acme/demo/pull/1", token="fixture-token")
            await dispatcher.tasks["feature"]
            await dispatcher.close()
    asyncio.run(run())
    assert "no verdict assumed" in caplog.text
    assert "secret-token" not in caplog.text


def test_close_terminates_agent_process_group():
    async def run():
        dispatcher = ReviewDispatcher()
        started = asyncio.Event()
        proc = MagicMock(returncode=None, pid=12345)
        async def wait():
            started.set()
            await asyncio.Event().wait()
        proc.wait = AsyncMock(side_effect=wait)
        with patch("jazzband.crosscheck_runtime.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc), patch("jazzband.crosscheck_runtime.os.killpg") as kill:
            dispatcher.schedule(branch="feature", sha="a" * 40, reviewer="codex", url="https://github.com/acme/demo/pull/1", token="fixture-token")
            await started.wait()
            proc.wait.side_effect = None
            proc.wait.return_value = None
            await dispatcher.close()
            kill.assert_called_once()
    asyncio.run(run())


def test_tracked_pr_poll_dispatches_configured_review(tmp_path):
    async def run():
        runtime = helpers.FeedbackGateTests()._make_runtime(helpers.FeedbackTracker([]), str(tmp_path))
        runtime.config = replace(runtime.config, review=ReviewConfig(True, "cross-vendor", "codex"))
        gh = MagicMock(owner="acme", repo="demo", token="fixture-token")
        gh.find_open_pr_for_branch.return_value = 1
        gh.get_pr.return_value = {"mergeable": True, "head": {"sha": "a" * 40}}
        for name in ("list_pr_review_comments", "list_pr_issue_comments", "list_pr_reviews", "get_pr_failed_check_runs"):
            getattr(gh, name).return_value = []
        runtime.github_client = gh
        runtime.review_dispatcher.schedule = MagicMock()
        await runtime._poll_pr_for_branch("feature", helpers.issue())
        runtime.review_dispatcher.schedule.assert_called_once_with(branch="feature", sha="a" * 40, reviewer="codex", url="https://github.com/acme/demo/pull/1", token="fixture-token")
        runtime.config = replace(runtime.config, review=ReviewConfig())
        runtime.review_dispatcher.schedule.reset_mock()
        await runtime._poll_pr_for_branch("feature", helpers.issue())
        runtime.review_dispatcher.schedule.assert_not_called()
    asyncio.run(run())
