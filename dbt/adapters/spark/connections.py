from contextlib import contextmanager

from dbt.adapters.base import Credentials
from dbt.adapters.sql import SQLConnectionManager
from dbt.logger import GLOBAL_LOGGER as logger

from TCLIService.ttypes import TOperationState as ThriftState
from thrift.transport import THttpClient
from pyhive import hive

from hologram.helpers import StrEnum
from dataclasses import dataclass
from typing import Optional

import dbt.exceptions
import decimal
import base64
import time


class SparkConnectionMethod(StrEnum):
    THRIFT = 'thrift'
    HTTP = 'http'


@dataclass
class SparkCredentials(Credentials):
    host: str
    method: SparkConnectionMethod
    schema: str
    cluster: Optional[str]
    token: Optional[str]
    user: Optional[str]
    database: Optional[str]
    port: int = 443
    organization: str = '0'
    connect_retries: int = 0
    connect_timeout: int = 10

    def __post_init__(self):
        # spark classifies database and schema as the same thing
        self.database = self.schema

    @property
    def type(self):
        return 'spark'

    def _connection_keys(self):
        return 'host', 'port', 'cluster', 'schema', 'organization'


class ConnectionWrapper(object):
    """Wrap a Spark connection in a way that no-ops transactions"""
    # https://forums.databricks.com/questions/2157/in-apache-spark-sql-can-we-roll-back-the-transacti.html

    def __init__(self, handle):
        self.handle = handle
        self._cursor = None

    def cursor(self):
        self._cursor = self.handle.cursor()
        return self

    def cancel(self):
        if self._cursor:
            # Handle bad response in the pyhive lib when
            # the connection is cancelled
            try:
                self._cursor.cancel()
            except EnvironmentError as exc:
                logger.debug(
                    "Exception while cancelling query: {}".format(exc)
                )

    def close(self):
        if self._cursor:
            # Handle bad response in the pyhive lib when
            # the connection is cancelled
            try:
                self._cursor.close()
            except EnvironmentError as exc:
                logger.debug(
                    "Exception while closing cursor: {}".format(exc)
                )

    def rollback(self, *args, **kwargs):
        logger.debug("NotImplemented: rollback")

    def fetchall(self):
        return self._cursor.fetchall()

    def execute(self, sql, bindings=None):
        if sql.strip().endswith(";"):
            sql = sql.strip()[:-1]

        # Reaching into the private enumeration here is bad form,
        # but there doesn't appear to be any way to determine that
        # a query has completed executing from the pyhive public API.
        # We need to use an async query + poll here, otherwise our
        # request may be dropped after ~5 minutes by the thrift server
        STATE_PENDING = [
            ThriftState.INITIALIZED_STATE,
            ThriftState.RUNNING_STATE,
            ThriftState.PENDING_STATE,
        ]

        STATE_SUCCESS = [
            ThriftState.FINISHED_STATE,
        ]

        # Convert decimal.Decimal to float as PyHive doesn't work with decimals
        if bindings:
            bindings = [float(x) if isinstance(x, decimal.Decimal) else x for x in bindings]

        self._cursor.execute(sql, bindings, async_=True)
        poll_state = self._cursor.poll()
        state = poll_state.operationState

        while state in STATE_PENDING:
            logger.debug("Poll status: {}, sleeping".format(state))

            poll_state = self._cursor.poll()
            state = poll_state.operationState

        # If an errorMessage is present, then raise a database exception
        # with that exact message. If no errorMessage is present, the
        # query did not necessarily succeed: check the state against the
        # known successful states, raising an error if the query did not
        # complete in a known good state. This can happen when queries are
        # cancelled, for instance. The errorMessage will be None, but the
        # state of the query will be "cancelled". By raising an exception
        # here, we prevent dbt from showing a status of OK when the query
        # has in fact failed.
        if poll_state.errorMessage:
            logger.debug("Poll response: {}".format(poll_state))
            logger.debug("Poll status: {}".format(state))
            raise dbt.exceptions.raise_database_error(poll_state.errorMessage)

        elif state not in STATE_SUCCESS:
            status_type = ThriftState._VALUES_TO_NAMES.get(
                state,
                'Unknown<{!r}>'.format(state))

            raise dbt.exceptions.raise_database_error(
                "Query failed with status: {}".format(status_type))

        logger.debug("Poll status: {}, query complete".format(state))

    @property
    def description(self):
        return self._cursor.description


