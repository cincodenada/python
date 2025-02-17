# Copyright (c) 2021 Tulir Asokan
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager
from urllib.parse import urlparse
import asyncio
import logging
import sqlite3

import aiosqlite

from .upgrade import UpgradeTable
from .database import Database


class TxnConnection(aiosqlite.Connection):
    def __init__(self, path: str, **kwargs) -> None:
        def connector() -> sqlite3.Connection:
            return sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES,
                                   isolation_level=None, **kwargs)

        super().__init__(connector, iter_chunk_size=64)

    @asynccontextmanager
    async def transaction(self) -> None:
        await self.execute("BEGIN TRANSACTION")
        try:
            yield
        except Exception:
            await self.rollback()
            raise
        else:
            await self.commit()

    async def execute(self, query: str, *args: Any, timeout: Optional[float] = None) -> None:
        await super().execute(query, args)

    async def fetch(self, query: str, *args: Any, timeout: Optional[float] = None
                    ) -> List[sqlite3.Row]:
        async with super().execute(query, args) as cursor:
            return list(await cursor.fetchall())

    async def fetchrow(self, query: str, *args: Any, timeout: Optional[float] = None
                       ) -> sqlite3.Row:
        async with super().execute(query, args) as cursor:
            return await cursor.fetchone()

    async def fetchval(self, query: str, *args: Any, column: int = 0,
                       timeout: Optional[float] = None) -> Any:
        row = await self.fetchrow(query, *args)
        if row is None:
            return None
        return row[column]


class SQLiteDatabase(Database):
    scheme = "sqlite"
    _pool: 'asyncio.Queue[TxnConnection]'
    _stopped: bool
    _conns: int

    def __init__(self, url: str, upgrade_table: UpgradeTable,
                 db_args: Optional[Dict[str, Any]] = None,
                 log: Optional[logging.Logger] = None) -> None:
        super().__init__(url, db_args=db_args, upgrade_table=upgrade_table, log=log)
        self._path = urlparse(url).path
        if self._path.startswith("/"):
            self._path = self._path[1:]
        self._pool = asyncio.Queue(self._db_args.pop("min_size", 5))
        self._db_args.pop("max_size", None)
        self._stopped = False
        self._conns = 0

    async def start(self) -> None:
        self.log.debug(f"Connecting to {self.url}")
        for _ in range(self._pool.maxsize):
            conn = await TxnConnection(self._path, **self._db_args)
            conn.row_factory = sqlite3.Row
            self._pool.put_nowait(conn)
            self._conns += 1
        await super().start()

    async def stop(self) -> None:
        self._stopped = True
        while self._conns > 0:
            conn = await self._pool.get()
            self._conns -= 1
            await conn.close()

    @asynccontextmanager
    async def acquire(self) -> TxnConnection:
        if self._stopped:
            raise RuntimeError("database pool has been stopped")
        conn = await self._pool.get()
        try:
            yield conn
        finally:
            self._pool.put_nowait(conn)


Database.schemes["sqlite"] = SQLiteDatabase
Database.schemes["sqlite3"] = SQLiteDatabase
