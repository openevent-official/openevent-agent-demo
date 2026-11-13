"""Incremental read barriers retain source ordering, replay and cleanup rules."""

from collections import Counter
from itertools import permutations
import json
import unittest
from unittest.mock import patch

from openevent.cmd_sdk import CmdOutput, CmdOutputReadResultInput, CmdRunResultInput

from chat_model_agent.history import position
from chat_model_agent.tool_runner import ReadProgress
import test_chat_agent_session as fixture


class ReadProgressTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ConversationSDKTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    @staticmethod
    def response(name, arguments, *, text=None):
        return {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": text, "tool_calls": [{
                "id": "call", "type": "function", "function": {
                    "name": name, "arguments": json.dumps(arguments)}}]}}]}

    def prepare_read(self, *, nbytes=4, chunk_bytes=2, large_history=False):
        f = self.f
        f.fresh(context_max_input_bytes=12000 if large_history else 1048576,
                max_tool_output_bytes=nbytes, output_read_chunk_bytes=chunk_bytes)
        text = "x" * (f.session.budget.target - 600) if large_history else "run and read"
        f.input(text)
        f.request_callback(lambda request, payload: f.end(request, payload,
            response_body=self.response("exec", {"command": "printf '%s' '" + "x" * nbytes + "'"})))
        f.run_answer()
        command, = f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind="tool.prepare")

        def finish_command(request, payload):
            if payload.get("kind") == "cmd.run.request":
                f.cmd.publish_run_result(fixture.CMD, "worker-token", fixture.CMD_CHANNEL,
                    fixture.AGENT, CmdRunResultInput(prev_seq=request.seq, status="SUCCESS",
                        timeout_ms=1000, exit_code=0,
                        stdout=CmdOutput("stdout", nbytes, "utf-8", "x" * nbytes),
                        stderr=CmdOutput("stderr", 0, "utf-8", "")))

        f.events.on_publish = finish_command
        f.session._tool_step(command)
        done = f.engine.journal.get(command.data["tool_key"] + ":done")
        exec_id = done.data["request_seq"]
        f.request_callback(lambda request, payload: f.end(request, payload,
            response_body=self.response("read_stdout", {"exec_id": exec_id, "nbytes": nbytes},
                                        text=text if large_history else None)))
        answer = f.run_answer()
        f.events.on_publish = None
        tool = f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind="tool.prepare")[-1]
        self.assertEqual(tool.data["name"], "read_stdout")
        self.answer, self.tool = answer, tool
        return tool

    def runner(self):
        owner = self.f.engine.journal.get(self.tool.data["answer"])
        return self.f.session._child(owner).tools

    def request_page(self, number):
        f, tool, runner = self.f, self.tool, self.runner()
        data = f.engine.journal.materialize(tool).data
        offset = number * data["chunk_bytes"]
        params = {**runner._params(data), "offset": data["request"]["offset"] + offset,
                  "nbytes": min(data["chunk_bytes"], min(data["nbytes"], data["output_limit"]) - offset)}
        request = f.async_runner.run(runner._request(tool, number, params, lambda: False))
        return request, params

    def emit_result(self, request, params, *, failure=False):
        count = 0 if failure else params["nbytes"]
        fields = {"error_message": "read failed"} if failure else {}
        seq = self.f.cmd.publish_output_read_result(fixture.CMD, "worker-token", fixture.CMD_CHANNEL,
            fixture.AGENT, CmdOutputReadResultInput(prev_seq=request,
                status="READ_FAILED" if failure else "SUCCESS", content="x" * count,
                content_encoding="utf-8", bytes=count,
                eof=False if failure else params["offset"] + count == self.tool.data["nbytes"],
                target_seq=params["target_seq"], stream=params["stream"],
                offset=params["offset"], nbytes=params["nbytes"], **fields))
        self.f.async_runner.run(self.f.engine.aread_through(fixture.CMD_CHANNEL, seq))
        return seq

    def progress(self):
        return self.f.session.index.projection.read_progress[self.tool.data["tool_key"]]

    def cancel(self):
        return self.f.user.cancel_turn(
            target_turn=fixture.TurnRef(fixture.AGENT, self.answer.data["turn_id"]),
            start_seq=self.f.session._turn(self.answer).creation_seq)

    def test_each_completed_page_is_checked_once_across_growing_snapshots(self):
        for count in (8, 16):
            with self.subTest(pages=count):
                self.prepare_read(nbytes=count, chunk_bytes=1)
                visits = Counter()
                original = self.f.engine.cmd_result

                def result(request, kind):
                    if kind == "cmd.output.read.result":
                        visits[request] += 1
                    return original(request, kind)

                with patch.object(self.f.engine, "cmd_result", side_effect=result):
                    requests = []
                    for number in range(count):
                        request, params = self.request_page(number)
                        requests.append(request)
                        source = self.emit_result(request, params)
                        snapshot = self.f.session.index.snapshot()
                        if number + 1 < count:
                            self.assertEqual(snapshot.processed, snapshot.read)
                        else:
                            self.assertEqual(snapshot.processed, source - 1)
                    for _ in range(3):
                        self.assertEqual(self.f.session.index.snapshot(), snapshot)
                self.assertEqual(visits, Counter({request: 1 for request in requests}))
                self.assertEqual(self.progress(), ReadProgress(count, count, source))

    def test_missing_or_later_result_does_not_advance_a_fixed_prefix(self):
        tool = self.prepare_read()
        runner, progress = self.runner(), ReadProgress()
        first, params = self.request_page(0)
        self.assertIsNone(runner.pending_source(tool, through=first, progress=progress))
        self.assertEqual(progress, ReadProgress())
        first_result = self.emit_result(first, params)
        self.assertIsNone(runner.pending_source(tool, through=first, progress=progress))
        self.assertEqual(progress, ReadProgress())
        self.assertIsNone(runner.pending_source(tool, through=first_result, progress=progress))
        self.assertEqual(progress, ReadProgress(1, 2))
        second, params = self.request_page(1)
        self.assertIsNone(runner.pending_source(tool, through=second, progress=progress))
        second_result = self.emit_result(second, params)
        self.assertIsNone(runner.pending_source(tool, through=second, progress=progress))
        self.assertEqual(progress, ReadProgress(1, 2))
        self.assertEqual(runner.pending_source(tool, through=second_result, progress=progress), second_result)
        self.assertEqual(progress, ReadProgress(2, 4, second_result))

        self.f.session.index.snapshot()
        current = self.progress()
        # Historical queries must independently rebuild their earlier prefix.
        self.assertIsNone(runner.pending_source(tool, through=first))
        self.assertIsNone(runner.pending_source(tool, through=first_result))
        self.assertIsNone(runner.pending_source(tool, through=second))
        self.assertEqual(runner.pending_source(tool, through=second_result), second_result)
        self.assertIs(self.progress(), current)
        self.assertEqual(current, ReadProgress(2, 4, second_result))

    def test_fast_results_before_request_commits_preserve_the_original_barrier(self):
        tool = self.prepare_read()
        f, observations = self.f, []

        def respond(request, payload):
            if payload.get("kind") == "cmd.output.read.request":
                page = payload["offset"] // tool.data["chunk_bytes"]
                self.assertIsNone(f.engine.journal.get(tool.data["tool_key"] + f":request:{page}:commit"))
                result = self.emit_result(request.seq, payload)
                snapshot = f.session.index.snapshot()
                observations.append((result, snapshot.processed, self.progress().next_page))

        f.events.on_publish = respond
        self.request_page(0)
        self.request_page(1)
        f.events.on_publish = None
        self.assertEqual([item[2] for item in observations], [1, 2])
        source = observations[-1][0]
        self.assertEqual(observations[-1][1], source - 1)
        later = f.input("after the read result")
        snapshot = f.session.index.snapshot()
        self.assertEqual(snapshot.processed, source - 1)
        self.assertNotIn(later, [item.event.get("input_id") for item in snapshot.events])

        request_count = len(f.events.messages("cmd.output.read.request", fixture.CMD_CHANNEL))
        f.session._tool_step(tool)
        snapshot = f.session.index.snapshot()
        result, = [item for item in snapshot.events
                   if item.ref == {"type": "tool_result", "record": tool.data["tool_key"] + ":done"}]
        self.assertEqual(result.cursor, {"seq": source, "index": 0})
        self.assertEqual(result.event["result"]["stdout"]["content"], "xxxx")
        self.assertEqual(len(f.events.messages("cmd.output.read.request", fixture.CMD_CHANNEL)), request_count)
        self.assertNotIn(tool.data["tool_key"], f.session.index.projection.read_progress)

    def test_result_cancel_and_timeout_order_is_recomputed_around_cached_pages(self):
        for order in permutations(("result", "cancel", "timeout")):
            with self.subTest(order=order):
                tool = self.prepare_read()
                first, params = self.request_page(0)
                self.emit_result(first, params)
                self.f.session.index.snapshot()
                progress = self.progress()
                last, params = self.request_page(1)
                sources = {}
                runner = self.runner()
                for kind in order:
                    if kind == "result":
                        sources[kind] = self.emit_result(last, params)
                    elif kind == "cancel":
                        sources[kind] = self.cancel()
                    else:
                        self.f.events.now += self.f.config.agent.cmd_result_timeout_ms
                        sources[kind] = self.f.async_runner.run(runner._timeout(tool, first)).seq
                    self.f.session.index.snapshot()
                    self.assertIs(self.progress(), progress)
                    self.assertEqual(runner.pending_source(tool, progress=progress), min(sources.values()))
                self.assertEqual(progress.source_seq, sources["result"])
                self.assertIsNone(runner.pending_source(tool, through=last))
                self.assertEqual(runner.pending_source(tool, through=min(sources.values())), min(sources.values()))

    def test_failed_page_keeps_its_source_without_rechecking_completed_pages(self):
        tool = self.prepare_read()
        first, params = self.request_page(0)
        self.emit_result(first, params)
        self.f.session.index.snapshot()
        last, params = self.request_page(1)
        source = self.emit_result(last, params, failure=True)
        snapshot = self.f.session.index.snapshot()
        self.assertEqual(snapshot.processed, source - 1)
        self.assertEqual(self.progress(), ReadProgress(1, 2, source))
        with patch.object(self.f.engine, "cmd_result", side_effect=AssertionError("completed read rescanned")):
            self.assertEqual(self.f.session.index.snapshot(), snapshot)
            self.assertEqual(self.runner().pending_source(tool, progress=self.progress()), source)

    def test_restart_rebuilds_partial_progress_and_done_releases_it(self):
        tool = self.prepare_read()
        first, params = self.request_page(0)
        self.emit_result(first, params)
        self.f.session.index.snapshot()
        old = self.progress()
        self.assertEqual(old, ReadProgress(1, 2))
        self.f.restart()
        self.assertEqual(self.f.session.index.projection.read_progress, {})
        self.f.session.index.snapshot()
        self.assertEqual(self.progress(), old)
        self.assertIsNot(self.progress(), old)
        last, params = self.request_page(1)
        source = self.emit_result(last, params)
        self.assertEqual(self.f.session.index.snapshot().processed, source - 1)
        self.f.session._tool_step(tool)
        self.f.session.index.snapshot()
        self.assertNotIn(tool.data["tool_key"], self.f.session.index.projection.read_progress)
        self.f.restart()
        snapshot = self.f.session.index.snapshot()
        self.assertNotIn(tool.data["tool_key"], self.f.session.index.projection.read_progress)
        result, = [item for item in snapshot.events
                   if item.ref == {"type": "tool_result", "record": tool.data["tool_key"] + ":done"}]
        self.assertEqual(result.cursor, {"seq": source, "index": 0})
        self.assertEqual(result.event["result"]["stdout"]["content"], "xxxx")
        self.assertEqual(len(self.f.events.messages("cmd.output.read.request", fixture.CMD_CHANNEL)), 2)

    def test_summary_and_cancellation_keep_unfinished_read_progress_until_done(self):
        tool = self.prepare_read(large_history=True)
        first, params = self.request_page(0)
        self.emit_result(first, params)
        f = self.f
        f.session.index.snapshot()
        progress = self.progress()
        self.assertEqual(progress, ReadProgress(1, 2))
        f.input("continue while the read is pending")
        f.request_callback(lambda request, payload: f.end(request, payload,
            response_body=fixture.body("remember the pending read task")))
        following = f.session._next_answer()
        f.events.on_publish = None
        self.assertIsNotNone(following.data["summary"])
        accepted = f.engine.journal.get(tool.data["tool_key"].rsplit(":tool:", 1)[0] + ":accept")
        _, covered = f.session.index.summary(following.data["summary"])
        self.assertGreaterEqual(position(covered), (accepted.data["terminal_seq"], 1))
        f.session.index.snapshot()
        self.assertIs(self.progress(), progress)

        cancelled = self.cancel()
        f.session.put("answer.cancel", self.answer.key + ":cancel", {"cancel_seq": cancelled})
        snapshot = f.session.index.snapshot()
        self.assertEqual(snapshot.processed, cancelled - 1)
        self.assertIs(self.progress(), progress)
        self.assertEqual(progress, ReadProgress(1, 2))
        f.async_runner.run(self.runner()._done(tool, "read_cancelled", request_seq=first))
        f.session.index.snapshot()
        self.assertNotIn(tool.data["tool_key"], f.session.index.projection.read_progress)


if __name__ == "__main__":
    unittest.main()