class SparkConnectionManager(SQLConnectionManager):
    TYPE = 'spark'

    SPARK_CONNECTION_URL = "https://{host}:{port}/sql/protocolv1/o/{organization}/{cluster}"

    @contextmanager
    def exception_handler(self, sql):
        try:
            yield

        except Exception as exc:
            logger.debug("Error while running:\n{}".format(sql))
            logger.debug(exc)
            if len(exc.args) == 0:
                raise

            thrift_resp = exc.args[0]
            if hasattr(thrift_resp, 'status'):
                msg = thrift_resp.status.errorMessage
                raise dbt.exceptions.RuntimeException(msg)
            else:
                raise dbt.exceptions.RuntimeException(str(exc))

    def cancel(self, connection):
        connection.handle.cancel()

    @classmethod
    def get_status(cls, cursor):
        return 'OK'

    # No transactions on Spark....
    def add_begin_query(self, *args, **kwargs):
        logger.debug("NotImplemented: add_begin_query")

    def add_commit_query(self, *args, **kwargs):
        logger.debug("NotImplemented: add_commit_query")

    def commit(self, *args, **kwargs):
        logger.debug("NotImplemented: commit")

    def rollback(self, *args, **kwargs):
        logger.debug("NotImplemented: rollback")

    @classmethod
    def validate_creds(cls, creds, required):
        method = creds.method

        for key in required:
            if not hasattr(creds, key):
                raise dbt.exceptions.DbtProfileError(
                    "The config '{}' is required when using the {} method"
                    " to connect to Spark".format(key, method))

    @classmethod
    def open(cls, connection):
        if connection.state == 'open':
            logger.debug('Connection is already open, skipping open.')
            return connection

        creds = connection.credentials
        exc = None

        for i in range(1 + creds.connect_retries):
            try:
                if creds.method == 'http':
                    cls.validate_creds(creds, ['host', 'port', 'cluster', 'organization', 'token', 'schema'])

                    conn_url = cls.SPARK_CONNECTION_URL.format(
                        host=creds.host,
                        port=creds.port,
                        organization=creds.organization,
                        cluster=creds.cluster
                    )

                    logger.debug("connection url: {}".format(conn_url))

                    transport = THttpClient.THttpClient(conn_url)

                    raw_token = "token:{}".format(creds.token).encode()
                    token = base64.standard_b64encode(raw_token).decode()
                    transport.setCustomHeaders({
                        'Authorization': 'Basic {}'.format(token)
                    })

                    conn = hive.connect(thrift_transport=transport)
                elif creds.method == 'thrift':
                    cls.validate_creds(creds, ['host', 'port', 'user', 'schema'])

                    conn = hive.connect(host=creds.host,
                                        port=creds.port,
                                        username=creds.user)
                else:
                    raise dbt.exceptions.DbtProfileError("invalid credential method: {}".format(creds.method))
                break
            except Exception as e:
                exc = e
                if getattr(e, 'message', None) is None:
                    raise dbt.exceptions.FailedToConnectException(str(e))

                message = e.message.lower()
                is_pending = 'pending' in message
                is_starting = 'temporarily_unavailable' in message

                warning = "Warning: {}\n\tRetrying in {} seconds ({} of {})"
                if is_pending or is_starting:
                    logger.warning(warning.format(e.message, creds.connect_timeout,
                                                  i, creds.connect_retries))
                    time.sleep(creds.connect_timeout)
                else:
                    raise dbt.exceptions.FailedToConnectException(str(e))
        else:
            raise exc

        handle = ConnectionWrapper(conn)
        connection.handle = handle
        connection.state = 'open'
        return connection
