#!/usr/bin/env python3
"""Crash-safe file writes for the pipeline.

A direct ``open(path, "w").write(...)`` truncates the target file first, so a
process killed mid-write (a workflow ``timeout-minutes`` firing, a runner
eviction, an OOM) leaves a truncated or empty file. For opinions.json or
opinions_state.json that means the next run fails to parse it and the pipeline
stalls until the file is repaired by hand.

These helpers write to a temporary file in the same directory, flush and fsync
it, then ``os.replace`` it over the target. ``os.replace`` is atomic on the same
filesystem, so a reader (or the next run) always sees either the old complete
file or the new complete file, never a half-written one. Pure standard library,
no dependencies, so it stays a safe leaf import for every other module.
"""
import os, json, tempfile


def atomic_write_text(path, text, encoding="utf-8"):
    """Write text to path atomically (temp file in the same dir, then replace)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".swp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, obj, ensure_ascii=False, indent=2):
    """Serialize obj to JSON and write it atomically.

    Byte-for-byte identical to json.dump(obj, f, ensure_ascii=..., indent=...),
    so converting an existing direct dump to this produces no spurious diff.
    """
    atomic_write_text(path, json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent))
