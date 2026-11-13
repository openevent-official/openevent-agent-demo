import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from chat_model_agent.objects import ObjectIndex, ObjectIndexError
from chat_model_agent.read_object import ObjectReadError, command_object, copy_object, main


BUILD = Path(__file__).resolve().parents[1] / "build" / "tests"


class ObjectsTest(unittest.TestCase):
    def setUp(self):
        BUILD.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=BUILD)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.key = {"object_id": 42, "object_token": "private-object-credential"}

    def environment(self, keys=None):
        return {"OPENEVENT_OBJECT_TARGET": "private-address",
                "OPENEVENT_OBJECT_KEYS": json.dumps([self.key] if keys is None else keys)}

    def test_prepare_registers_reference_and_returns_only_model_fields(self):
        metadata = SimpleNamespace(name="记录.bin", type="", nbytes=8)
        calls = []
        index = ObjectIndex(lambda keys: calls.append(keys) or [metadata])
        result = index.prepare_many([SimpleNamespace(**self.key)])[0]
        self.assertEqual(result, {"name": "记录.bin", "type": "application/octet-stream", "nbytes": 8, "object_id": 42})
        self.assertEqual(calls, [[self.key]])
        self.assertNotIn(self.key["object_token"], json.dumps(result))
        self.assertEqual(index.get(42), self.key)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_parallel_session_updates_preserve_all_references(self):
        index = ObjectIndex()
        def register(number):
            index.register({"object_id": number, "object_token": f"secret-{number}"})
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(register, range(1, 25)))
        for number in range(1, 25):
            self.assertEqual(index.get(number), {"object_id": number, "object_token": f"secret-{number}"})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_parallel_preparation_caches_metadata(self):
        calls = []
        index = ObjectIndex(lambda keys: calls.append(keys) or
                            [{"name": "shared.txt", "type": "text/plain", "nbytes": 8}])
        with ThreadPoolExecutor(max_workers=8) as pool:
            descriptions = [batch[0] for batch in pool.map(index.prepare_many, [[self.key]] * 24)]
        self.assertTrue(calls)
        self.assertTrue(all(keys == [self.key] for keys in calls))
        initial_calls = len(calls)
        descriptions[0]["name"] = "caller changed this"
        self.assertEqual(index.prepare_many([self.key])[0]["name"], "shared.txt")
        self.assertEqual(len(calls), initial_calls)
        self.assertEqual(descriptions[1]["name"], "shared.txt")

    def test_slow_metadata_does_not_block_another_object(self):
        started, release = Event(), Event()
        def metadata_reader(keys):
            ident = keys[0]["object_id"]
            if ident == 42:
                started.set()
                if not release.wait(5):
                    raise AssertionError("slow metadata was never released")
            return [{"name": f"{ident}.txt", "type": "text/plain", "nbytes": 8}]
        index = ObjectIndex(metadata_reader)
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(index.prepare_many, [self.key])
            try:
                self.assertTrue(started.wait(2))
                fast = pool.submit(index.prepare_many, [{"object_id": 43, "object_token": "another-secret"}])
                self.assertEqual(fast.result(timeout=2)[0]["object_id"], 43)
            finally:
                release.set()
            self.assertEqual(slow.result(timeout=2)[0]["object_id"], 42)
        self.assertEqual(index.get(42), self.key)
        self.assertEqual(index.get(43)["object_token"], "another-secret")

    def test_metadata_cache_evicts_old_descriptions_but_preserves_references(self):
        calls = []
        def read_metadata(keys):
            calls.extend(key["object_id"] for key in keys)
            return [{"name": f"{key['object_id']}.txt", "type": "text/plain", "nbytes": key["object_id"]}
                    for key in keys]
        index = ObjectIndex(read_metadata)
        keys = [{"object_id": ident, "object_token": f"secret-{ident}"} for ident in (1, 2, 3)]
        with patch("chat_model_agent.objects._DESCRIPTION_CACHE_LIMIT", 2):
            first = index.prepare_many([keys[0]])[0]
            index.prepare_many([keys[1]])
            self.assertEqual(index.prepare_many([keys[0]])[0], first)
            index.prepare_many([keys[2]])
            self.assertEqual(calls, [1, 2, 3])
            self.assertEqual(len(index._descriptions), 2)
            # Evict only descriptions: a previously selected object remains
            # available to an already prepared command without another RPC.
            self.assertEqual(index.get(2), keys[1])
            self.assertEqual(index.prepare_many([keys[0]])[0], first)
            self.assertEqual(calls, [1, 2, 3])
            self.assertEqual(index.prepare_many([keys[1]])[0]["name"], "2.txt")
            self.assertEqual(calls, [1, 2, 3, 2])
            self.assertEqual(len(index._descriptions), 2)

    def test_batch_only_fetches_missing_metadata_and_preserves_order_and_duplicates(self):
        calls = []
        other = {"object_id": 43, "object_token": "another-secret"}
        def read_metadata(keys):
            calls.append(keys)
            return [{"name": str(key["object_id"]), "type": "text/plain", "nbytes": key["object_id"]}
                    for key in keys]
        index = ObjectIndex(read_metadata)
        self.assertEqual(index.prepare_many([]), [])
        self.assertEqual(ObjectIndex().prepare_many([]), [])
        self.assertEqual(calls, [])
        index.prepare_many([self.key])
        descriptions = index.prepare_many([other, self.key, other])
        self.assertEqual(calls, [[self.key], [other]])
        self.assertEqual([value["object_id"] for value in descriptions], [43, 42, 43])
        self.assertEqual(descriptions[1]["name"], "42")
        descriptions[0]["name"] = "caller changed this"
        self.assertEqual(descriptions[2]["name"], "43")
        self.assertEqual(index.prepare_many([other])[0]["name"], "43")
        self.assertEqual(calls, [[self.key], [other]])

    def test_batch_splits_at_service_limit_and_keeps_results_larger_than_the_cache(self):
        calls = []
        keys = [{"object_id": ident, "object_token": f"secret-{ident}"} for ident in range(1, 1026)]
        def read_metadata(batch):
            calls.append([key["object_id"] for key in batch])
            return [{"name": str(key["object_id"]), "type": "text/plain", "nbytes": key["object_id"]}
                    for key in batch]
        index = ObjectIndex(read_metadata)
        descriptions = index.prepare_many([*keys, keys[0]])
        self.assertEqual([len(batch) for batch in calls], [1024, 1])
        self.assertEqual([value["object_id"] for value in descriptions], [*range(1, 1026), 1])
        self.assertEqual(len(index._descriptions), 256)
        self.assertEqual(index.get(2), keys[1])
        self.assertEqual(index.prepare_many([keys[0]])[0]["name"], "1")
        self.assertEqual(len(calls), 2)

    def test_later_batch_failure_does_not_return_partial_results(self):
        calls = []
        keys = [{"object_id": ident, "object_token": f"secret-{ident}"} for ident in range(1, 1026)]
        def read_metadata(batch):
            calls.append(batch)
            if len(calls) == 2:
                raise RuntimeError("metadata RPC failed")
            return [{"name": "attachment", "type": "text/plain", "nbytes": 1} for _ in batch]
        index = ObjectIndex(read_metadata)
        with self.assertRaisesRegex(RuntimeError, "metadata RPC failed"):
            index.prepare_many(keys)
        self.assertEqual([len(batch) for batch in calls], [1024, 1])

    def test_fresh_instance_requires_recovery_from_original_message_references(self):
        index = ObjectIndex()
        index.register(self.key)
        recovered = ObjectIndex()
        with self.assertRaises(ObjectIndexError):
            recovered.get(42)
        recovered.register(self.key)
        self.assertEqual(recovered.get(42), self.key)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_registration_and_lookup_do_not_share_mutable_keys(self):
        index = ObjectIndex()
        supplied = dict(self.key)
        index.register(supplied)
        supplied["object_token"] = "caller changed input"
        received = index.get(42)
        received["object_token"] = "caller changed output"
        self.assertEqual(index.get(42), self.key)

    def test_conflicting_reference_does_not_overwrite_original(self):
        index = ObjectIndex()
        index.register(self.key)
        with self.assertRaises(ObjectIndexError) as error:
            index.register({"object_id": 42, "object_token": "different-secret"})
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(index.get(42), self.key)

    def test_failed_metadata_is_not_cached(self):
        read_metadata = MagicMock(side_effect=[RuntimeError("metadata RPC failed"),
            [{"name": "ready", "type": "", "nbytes": 8}]])
        index = ObjectIndex(read_metadata)
        with self.assertRaisesRegex(RuntimeError, "metadata RPC failed"):
            index.prepare_many([self.key])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(index.prepare_many([self.key])[0]["name"], "ready")
        self.assertEqual(read_metadata.call_count, 2)

    def test_cached_description_does_not_bypass_reference_validation(self):
        index = ObjectIndex(lambda _keys: [{"name": "ready", "type": "", "nbytes": 8}])
        index.prepare_many([self.key])
        with self.assertRaises(ObjectIndexError):
            index.prepare_many([{**self.key, "object_token": "different-secret"}])
        self.assertEqual(index.get(42), self.key)

    def test_command_references_select_multiple_objects_without_consuming_credentials(self):
        other = {"object_id": 43, "object_token": "second-private-credential"}
        environment = self.environment([self.key, other])
        with patch.dict(os.environ, environment, clear=True):
            for key in (self.key, other, self.key):
                received = command_object(key["object_id"])
                self.assertEqual(received, key)
                received["object_token"] = "caller changed output"
            self.assertEqual(os.environ["OPENEVENT_OBJECT_KEYS"], environment["OPENEVENT_OBJECT_KEYS"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_command_references_reject_conflicts_without_disclosing_credentials(self):
        conflicting = {**self.key, "object_token": "different-private-credential"}
        with patch.dict(os.environ, self.environment([self.key, conflicting]), clear=True):
            with self.assertRaisesRegex(ObjectReadError, "^invalid command object references$") as error:
                command_object(42)
        self.assertNotIn(self.key["object_token"], str(error.exception))
        self.assertNotIn(conflicting["object_token"], str(error.exception))

    def test_separate_cli_processes_read_multiple_attachments_without_an_agent(self):
        other = {"object_id": 43, "object_token": "second-private-credential"}
        script = """
import runpy
import sys
from types import SimpleNamespace
from chat_model_agent import dependencies

class Client:
    def __init__(self, target):
        assert target == "private-address"
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        pass
    def content(self, object_id, token):
        expected = {42: ("private-object-credential", b"\\x00\\xfe\\n"),
                    43: ("second-private-credential", b"second attachment")}
        expected_token, data = expected[object_id]
        assert token == expected_token
        return data
    def read_object(self, object_id, object_token, offset, nbytes):
        return SimpleNamespace(data=self.content(object_id, object_token)[offset:offset + nbytes])

dependencies.require_sdk = lambda: SimpleNamespace(OpenEventClient=Client)
sys.argv[0] = "openevent-read-object"
runpy.run_module("chat_model_agent.read_object", run_name="__main__")
"""
        environment = {**os.environ, **self.environment([self.key, other]), "PYTHONDONTWRITEBYTECODE": "1"}
        for ident, expected in ((42, b"\x00\xfe\n"), (43, b"second attachment"), (42, b"\x00\xfe\n")):
            with self.subTest(object_id=ident):
                result = subprocess.run([sys.executable, "-B", "-c", script, str(ident)],
                    cwd=BUILD.parents[1], capture_output=True, timeout=5, env=environment)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, expected, b""))

    def test_reads_complete_binary_objects_through_4_mib_with_one_rpc(self):
        for original in (b"\x00", b"\x00\xff\xe4\xb8\xad\nsecond", bytes(range(256)) * 16384):
            with self.subTest(size=len(original)):
                calls = []
                class Client:
                    def read_object(self, object_id, object_token, offset, nbytes):
                        calls.append((object_id, object_token, offset, nbytes))
                        return SimpleNamespace(data=original[offset:offset + nbytes])
                output = io.BytesIO()
                copy_object(Client(), self.key, output)
                self.assertEqual(output.getvalue(), original)
                self.assertEqual(calls, [(42, self.key["object_token"], 0, 4 * 1024 * 1024)])

    def test_cli_emits_raw_bytes_without_credentials(self):
        class Client:
            def __init__(self, target):
                self.target = target
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                pass
            def read_object(self, *_args):
                return SimpleNamespace(data=b"\x00\xfe\n")
        output, errors = io.BytesIO(), io.StringIO()
        with patch.dict(os.environ, self.environment(), clear=True), patch("chat_model_agent.read_object.require_sdk", return_value=SimpleNamespace(OpenEventClient=Client)), patch("sys.stdout", SimpleNamespace(buffer=output)), patch("sys.stderr", errors):
            self.assertEqual(main(["42"]), 0)
        self.assertEqual(output.getvalue(), b"\x00\xfe\n")
        self.assertEqual(errors.getvalue(), "")

    def test_cli_does_not_echo_remote_errors_containing_secrets(self):
        errors = io.StringIO()
        with patch.dict(os.environ, self.environment(), clear=True), patch("chat_model_agent.read_object.require_sdk", side_effect=RuntimeError(self.key["object_token"])), patch("sys.stderr", errors):
            self.assertEqual(main(["42"]), 1)
        self.assertEqual(errors.getvalue(), "openevent-read-object: OpenEvent SDK is unavailable or incompatible\n")
        self.assertNotIn(self.key["object_token"], errors.getvalue())
        self.assertNotIn("private-address", errors.getvalue())

    def run_reader(self, args=("42",), *, environment=None, sdk=None, output=None):
        environment = environment if environment is not None else self.environment()
        output, errors = output if output is not None else io.BytesIO(), io.StringIO()
        with patch.dict(os.environ, environment, clear=True), patch("chat_model_agent.read_object.require_sdk", return_value=sdk), patch("sys.stdout", SimpleNamespace(buffer=output)), patch("sys.stderr", errors):
            status = main(args)
        for private in (self.key["object_token"], "private-address"):
            self.assertNotIn(private, errors.getvalue())
        return status, output, errors.getvalue()

    def test_cli_explains_invalid_ids_and_missing_environment(self):
        for ident in ("0", "-1", str(1 << 64), "not-an-id"):
            with self.subTest(object_id=ident):
                status, output, error = self.run_reader([ident])
                self.assertEqual((status, output.getvalue(), error),
                                 (1, b"", "openevent-read-object: invalid object ID\n"))
        for environment in ({}, {"OPENEVENT_OBJECT_TARGET": "private-address"},
                            {"OPENEVENT_OBJECT_KEYS": "[]"},
                            {"OPENEVENT_OBJECT_TARGET": "", "OPENEVENT_OBJECT_KEYS": "[]"}):
            with self.subTest(environment=environment):
                status, output, error = self.run_reader(environment=environment)
                self.assertEqual((status, output.getvalue(), error),
                                 (1, b"", "openevent-read-object: object reader environment is not configured\n"))

    def test_cli_reports_an_object_missing_from_the_command_references(self):
        other = {"object_id": 43, "object_token": "second-private-credential"}
        for keys in ([], [other]):
            with self.subTest(keys=keys):
                sdk = MagicMock()
                status, output, error = self.run_reader(environment=self.environment(keys), sdk=sdk)
                self.assertEqual((status, output.getvalue(), error),
                                 (1, b"", "openevent-read-object: object ID is not registered\n"))
                self.assertNotIn(other["object_token"], error)
                sdk.OpenEventClient.assert_not_called()

    def test_cli_rejects_malformed_references_without_disclosing_credentials(self):
        malformed = ("", "not-json-private-object-credential", json.dumps(self.key),
                     json.dumps([{"object_id": 42}]), json.dumps([{**self.key, "object_id": 0}]),
                     json.dumps([self.key, {**self.key, "object_token": "different-private-credential"}]))
        for encoded in malformed:
            with self.subTest(references=encoded):
                sdk = MagicMock()
                environment = {**self.environment(), "OPENEVENT_OBJECT_KEYS": encoded}
                status, output, error = self.run_reader(environment=environment, sdk=sdk)
                self.assertEqual((status, output.getvalue(), error),
                                 (1, b"", "openevent-read-object: invalid command object references\n"))
                self.assertNotIn("different-private-credential", error)
                sdk.OpenEventClient.assert_not_called()

    def test_cli_explains_service_failures_without_remote_details(self):
        secret = f"{self.key['object_token']} private-address"
        for failure, expected in (
            ("connect", "could not connect to object service"),
            ("content", "could not read object content"),
        ):
            with self.subTest(failure=failure):
                client = MagicMock()
                client.__enter__.return_value = client
                client.read_object.return_value = SimpleNamespace(data=b"abc")
                sdk = SimpleNamespace(OpenEventClient=MagicMock(return_value=client))
                if failure == "connect":
                    sdk.OpenEventClient.side_effect = RuntimeError(secret)
                else:
                    client.read_object.side_effect = RuntimeError(secret)
                status, output, error = self.run_reader(sdk=sdk)
                self.assertEqual((status, output.getvalue(), error),
                                 (1, b"", f"openevent-read-object: {expected}\n"))

    def test_cli_reports_partial_stdout_failure_without_private_details(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.read_object.return_value = SimpleNamespace(data=b"\x00\xff\nremaining")
        sdk = SimpleNamespace(OpenEventClient=lambda _target: client)
        class FailingOutput(io.BytesIO):
            def write(self, data):
                super().write(data[:3])
                raise OSError("private-object-credential private-address")
        status, output, error = self.run_reader(sdk=sdk, output=FailingOutput())
        self.assertEqual((status, output.getvalue(), error),
                         (1, b"\x00\xff\n", "openevent-read-object: could not write object content to stdout\n"))
        client.__exit__.assert_called_once()

    def test_cli_keeps_broken_pipe_quiet_when_writing_or_flushing(self):
        for operation in ("write", "flush"):
            with self.subTest(operation=operation):
                client = MagicMock()
                client.__enter__.return_value = client
                client.read_object.return_value = SimpleNamespace(data=b"\x00\xff\n")
                sdk = SimpleNamespace(OpenEventClient=lambda _target: client)
                output = MagicMock()
                getattr(output, operation).side_effect = BrokenPipeError("private output detail")
                status, _, error = self.run_reader(sdk=sdk, output=output)
                self.assertEqual((status, error), (1, ""))
                client.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
