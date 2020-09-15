# This file is part of the PostgreSQL k8s Charm for Juju.
# Copyright 2020 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3, as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranties of
# MERCHANTABILITY, SATISFACTORY QUALITY, or FITNESS FOR A PARTICULAR
# PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import functools
import logging
import re
from typing import Any, Iterable

import psycopg2
import psycopg2.extensions
from tenacity import before_log, retry, retry_if_exception_type, stop_after_delay, wait_random_exponential

from connstr import ConnectionString


log = logging.getLogger(__name__)

PGConnection = psycopg2.extensions.connection


@retry(
    retry=retry_if_exception_type(psycopg2.OperationalError),
    stop=stop_after_delay(300),
    wait=wait_random_exponential(multiplier=1, max=15),
    reraise=True,
    before=before_log(log, logging.DEBUG),
)
def connect(conn_str: ConnectionString) -> PGConnection:
    con = psycopg2.connect(str(conn_str))
    con.autocommit = True
    return con


def ensure_user(con: PGConnection, username: str, password: str, superuser: bool = False, replication: bool = False):
    if role_exists(con, username):
        cmd = ["ALTER ROLE"]
    else:
        cmd = ["CREATE ROLE"]
    cmd.append("%s WITH LOGIN")
    cmd.append("SUPERUSER" if superuser else "NOSUPERUSER")
    cmd.append("REPLICATION" if replication else "NOREPLICATION")
    cmd.append("PASSWORD %s")
    cur = con.cursor()
    cur.execute(" ".join(cmd), (pgidentifier(username), password))


def ensure_roles(con: PGConnection, roles: Iterable[str]):
    for role in roles:
        ensure_role(con, role)


def ensure_role(con: PGConnection, role: str):
    cur = con.cursor()
    cur.execute("SELECT TRUE FROM pg_roles WHERE rolname=%s", (role,))
    if cur.fetchone() is None:
        cur.execute("CREATE ROLE %s INHERIT NOLOGIN", (pgidentifier(role),))


def role_exists(con: PGConnection, role: str) -> bool:
    cur = con.cursor()
    cur.execute("SELECT TRUE FROM pg_roles WHERE rolname=%s", (role,))
    return cur.fetchone() is not None


def grant_user_roles(con: PGConnection, username: str, roles: Iterable[str]):
    wanted_roles = set(roles)

    cur = con.cursor()
    cur.execute(
        """
        SELECT role.rolname FROM pg_roles AS role, pg_roles AS member, pg_auth_members
        WHERE member.oid = pg_auth_members.member AND role.oid = pg_auth_members.roleid AND member.rolname = %s
        """,
        (username,),
    )
    existing_roles = set(r[0] for r in cur.fetchall())

    roles_to_grant = wanted_roles.difference(existing_roles)

    if roles_to_grant:
        log.info("Granting {} to {}".format(",".join(roles_to_grant), username))
        for role in roles_to_grant:
            ensure_role(con, role)
            cur.execute("GRANT %s TO %s", (pgidentifier(role), pgidentifier(username)))


def ensure_db(con: PGConnection, dbname: str, ownername: str):
    cur = con.cursor()
    cur.execute("SELECT datname FROM pg_database WHERE datname=%s", (dbname,))
    if cur.fetchone() is None:
        cur.execute("CREATE DATABASE %s OWNER %s", (pgidentifier(dbname), pgidentifier(ownername)))
    else:
        cur.execute("ALTER DATABASE %s OWNER TO %s", (pgidentifier(dbname), pgidentifier(ownername)))
    grant_database_privileges(con, ownername, dbname, ["ALL"])


def grant_database_privileges(con: PGConnection, role: str, dbname: str, privs: Iterable[str]):
    cur = con.cursor()
    for priv in privs:
        cur.execute("GRANT %s ON DATABASE %s TO %s", (AsIs(priv), pgidentifier(dbname), pgidentifier(role)))


def ensure_extensions(con, extensions: Iterable[str]):
    """extensions in format defined in config.yaml"""

    # Convert extensions to (name, schema) tuples
    extensions = list(extensions)
    for i in range(0, len(extensions)):
        m = re.search(r"^\s*([^(\s]+)\s*(?:\((\w+)\))?", extensions[i])
        if m is None:
            raise RuntimeError("Invalid extension {}".format(extensions[i]))
        extensions[i] = (m.group(1), m.group(2) or "public")

    cur = con.cursor()
    cur.execute("SELECT extname,nspname FROM pg_extension,pg_namespace WHERE pg_namespace.oid = extnamespace")
    installed_extensions = frozenset((x[0], x[1]) for x in cur.fetchall())
    log.debug(f"ensure_extensions({extensions}), have {installed_extensions}")
    extensions_set = frozenset(set(extensions))
    extensions_to_create = extensions_set.difference(installed_extensions)
    for ext, schema in extensions_to_create:
        log.info(f"Creating extension {ext}")
        if schema != "public":
            cur.execute("CREATE SCHEMA IF NOT EXISTS %s", (pgidentifier(schema),))
            cur.execute("GRANT USAGE ON SCHEMA %s TO PUBLIC", (pgidentifier(schema),))
        cur.execute("CREATE EXTENSION %s WITH SCHEMA %s", (pgidentifier(ext), pgidentifier(schema)))


@functools.total_ordering
class AsIs(psycopg2.extensions.ISQLQuote):
    """An extension of psycopg2.extensions.AsIs

    The comparison operators make it usable in unittests and
    stable no matter the psycopg2 version.
    """

    def getquoted(self):
        return str(self._wrapped).encode("UTF8")

    def __conform__(self, protocol: Any):
        if protocol is psycopg2.extensions.ISQLQuote:
            return self

    def __eq__(self, other: Any):
        return self._wrapped == other

    def __lt__(self, other: Any):
        return self._wrapped < other

    def __str__(self):
        return str(self._wrapped)

    def __repr__(self):
        return "{}({!r})".format(self.__class__.__name__, self._wrapped)


def quote_identifier(identifier: str):
    r'''Quote an identifier, such as a table or role name.

    In SQL, identifiers are quoted using " rather than ' (which is reserved
    for strings).

    >>> print(quote_identifier('hello'))
    "hello"

    Quotes and Unicode are handled if you make use of them in your
    identifiers.

    >>> print(quote_identifier("'"))
    "'"
    >>> print(quote_identifier('"'))
    """"
    >>> print(quote_identifier("\\"))
    "\"
    >>> print(quote_identifier('\\"'))
    "\"""
    >>> print(quote_identifier('\\ aargh \u0441\u043b\u043e\u043d'))
    U&"\\ aargh \0441\043b\043e\043d"
    '''
    try:
        identifier.encode("US-ASCII")
        return '"{}"'.format(identifier.replace('"', '""'))
    except UnicodeEncodeError:
        escaped = []
        for c in identifier:
            if c == "\\":
                escaped.append("\\\\")
            elif c == '"':
                escaped.append('""')
            else:
                c = c.encode("US-ASCII", "backslashreplace").decode("US-ASCII")
                # Note Python only supports 32 bit unicode, so we use
                # the 4 hexdigit PostgreSQL syntax (\1234) rather than
                # the 6 hexdigit format (\+123456).
                if c.startswith("\\u"):
                    c = "\\" + c[2:]
                escaped.append(c)
        return 'U&"%s"' % "".join(escaped)


def pgidentifier(token: str):
    """Wrap a string for interpolation by psycopg2 as an SQL identifier"""
    return AsIs(quote_identifier(token))
