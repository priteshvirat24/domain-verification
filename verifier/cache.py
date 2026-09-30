"""Persistent domain-level cache with TTL and Actor-compatible interface."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import fields
from pathlib import Path

from .models import DomainRecord, Evidence, FetchRecord


def cache_key(domain: str) -> str:
    return "DOMAIN_" + hashlib.sha256(domain.encode()).hexdigest()


def url_key(url: str) -> str:
    return "URL_" + hashlib.sha256(url.encode()).hexdigest()


def pair_key(*parts: str) -> str:
    return "PAIR_" + hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _record(data: dict) -> DomainRecord:
    valid = {f.name for f in fields(DomainRecord)}
    return DomainRecord(**{k: v for k, v in data.items() if k in valid})


class SQLiteCache:
    def __init__(self, path: str | Path, ttl_seconds: int):
        self.db = sqlite3.connect(path, timeout=60.0)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("create table if not exists domains (domain text primary key, timestamp real not null, payload text not null)")
        self.db.execute("create table if not exists urls (url text primary key, timestamp real not null, payload text not null)")
        self.db.execute("create table if not exists pairs (pair_key text primary key, timestamp real not null, payload text not null)")
        self.db.commit()
        self.ttl_seconds = ttl_seconds

    async def get(self, domain: str) -> DomainRecord | None:
        row = self.db.execute("select timestamp,payload from domains where domain=?", (domain,)).fetchone()
        return _record(json.loads(row[1])) if row and time.time() - row[0] <= self.ttl_seconds else None

    async def put(self, record: DomainRecord) -> None:
        self.db.execute("insert or replace into domains values (?,?,?)",
                        (record.domain, time.time(), json.dumps(record.to_dict(), ensure_ascii=False)))
        self.db.commit()

    async def get_url(self, url: str) -> FetchRecord | None:
        row = self.db.execute("select timestamp,payload from urls where url=?", (url,)).fetchone()
        return FetchRecord(**json.loads(row[1])) if row and time.time() - row[0] <= self.ttl_seconds else None

    async def put_url(self, record: FetchRecord) -> None:
        from dataclasses import asdict
        self.db.execute("insert or replace into urls values (?,?,?)",
                        (record.requested_url, time.time(), json.dumps(asdict(record), ensure_ascii=False)))
        self.db.commit()

    async def get_pair(self, key: tuple[str, ...]) -> list[Evidence] | None:
        row = self.db.execute("select timestamp,payload from pairs where pair_key=?", (pair_key(*key),)).fetchone()
        return [Evidence(**x) for x in json.loads(row[1])] if row and time.time() - row[0] <= self.ttl_seconds else None

    async def put_pair(self, key: tuple[str, ...], evidence: list[Evidence]) -> None:
        self.db.execute("insert or replace into pairs values (?,?,?)",
                        (pair_key(*key), time.time(), json.dumps([e.to_dict() for e in evidence], ensure_ascii=False)))
        self.db.commit()

    def close(self) -> None:
        self.db.close()


class ApifyCache:
    def __init__(self, store, ttl_seconds: int):
        self.store = store
        self.ttl_seconds = ttl_seconds

    async def get(self, domain: str) -> DomainRecord | None:
        value = await self.store.get_value(cache_key(domain))
        if not value or time.time() - value.get("timestamp", 0) > self.ttl_seconds:
            return None
        return _record(value["record"])

    async def put(self, record: DomainRecord) -> None:
        await self.store.set_value(cache_key(record.domain),
                                   {"timestamp": time.time(), "record": record.to_dict()})

    async def get_url(self, url: str) -> FetchRecord | None:
        value = await self.store.get_value(url_key(url))
        if not value or time.time() - value.get("timestamp", 0) > self.ttl_seconds:
            return None
        return FetchRecord(**value["record"])

    async def put_url(self, record: FetchRecord) -> None:
        from dataclasses import asdict
        await self.store.set_value(url_key(record.requested_url),
                                   {"timestamp": time.time(), "record": asdict(record)})

    async def get_pair(self, key: tuple[str, ...]) -> list[Evidence] | None:
        value = await self.store.get_value(pair_key(*key))
        if not value or time.time() - value.get("timestamp", 0) > self.ttl_seconds:
            return None
        return [Evidence(**x) for x in value["evidence"]]

    async def put_pair(self, key: tuple[str, ...], evidence: list[Evidence]) -> None:
        await self.store.set_value(pair_key(*key),
                                   {"timestamp": time.time(), "evidence": [e.to_dict() for e in evidence]})
