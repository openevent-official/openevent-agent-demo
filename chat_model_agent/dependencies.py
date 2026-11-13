"""Check installed public dependencies without adding source directories."""

from importlib import import_module, metadata
import inspect
from pathlib import Path
import re


class DependencyError(RuntimeError):
    pass


def require_sdk():
    try:
        installed = metadata.distribution("openevent-sdk")
        release = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\.post\d+)?(?:\+[^\s]+)?", installed.version)
        if release is None or tuple(map(int, release.groups()[:3])) < (0, 13, 0):
            raise DependencyError("openevent-sdk >= 0.13.0 must be installed")
        sdk = import_module("openevent.sdk")
        expected = Path(installed.locate_file("openevent/sdk/__init__.py")).resolve()
        if Path(sdk.__file__).resolve() != expected:
            raise DependencyError("openevent.sdk must come from the installed distribution")
        for method in ("get_uuid", "get_seq_by_uuid", "get_status", "fetch", "get_channel",
                       "list_channels", "publish_auto_seq", "get_object_metadata", "batch_get_object_metadata", "read_object"):
            if not callable(getattr(sdk.OpenEventClient, method, None)):
                raise DependencyError("installed openevent-sdk does not provide the required public API")
        return sdk
    except DependencyError:
        raise
    except (ImportError, metadata.PackageNotFoundError, AttributeError, TypeError):
        raise DependencyError("openevent-sdk >= 0.13.0 must be installed") from None


def require_dependencies():
    require_sdk()
    requirements = (
        ("openevent-modules-chat", "openevent.chat_sdk",
         ("create_client", "parse_message", "serialize_payload", "TextPart", "TurnRef", "TurnWriter", "ChatProtocolClient")),
        ("openevent-model-proxy", "openevent.model_proxy_sdk",
         ("InferRequestInput", "InferAppend", "InferEnd", "InferCancelInput", "prepare_payload", "publish_infer_request", "publish_infer_cancel", "parse_message")),
        ("openevent-modules-cmd", "openevent.cmd_sdk",
         ("create_client", "CmdRunRequestInput", "CmdKillRequestInput", "CmdOutputReadRequestInput", "parse_message", "dumps_payload")),
    )
    modules = {}
    for package, module_name, attributes in requirements:
        try:
            metadata.distribution(package)
            module = import_module(module_name)
            if any(not callable(getattr(module, name, None)) for name in attributes):
                raise AttributeError()
            modules[module_name] = module
        except (ImportError, metadata.PackageNotFoundError, AttributeError):
            raise DependencyError(f"install the current {package} package") from None
    chat = modules["openevent.chat_sdk"]
    if not callable(getattr(chat.TurnWriter, "reset", None)):
        raise DependencyError("installed Chat SDK must support TurnWriter.reset")
    try:
        inspect.signature(chat.TurnWriter).bind(None, turn_id="turn", start_seq=1, last_seq=1,
                                               content_seqs=(1,))
        inspect.signature(chat.ChatProtocolClient.cancel_turn).bind(None, target_turn=None, start_seq=1)
        inspect.signature(chat.ChatProtocolClient.get_turn_content).bind(None, terminal_seq=1)
    except (AttributeError, TypeError, ValueError):
        raise DependencyError("installed Chat SDK must support terminal content reads and current writer/cancel state") from None
    cmd = modules["openevent.cmd_sdk"]
    for name in ("publish_run_request", "publish_kill_request", "publish_output_read_request"):
        method = getattr(cmd.CmdProtocolClient, name, None)
        if not callable(method) or "uuid" not in inspect.signature(method).parameters:
            raise DependencyError("installed Cmd SDK must support explicit publication UUIDs")
    if "object_keys" not in inspect.signature(cmd.CmdProtocolClient.publish_run_request).parameters:
        raise DependencyError("installed Cmd SDK must support command object references")
