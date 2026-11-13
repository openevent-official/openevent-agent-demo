"""Object references and metadata held with the Agent's in-memory history."""

from collections import OrderedDict
from threading import Lock


MAX_UINT64 = (1 << 64) - 1
_DESCRIPTION_CACHE_LIMIT = 256
_METADATA_BATCH_LIMIT = 1024


class ObjectIndexError(RuntimeError):
    """Reference errors deliberately omit object credentials and private paths."""


def _field(value, name):
    return value[name] if isinstance(value, dict) else getattr(value, name)


def object_key(value):
    try:
        ident, token = _field(value, "object_id"), _field(value, "object_token")
        if type(ident) is not int or not 0 < ident <= MAX_UINT64 or not isinstance(token, str) or not token:
            raise ValueError()
        token.encode("utf-8")
        return {"object_id": ident, "object_token": token}
    except (KeyError, AttributeError, TypeError, ValueError, UnicodeError):
        raise ObjectIndexError("invalid object reference") from None


def attachment_description(key, metadata):
    key = object_key(key)
    try:
        name, media_type, size = (_field(metadata, name) for name in ("name", "type", "nbytes"))
        if not isinstance(name, str) or not isinstance(media_type, str) or type(size) is not int or not 0 <= size <= MAX_UINT64:
            raise ValueError()
        name.encode("utf-8")
        media_type.encode("utf-8")
        return {"name": name, "type": media_type or "application/octet-stream", "nbytes": size,
                "object_id": key["object_id"]}
    except (KeyError, AttributeError, TypeError, ValueError, UnicodeError):
        raise ObjectIndexError("invalid object metadata") from None


class ObjectIndex:
    def __init__(self, batch_metadata_reader=None):
        self.batch_metadata_reader = batch_metadata_reader
        self._references = {}
        self._descriptions = OrderedDict()
        self._cache_lock = Lock()

    def register(self, key):
        key = object_key(key)
        with self._cache_lock:
            ident = key["object_id"]
            if ident in self._references and self._references[ident] != key:
                raise ObjectIndexError("conflicting object references")
            self._references[ident] = key

    def prepare_many(self, keys):
        keys = [object_key(key) for key in keys]
        references = [(key["object_id"], key["object_token"]) for key in keys]
        with self._cache_lock:
            descriptions = {reference: self._descriptions[reference] for reference in references
                            if reference in self._descriptions}
        missing = list(dict.fromkeys(reference for reference in references if reference not in descriptions))
        if missing and self.batch_metadata_reader is None:
            raise ObjectIndexError("object metadata reader is not configured")
        for start in range(0, len(missing), _METADATA_BATCH_LIMIT):
            batch = missing[start:start + _METADATA_BATCH_LIMIT]
            batch_keys = [{"object_id": ident, "object_token": token} for ident, token in batch]
            metadata = self.batch_metadata_reader(batch_keys)
            descriptions.update((reference, attachment_description(key, value))
                                for reference, key, value in zip(batch, batch_keys, metadata, strict=True))
        return self._remember(keys, descriptions)

    def _remember(self, keys, descriptions):
        for key in keys:
            self.register(key)
        results = []
        with self._cache_lock:
            for key in keys:
                reference = key["object_id"], key["object_token"]
                result = self._descriptions.setdefault(reference, descriptions[reference])
                self._descriptions.move_to_end(reference)
                while len(self._descriptions) > _DESCRIPTION_CACHE_LIMIT:
                    self._descriptions.popitem(last=False)
                results.append(dict(result))
        return results

    def get(self, object_id):
        if type(object_id) is not int or not 0 < object_id <= MAX_UINT64:
            raise ObjectIndexError("invalid object ID")
        with self._cache_lock:
            try:
                return dict(self._references[object_id])
            except KeyError:
                raise ObjectIndexError("object ID is not registered") from None
