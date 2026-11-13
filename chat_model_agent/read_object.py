"""Read an object attached to this command without printing its credentials."""

import argparse
import json
import os
import sys

from .dependencies import require_sdk
from .objects import MAX_UINT64, ObjectIndex, ObjectIndexError


# OpenEvent objects have a fixed 4 MiB limit; ReadObject clips to the actual size.
_OBJECT_READ_BYTES = 4 * 1024 * 1024


class ObjectReadError(ValueError):
    """Fixed error descriptions safe to show without credentials or private paths."""


def command_object(object_id):
    try:
        keys = json.loads(os.environ["OPENEVENT_OBJECT_KEYS"])
        if not isinstance(keys, list):
            raise ValueError()
        index = ObjectIndex()
        for key in keys:
            index.register(key)
    except (KeyError, ValueError, TypeError, ObjectIndexError):
        raise ObjectReadError("invalid command object references") from None
    return index.get(object_id)


def copy_object(client, key, output):
    try:
        result = client.read_object(key["object_id"], key["object_token"], 0, _OBJECT_READ_BYTES)
    except Exception:
        raise ObjectReadError("could not read object content") from None
    data = getattr(result, "data", None)
    if not isinstance(data, bytes) or not 0 < len(data) <= _OBJECT_READ_BYTES:
        raise ObjectReadError("incomplete or invalid object content")
    try:
        output.write(data)
        output.flush()
    except BrokenPipeError:
        raise
    except OSError:
        raise ObjectReadError("could not write object content to stdout") from None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="openevent-read-object", description="Write an attached object's original bytes to stdout")
    parser.add_argument("object_id")
    args = parser.parse_args(argv)
    failure = "invalid object ID"
    try:
        object_id = int(args.object_id)
        if not 0 < object_id <= MAX_UINT64:
            raise ValueError()
        failure = "object reader environment is not configured"
        target = os.environ["OPENEVENT_OBJECT_TARGET"]
        if not target or "OPENEVENT_OBJECT_KEYS" not in os.environ:
            raise ValueError()
        key = command_object(object_id)
        failure = "OpenEvent SDK is unavailable or incompatible"
        sdk = require_sdk()
        failure = "could not connect to object service"
        with sdk.OpenEventClient(target) as client:
            copy_object(client, key, sys.stdout.buffer)
        return 0
    except BrokenPipeError:
        return 1
    except (ObjectIndexError, ObjectReadError) as error:
        failure = str(error)
    except Exception:
        pass
    print(f"openevent-read-object: {failure}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
