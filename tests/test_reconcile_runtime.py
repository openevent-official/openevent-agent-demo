from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from contextlib import redirect_stdout
from pathlib import Path

import yaml


def _load_module():
    path = Path(__file__).resolve().parents[1] / "scripts/reconcile_runtime.py"
    spec = importlib.util.spec_from_file_location("reconcile_runtime", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reconcile = _load_module()


def _spec(root: str | None = None) -> dict:
    runtime_root = Path(root or "runtime/agent-demo-test")
    return {
        "version": "v1",
        "runtime": {
            "name": "agent-demo-test",
            "root": root or "runtime/agent-demo-test",
            "supervisor": {
                "programs": {
                    "openevent": "openevent",
                    "im_syncer": "im-p2p-syncer",
                    "model_proxy": "model-proxy",
                    "cmd_worker": "cmd-worker",
                    "agent": "im-model-agent",
                }
            },
        },
        "openevent": {
            "grpc_addr": "127.0.0.1:9527",
            "admin_addr": "127.0.0.1:9528",
            "storage": {"metadata_path": str(runtime_root / "data/openevent/meta")},
            "store": {"rocksdb": {"path": str(runtime_root / "data/openevent/messages")}},
        },
        "principals": {
            "p_worker": 90001,
            "p_bot": 90002,
            "p_model": 20001,
            "p_cmd_worker": 30001,
            "p_user": 10001,
        },
        "im": {
            "provider": "lark",
            "worker_principal": "p_worker",
            "users": [{"principal": "p_user", "user_email": "user@example.com"}],
            "bot": {"principal": "p_bot", "app_id": "cli_app", "app_secret": "secret"},
        },
        "model": {
            "proxy_principal": "p_model",
            "base_url": "https://api.example.test",
            "api_key": "sk-test",
            "model": "gpt-test",
        },
        "cmd": {"worker_principal": "p_cmd_worker"},
        "agent": {"principal": "p_bot", "system_prompt": "be useful"},
        "sessions": [
            {
                "session_id": "s1",
                "user": {"principal": "p_user"},
                "channels": {"im": "agent-demo-test.im.s1"},
            }
        ],
    }


def _write_spec(data: dict, root: Path) -> Path:
    path = root / "desired.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


@dataclass
class Binding:
    principal: int
    token: str


@dataclass
class Channel:
    channel_id: int
    name: str
    visibility: int
    protocol: str
    description: str
    creator: int
    members: list[int] = field(default_factory=list)


class FakeRuntime:
    def __init__(self):
        self.bindings: list[Binding] = []
        self.channels: list[Channel] = []
        self.next_channel_id = 100

    def token_usable(self, principal: int, token: str) -> bool:
        return any(binding.principal == principal and binding.token == token for binding in self.bindings)

    def list_tokens(self):
        return list(self.bindings)

    def add_token(self, principal: int) -> str:
        token = f"tok-{principal}"
        self.bindings.append(Binding(principal, token))
        return token

    def list_channels(self, principal: int, token: str):
        return list(self.channels)

    def get_channel(self, principal: int, token: str, channel_id: int):
        return next((channel for channel in self.channels if channel.channel_id == channel_id), None)

    def create_channel(self, principal: int, token: str, **kwargs):
        channel = Channel(
            channel_id=self.next_channel_id,
            name=kwargs["name"],
            visibility=kwargs["visibility"],
            protocol=kwargs["protocol"],
            description=kwargs["description"],
            creator=principal,
            members=list(dict.fromkeys([principal, *kwargs["members"]])),
        )
        self.next_channel_id += 1
        self.channels.append(channel)
        return channel

    def add_member(self, principal: int, token: str, channel_id: int, target_principal: int):
        channel = self.get_channel(principal, token, channel_id)
        if target_principal not in channel.members:
            channel.members.append(target_principal)


class ReconcileRuntimeTests(unittest.TestCase):
    def test_openevent_storage_paths_are_required(self):
        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            del data["openevent"]["storage"]
            path = _write_spec(data, Path(temp))

            with self.assertRaisesRegex(reconcile.SpecError, r"openevent\.storage"):
                reconcile.parse_spec(path, repo_root=Path(temp))

        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            del data["openevent"]["store"]["rocksdb"]["path"]
            path = _write_spec(data, Path(temp))

            with self.assertRaisesRegex(reconcile.SpecError, r"openevent\.store\.rocksdb\.path"):
                reconcile.parse_spec(path, repo_root=Path(temp))

    def test_openevent_storage_paths_render_from_spec(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = _spec(temp)
            data["openevent"]["storage"]["metadata_path"] = "custom/meta"
            data["openevent"]["store"]["rocksdb"]["path"] = "custom/messages"
            spec = reconcile.parse_spec(_write_spec(data, root), repo_root=root)

            config = reconcile.render_openevent_config(spec)

            self.assertEqual(config["storage"]["metadata_path"], str(root / "custom/meta"))
            self.assertEqual(config["store"]["rocksdb"]["path"], str(root / "custom/messages"))

    def test_normalized_includes_principal_refs_and_resolved_values(self):
        with tempfile.TemporaryDirectory() as temp:
            spec = reconcile.parse_spec(_write_spec(_spec(temp), Path(temp)), repo_root=Path(temp))
            normalized = spec.normalized()

            self.assertEqual(normalized["im"]["provider"], "lark")
            self.assertEqual(normalized["im"]["worker_principal"], "p_worker")
            self.assertEqual(normalized["im"]["resolved_worker_principal"], 90001)
            self.assertEqual(normalized["im"]["users"][0]["user_email"], "user@example.com")
            self.assertEqual(normalized["im"]["bot"]["principal"], "p_bot")
            self.assertEqual(normalized["im"]["bot"]["resolved_principal"], 90002)
            self.assertEqual(normalized["im"]["bot"]["api_base_url"], "https://open.larksuite.com")
            self.assertEqual(normalized["model"]["proxy_principal"], "p_model")
            self.assertEqual(normalized["model"]["resolved_proxy_principal"], 20001)
            self.assertEqual(normalized["cmd"]["worker_principal"], "p_cmd_worker")
            self.assertEqual(normalized["cmd"]["resolved_worker_principal"], 30001)
            self.assertEqual(normalized["agent"]["principal"], "p_bot")
            self.assertEqual(normalized["agent"]["resolved_principal"], 90002)
            self.assertEqual(normalized["sessions"][0]["user"]["principal"], "p_user")
            self.assertEqual(normalized["sessions"][0]["user"]["resolved_principal"], 10001)

    def test_dry_rendered_configs_validate_against_module_parsers(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_spec(_spec(temp), Path(temp))
            spec = reconcile.parse_spec(path, repo_root=Path(temp))
            resolved = reconcile.dry_resolve(spec)
            configs = reconcile.render_configs(spec, resolved)

            reconcile.validate_configs(configs)
            im_config = configs["im_syncer"]["data"]
            agent_config = configs["agent"]["data"]

            self.assertEqual(im_config["providers"][0]["name"], "lark")
            self.assertEqual(im_config["providers"][0]["adapter"], "lark")
            self.assertEqual(im_config["providers"][0]["options"]["api_base_url"], "https://open.larksuite.com")
            self.assertNotIn(
                spec.im_worker_principal,
                {item["principal"] for item in im_config["principal_tokens"]},
            )
            self.assertEqual(len(im_config["mappings"]), 2)
            self.assertTrue(im_config["mappings"][0]["external_user_id"].startswith("dry-open-id-"))
            self.assertEqual(agent_config["sessions"][0]["session_id"], "s1")
            self.assertEqual(agent_config["sessions"][0]["im_channel_id"], 10001)
            self.assertEqual(agent_config["sessions"][0]["cmd_channel_id"], 40001)
            self.assertEqual(agent_config["cmd_worker"]["principal"], 30001)
            self.assertNotIn("from_seq", agent_config["openevent"]["subscribe"])

    def test_user_phone_dry_run_uses_placeholder_external_id(self):
        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            del data["im"]["users"][0]["user_email"]
            data["im"]["users"][0]["user_phone"] = "+8613800000000"
            path = _write_spec(data, Path(temp))

            spec = reconcile.parse_spec(path, repo_root=Path(temp))
            resolved = reconcile.dry_resolve(spec)
            configs = reconcile.render_configs(spec, resolved)

            external_user_id = configs["im_syncer"]["data"]["mappings"][0]["external_user_id"]
            self.assertTrue(external_user_id.startswith("dry-open-id-"))
            self.assertEqual(resolved.im_user_external_id_sources[10001], "dry-run-phone")

    def test_user_email_dry_run_uses_placeholder_external_id(self):
        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            path = _write_spec(data, Path(temp))

            spec = reconcile.parse_spec(path, repo_root=Path(temp))
            resolved = reconcile.dry_resolve(spec)
            configs = reconcile.render_configs(spec, resolved)

            external_user_id = configs["im_syncer"]["data"]["mappings"][0]["external_user_id"]
            self.assertTrue(external_user_id.startswith("dry-open-id-"))
            self.assertEqual(resolved.im_user_external_id_sources[10001], "dry-run-email")

    def test_im_bot_mapping_uses_app_id(self):
        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            path = _write_spec(data, Path(temp))

            spec = reconcile.parse_spec(path, repo_root=Path(temp))
            resolved = reconcile.dry_resolve(spec)
            mappings = reconcile.render_configs(spec, resolved)["im_syncer"]["data"]["mappings"]

            bot_mapping = next(item for item in mappings if item["identity_type"] == "bot")
            self.assertEqual(bot_mapping["external_user_id"], "cli_app")

    def test_im_bot_api_base_url_is_provider_managed(self):
        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            data["im"]["bot"]["api_base_url"] = "https://example.test"
            path = _write_spec(data, Path(temp))

            with self.assertRaisesRegex(reconcile.SpecError, r"im\.bot\.api_base_url"):
                reconcile.parse_spec(path, repo_root=Path(temp))

    def test_im_user_inline_token_maps_to_principal_ref(self):
        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            data["im"]["users"][0]["token"] = "tok-inline-user"
            path = _write_spec(data, Path(temp))

            spec = reconcile.parse_spec(path, repo_root=Path(temp))
            resolved = reconcile.dry_resolve(spec)

            self.assertEqual(resolved.tokens["p_user"], "tok-inline-user")
            self.assertEqual(resolved.token_sources["p_user"], "input")

    def test_im_user_inline_token_must_match_tokens_ref(self):
        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            data["tokens"] = {"p_user": "tok-global-user"}
            data["im"]["users"][0]["token"] = "tok-inline-user"
            path = _write_spec(data, Path(temp))

            with self.assertRaisesRegex(reconcile.SpecError, r"im\.users\[0\]\.token"):
                reconcile.parse_spec(path, repo_root=Path(temp))

    def test_user_phone_apply_resolution_uses_lark_openapi_resolver(self):
        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            del data["im"]["users"][0]["user_email"]
            data["im"]["users"][0]["user_phone"] = "+8613800000000"
            spec = reconcile.parse_spec(_write_spec(data, Path(temp)), repo_root=Path(temp))
            actions: list[dict] = []

            original = reconcile.LarkOpenAPIUserResolver.open_id_by_phone

            def fake_open_id_by_phone(self, phone):
                return "ou_from_phone"

            try:
                reconcile.LarkOpenAPIUserResolver.open_id_by_phone = fake_open_id_by_phone
                external_ids, sources = reconcile._resolve_im_user_external_ids(
                    spec,
                    dry_run=False,
                    actions=actions,
                )
            finally:
                reconcile.LarkOpenAPIUserResolver.open_id_by_phone = original

            self.assertEqual(external_ids[10001], "ou_from_phone")
            self.assertEqual(sources[10001], "lark-phone")

    def test_user_email_apply_resolution_uses_lark_openapi_resolver(self):
        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            spec = reconcile.parse_spec(_write_spec(data, Path(temp)), repo_root=Path(temp))
            actions: list[dict] = []

            original = reconcile.LarkOpenAPIUserResolver.open_id_by_email

            def fake_open_id_by_email(self, email):
                return "ou_from_email"

            try:
                reconcile.LarkOpenAPIUserResolver.open_id_by_email = fake_open_id_by_email
                external_ids, sources = reconcile._resolve_im_user_external_ids(
                    spec,
                    dry_run=False,
                    actions=actions,
                )
            finally:
                reconcile.LarkOpenAPIUserResolver.open_id_by_email = original

            self.assertEqual(external_ids[10001], "ou_from_email")
            self.assertEqual(sources[10001], "lark-email")


    def test_provider_session_is_resolved_from_state(self):
        with tempfile.TemporaryDirectory() as temp:
            spec = reconcile.parse_spec(_write_spec(_spec(temp), Path(temp)), repo_root=Path(temp))
            previous = {
                "im": {
                    "provider": "lark",
                    "sessions": {
                        "s1": {"provider": "lark", "provider_session_id": "oc_state"}
                    }
                }
            }
            actions: list[dict] = []

            provider_sessions, sources = reconcile._resolve_im_provider_session_ids(
                spec,
                {10001: "ou_1"},
                previous,
                dry_run=False,
                actions=actions,
            )

            self.assertEqual(provider_sessions["s1"], "oc_state")
            self.assertEqual(sources["s1"], "state")

    def test_provider_session_state_must_match_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            spec = reconcile.parse_spec(_write_spec(_spec(temp), Path(temp)), repo_root=Path(temp))
            previous = {
                "im": {
                    "provider": "feishu",
                    "sessions": {
                        "s1": {"provider": "feishu", "provider_session_id": "oc_state"}
                    }
                }
            }
            actions: list[dict] = []

            provider_sessions, sources = reconcile._resolve_im_provider_session_ids(
                spec,
                {10001: "ou_1"},
                previous,
                dry_run=True,
                actions=actions,
            )

            self.assertTrue(provider_sessions["s1"].startswith("dry-lark-chat-"))
            self.assertEqual(sources["s1"], "dry-run")

    def test_provider_session_dry_run_uses_user_principal_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            spec = reconcile.parse_spec(_write_spec(_spec(temp), Path(temp)), repo_root=Path(temp))
            actions: list[dict] = []

            provider_sessions, sources = reconcile._resolve_im_provider_session_ids(
                spec,
                {10001: "ou_1"},
                None,
                dry_run=True,
                actions=actions,
            )

            self.assertTrue(provider_sessions["s1"].startswith("dry-lark-chat-"))
            self.assertEqual(sources["s1"], "dry-run")

    def test_im_users_are_required(self):
        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            del data["im"]["users"]
            path = _write_spec(data, Path(temp))

            with self.assertRaises(reconcile.SpecError):
                reconcile.parse_spec(path, repo_root=Path(temp))

    def test_session_channel_ids_can_pin_im_channel(self):
        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            data["sessions"][0]["channel_ids"] = {"im": 77}
            spec = reconcile.parse_spec(_write_spec(data, Path(temp)), repo_root=Path(temp))
            resolved = reconcile.dry_resolve(spec)

            self.assertEqual(spec.sessions[0].im_channel_id, 77)
            self.assertEqual(resolved.channels["s1"].im, 77)

    def test_session_user_must_reference_im_user(self):
        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            data["sessions"][0]["user"]["principal"] = "missing_user"
            path = _write_spec(data, Path(temp))

            with self.assertRaises(reconcile.SpecError):
                reconcile.parse_spec(path, repo_root=Path(temp))

    def test_tokens_must_reference_declared_principals(self):
        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            data["tokens"] = {"missing": "tok-missing"}
            path = _write_spec(data, Path(temp))

            with self.assertRaisesRegex(reconcile.SpecError, r"tokens\.missing"):
                reconcile.parse_spec(path, repo_root=Path(temp))

    def test_runtime_principals_must_be_distinct_except_agent_bot(self):
        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            data["model"]["proxy_principal"] = "p_bot"
            path = _write_spec(data, Path(temp))

            with self.assertRaisesRegex(reconcile.SpecError, r"model\.proxy_principal"):
                reconcile.parse_spec(path, repo_root=Path(temp))

    def test_session_rejects_inline_im_detail_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            data["sessions"][0]["user"]["external_id"] = "ou_1"
            path = _write_spec(data, Path(temp))

            with self.assertRaisesRegex(reconcile.SpecError, r"sessions\[0\]\.user\.external_id"):
                reconcile.parse_spec(path, repo_root=Path(temp))

        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            data["sessions"][0]["im"] = {"provider_session_id": "oc_1"}
            path = _write_spec(data, Path(temp))

            with self.assertRaisesRegex(reconcile.SpecError, r"sessions\[0\]\.im"):
                reconcile.parse_spec(path, repo_root=Path(temp))

    def test_session_rejects_nested_channel_names_and_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            data = _spec(temp)
            data["sessions"][0]["channels"] = {
                "names": {"im": "agent-demo-test.im.s1", "model": "m", "wal": "w"},
                "ids": {"model": 1, "wal": 2},
            }
            path = _write_spec(data, Path(temp))

            with self.assertRaisesRegex(reconcile.SpecError, r"sessions\[0\]\.channels\.names"):
                reconcile.parse_spec(path, repo_root=Path(temp))

    def test_print_config_uses_dry_resolution(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_spec(_spec(temp), Path(temp))

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = reconcile.main(["--spec", str(path), "--print-config", "agent"])

            self.assertEqual(result, 0)
            self.assertEqual(yaml.safe_load(stdout.getvalue())["version"], "v1")

    def test_generated_component_configs_use_config_dir(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _write_spec(_spec(temp), root)
            spec = reconcile.parse_spec(path, repo_root=root)
            resolved = reconcile.dry_resolve(spec)
            plan = reconcile.write_runtime_files(spec, resolved, dry_run=True)

            self.assertEqual(spec.paths.config_dir, root / "config")
            self.assertEqual(spec.paths.desired_path, root / "config/desired.normalized.yaml")
            self.assertEqual(spec.paths.state_path, root / "config/state.yaml")
            self.assertEqual(spec.paths.secrets_path, root / "config/secrets.yaml")
            self.assertEqual(spec.paths.plan_path, root / "config/plan.yaml")
            for item in plan["configs"].values():
                self.assertEqual(Path(item["path"]).parent, root / "config")

    def test_apply_resolution_creates_agent_bot_owned_channels(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_spec(_spec(temp), Path(temp))
            spec = reconcile.parse_spec(path, repo_root=Path(temp))
            runtime = FakeRuntime()
            actions: list[dict] = []

            tokens, _ = reconcile._resolve_principal_tokens(spec, runtime, None, None, actions)
            provider_sessions = {"s1": "oc_1"}
            channels = reconcile._resolve_channels(spec, runtime, tokens, provider_sessions, None, actions)

            self.assertEqual(channels["s1"].im, 100)
            self.assertEqual(channels["s1"].model, 101)
            self.assertEqual(channels["s1"].wal, 102)
            self.assertEqual(channels["s1"].cmd, 103)
            self.assertEqual(runtime.channels[0].creator, spec.agent_principal)
            self.assertIn(spec.agent_principal, runtime.channels[0].members)
            self.assertIn(spec.im_worker_principal, runtime.channels[0].members)
            self.assertIn(10001, runtime.channels[0].members)
            self.assertEqual(json.loads(runtime.channels[2].description)["im_channel_id"], 100)
            self.assertEqual(json.loads(runtime.channels[2].description)["model_channel_id"], 101)
            self.assertEqual(json.loads(runtime.channels[3].description)["metadata"]["cmd_worker_principal"], 30001)

    def test_existing_channel_must_be_agent_bot_owned(self):
        with tempfile.TemporaryDirectory() as temp:
            spec = reconcile.parse_spec(_write_spec(_spec(temp), Path(temp)), repo_root=Path(temp))
            runtime = FakeRuntime()
            actions: list[dict] = []
            tokens, _ = reconcile._resolve_principal_tokens(spec, runtime, None, None, actions)
            provider_sessions = {"s1": "oc_1"}

            session = spec.sessions[0]
            runtime.channels.append(
                Channel(
                    channel_id=77,
                    name=session.im_channel_name,
                    visibility=reconcile.VISIBILITY_VALUES[spec.channel_visibility],
                    protocol=reconcile.PROTOCOL_IM,
                    description=reconcile._stable_json(reconcile._im_description(spec, session, provider_sessions["s1"])),
                    creator=99000,
                    members=[session.user_principal, spec.agent_principal, spec.im_worker_principal],
                )
            )

            channels = reconcile._resolve_channels(spec, runtime, tokens, provider_sessions, None, actions)

            self.assertEqual(channels["s1"].im, 100)
            self.assertEqual(runtime.channels[-1].creator, spec.agent_principal)

    def test_restart_order_and_force_restart_downstream(self):
        with tempfile.TemporaryDirectory() as temp:
            spec = reconcile.parse_spec(_write_spec(_spec(temp), Path(temp)), repo_root=Path(temp))
            calls = []
            original = reconcile.subprocess.run

            def fake_run(args, check=False):
                calls.append(args)
                return type("Result", (), {"returncode": 0})()

            try:
                reconcile.subprocess.run = fake_run
                reconcile.restart_changed(
                    spec,
                    {
                        "configs": {
                            "model_proxy": {"changed": False},
                            "cmd_worker": {"changed": False},
                            "im_syncer": {"changed": True},
                            "agent": {"changed": False},
                        }
                    },
                    force={"model_proxy", "cmd_worker", "agent"},
                )
            finally:
                reconcile.subprocess.run = original

            self.assertEqual(
                [call[-1] for call in calls],
                ["model-proxy", "cmd-worker", "im-p2p-syncer", "im-model-agent"],
            )

    def test_ensure_program_running_starts_stopped_program(self):
        with tempfile.TemporaryDirectory() as temp:
            spec = reconcile.parse_spec(_write_spec(_spec(temp), Path(temp)), repo_root=Path(temp))
            calls = []
            original = reconcile.subprocess.run

            def fake_run(args, check=False, capture_output=False, text=False):
                calls.append(args)
                if args[1] == "status":
                    return type("Result", (), {"returncode": 3, "stdout": "STOPPED", "stderr": ""})()
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            try:
                reconcile.subprocess.run = fake_run
                reconcile.ensure_program_running(spec, "agent")
            finally:
                reconcile.subprocess.run = original

            self.assertEqual([call[1] for call in calls], ["status", "start"])


if __name__ == "__main__":
    unittest.main()
