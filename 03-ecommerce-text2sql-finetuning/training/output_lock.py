"""Exclusive local output ownership. Stale locks require explicit inspection."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import time
import uuid


@contextmanager
def exclusive_output(directory, name='.pipeline.lock'):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / name
    token = uuid.uuid4().hex
    try:
        handle = lock.open('x', encoding='utf-8')
    except FileExistsError:
        raise RuntimeError(f'output is already locked: {lock}; inspect owner before removing a stale lock') from None
    try:
        with handle:
            json.dump({'pid': os.getpid(), 'host': socket.gethostname(), 'token': token,
                       'created_at': time.time()}, handle)
        yield
    finally:
        # Never unlink a lock replaced by another actor.
        try:
            if json.loads(lock.read_text(encoding='utf-8')).get('token') == token:
                lock.unlink()
        except (FileNotFoundError, ValueError):
            pass
