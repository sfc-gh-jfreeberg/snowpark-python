#!/usr/bin/env python3
#
# Copyright (c) 2012-2023 Snowflake Computing Inc. All rights reserved.
#

import functools
import os
import sys
import time
from copy import copy
from logging import getLogger
from typing import IO, Any, Dict, Iterable, Iterator, List, Optional, Tuple, Union
from unittest.mock import Mock

import pandas as pd

import snowflake.connector
from snowflake.connector.constants import FIELD_ID_TO_NAME
from snowflake.connector.cursor import ResultMetadata, SnowflakeCursor
from snowflake.connector.errors import NotSupportedError, ProgrammingError
from snowflake.connector.network import ReauthenticationRequest
from snowflake.connector.options import pandas
from snowflake.snowpark._internal.analyzer.analyzer_utils import (
    escape_quotes,
    quote_name_without_upper_casing,
)
from snowflake.snowpark._internal.analyzer.expression import Attribute
from snowflake.snowpark._internal.analyzer.snowflake_plan import (
    BatchInsertQuery,
    SnowflakePlan,
)
from snowflake.snowpark._internal.analyzer.snowflake_plan_node import SaveMode
from snowflake.snowpark._internal.error_message import SnowparkClientExceptionMessages
from snowflake.snowpark._internal.utils import (
    is_in_stored_procedure,
    normalize_local_file,
    result_set_to_rows,
    unwrap_stage_location_single_quote,
)
from snowflake.snowpark.async_job import AsyncJob, _AsyncResultType
from snowflake.snowpark.exceptions import SnowparkSQLException
from snowflake.snowpark.mock.plan import MockExecutionPlan, execute_mock_plan
from snowflake.snowpark.mock.snowflake_data_type import TableEmulator
from snowflake.snowpark.mock.util import parse_table_name
from snowflake.snowpark.row import Row

logger = getLogger(__name__)

# set `paramstyle` to qmark for batch insertion
snowflake.connector.paramstyle = "qmark"

# parameters needed for usage tracking
PARAM_APPLICATION = "application"
PARAM_INTERNAL_APPLICATION_NAME = "internal_application_name"
PARAM_INTERNAL_APPLICATION_VERSION = "internal_application_version"


def _build_put_statement(*args, **kwargs):
    raise NotImplementedError()


def _build_target_path(stage_location: str, dest_prefix: str = "") -> str:
    qualified_stage_name = unwrap_stage_location_single_quote(stage_location)
    dest_prefix_name = (
        dest_prefix
        if not dest_prefix or dest_prefix.startswith("/")
        else f"/{dest_prefix}"
    )
    return f"{qualified_stage_name}{dest_prefix_name if dest_prefix_name else ''}"


class MockServerConnection:
    class TableRegistry:
        # Table registry. TODO: move to datastore
        def __init__(self, conn: "MockServerConnection") -> None:
            self.table_registry = {}
            self.conn = conn

        def get_fully_qualified_name(self, name: Union[str, Iterable[str]]) -> str:
            def uppercase_and_enquote_if_not_quoted(string):
                if (
                    len(string) > 2 and string[0] == '"' and string[-1] == '"'
                ):  # already quoted
                    return string
                string = string.replace('"', '""')
                return f'"{string.upper()}"'

            current_schema = self.conn._get_current_parameter("schema")
            current_database = self.conn._get_current_parameter("database")
            if isinstance(name, str):
                name = parse_table_name(name)
            if len(name) == 1:
                name = [current_schema] + name
            if len(name) == 2:
                name = [current_database] + name
            return ".".join(uppercase_and_enquote_if_not_quoted(n) for n in name)

        def read_table(self, name: Union[str, Iterable[str]]) -> TableEmulator:
            name = self.get_fully_qualified_name(name)
            if name in self.table_registry:
                return copy(self.table_registry[name])
            else:
                raise SnowparkSQLException(
                    f"Table {name} does not exist"
                )  # TODO: match exception message

        def write_table(
            self, name: Union[str, Iterable[str]], table: TableEmulator, mode: SaveMode
        ) -> Row:
            name = self.get_fully_qualified_name(name)
            table = copy(table)
            if mode == SaveMode.APPEND:
                # Fix append by index
                if name in self.table_registry:
                    target_table = self.table_registry[name]
                    table.columns = target_table.columns
                    self.table_registry[name] = pd.concat([target_table, table])
                else:
                    self.table_registry[name] = table
            elif mode == SaveMode.IGNORE:
                if name not in self.table_registry:
                    self.table_registry[name] = table
            elif mode == SaveMode.OVERWRITE:
                self.table_registry[name] = table
            elif mode == SaveMode.ERROR_IF_EXISTS:
                if name in self.table_registry:
                    raise SnowparkSQLException(f"Table {name} already exists")
                else:
                    self.table_registry[name] = table
            else:
                raise ProgrammingError(f"Unrecognized mode: {mode}")
            return [
                Row(status=f"Table {name} successfully created.")
            ]  # TODO: match message

        def drop_table(self, name: Union[str, Iterable[str]]) -> None:
            name = self.get_fully_qualified_name(name)
            if name in self.table_registry:
                self.table_registry.pop(name)

    class _Decorator:
        @classmethod
        def wrap_exception(cls, func):
            def wrap(*args, **kwargs):
                try:
                    return func(*args, **kwargs)
                except ReauthenticationRequest as ex:
                    raise SnowparkClientExceptionMessages.SERVER_SESSION_EXPIRED(
                        ex.cause
                    )
                except Exception as ex:
                    raise ex

            return wrap

        @classmethod
        def log_msg_and_perf_telemetry(cls, msg):
            def log_and_telemetry(func):
                @functools.wraps(func)
                def wrap(*args, **kwargs):
                    logger.debug(msg)
                    start_time = time.perf_counter()
                    result = func(*args, **kwargs)
                    end_time = time.perf_counter()
                    duration = end_time - start_time
                    sfqid = result["sfqid"] if result and "sfqid" in result else None
                    # If we don't have a query id, then its pretty useless to send perf telemetry
                    if sfqid:
                        args[0]._telemetry_client.send_upload_file_perf_telemetry(
                            func.__name__, duration, sfqid
                        )
                    logger.debug(f"Finished in {duration:.4f} secs")
                    return result

                return wrap

            return log_and_telemetry

    def __init__(self) -> None:
        self._conn = Mock()
        self.remove_query_listener = Mock()
        self.add_query_listener = Mock()
        self._telemetry_client = Mock()
        self.table_registry = MockServerConnection.TableRegistry(self)

    def get_session_id(self) -> int:
        return 1

    def close(self) -> None:
        if self._conn:
            self._conn.close()

    def is_closed(self) -> bool:
        return self._conn.is_closed()

    @_Decorator.wrap_exception
    def _get_current_parameter(self, param: str, quoted: bool = True) -> Optional[str]:
        name = getattr(self._conn, param) or self._get_string_datum(
            f"SELECT CURRENT_{param.upper()}()"
        )
        if param == "database":
            return '"mock_database"'
        if param == "schema":
            return '"mock_schema"'
        if param == "warehouse":
            return '"mock_warehouse"'
        return (
            (quote_name_without_upper_casing(name) if quoted else escape_quotes(name))
            if name
            else None
        )

    def _get_string_datum(self, query: str) -> Optional[str]:
        rows = result_set_to_rows(self.run_query(query)["data"])
        return rows[0][0] if len(rows) > 0 else None

    # @SnowflakePlan.Decorator.wrap_exception
    # def get_result_attributes(self, query: str) -> List[Attribute]:
    #     return convert_result_meta_to_attribute(self._cursor.describe(query))

    @_Decorator.log_msg_and_perf_telemetry("Uploading file to stage")
    def upload_file(
        self,
        path: str,
        stage_location: str,
        dest_prefix: str = "",
        parallel: int = 4,
        compress_data: bool = True,
        source_compression: str = "AUTO_DETECT",
        overwrite: bool = False,
    ) -> Optional[Dict[str, Any]]:
        if is_in_stored_procedure():  # pragma: no cover
            file_name = os.path.basename(path)
            target_path = _build_target_path(stage_location, dest_prefix)
            try:
                # upload_stream directly consume stage path, so we don't need to normalize it
                self._cursor.upload_stream(
                    open(path, "rb"), f"{target_path}/{file_name}"
                )
            except ProgrammingError as pe:
                tb = sys.exc_info()[2]
                ne = SnowparkClientExceptionMessages.SQL_EXCEPTION_FROM_PROGRAMMING_ERROR(
                    pe
                )
                raise ne.with_traceback(tb) from None
        else:
            uri = normalize_local_file(path)
            return self.run_query(
                _build_put_statement(
                    uri,
                    stage_location,
                    dest_prefix,
                    parallel,
                    compress_data,
                    source_compression,
                    overwrite,
                )
            )

    @_Decorator.log_msg_and_perf_telemetry("Uploading stream to stage")
    def upload_stream(
        self,
        input_stream: IO[bytes],
        stage_location: str,
        dest_filename: str,
        dest_prefix: str = "",
        parallel: int = 4,
        compress_data: bool = True,
        source_compression: str = "AUTO_DETECT",
        overwrite: bool = False,
        is_in_udf: bool = False,
    ) -> Optional[Dict[str, Any]]:
        raise NotImplementedError(
            "[Local Testing] PUT stream is currently not supported."
        )

    @_Decorator.wrap_exception
    def run_query(
        self,
        query: str,
        to_pandas: bool = False,
        to_iter: bool = False,
        is_ddl_on_temp_object: bool = False,
        block: bool = True,
        data_type: _AsyncResultType = _AsyncResultType.ROW,
        async_job_plan: Optional[
            SnowflakePlan
        ] = None,  # this argument is currently only used by AsyncJob
        **kwargs,
    ) -> Union[Dict[str, Any], AsyncJob]:
        raise NotImplementedError(
            "[Local Testing] Running SQL queries is not supported."
        )

    def _to_data_or_iter(
        self,
        results_cursor: SnowflakeCursor,
        to_pandas: bool = False,
        to_iter: bool = False,
    ) -> Dict[str, Any]:
        if to_pandas:
            try:
                data_or_iter = (
                    map(
                        functools.partial(
                            _fix_pandas_df_integer, results_cursor=results_cursor
                        ),
                        results_cursor.fetch_pandas_batches(),
                    )
                    if to_iter
                    else _fix_pandas_df_integer(
                        results_cursor.fetch_pandas_all(), results_cursor
                    )
                )
            except NotSupportedError:
                data_or_iter = (
                    iter(results_cursor) if to_iter else results_cursor.fetchall()
                )
            except KeyboardInterrupt:
                raise
            except BaseException as ex:
                raise SnowparkClientExceptionMessages.SERVER_FAILED_FETCH_PANDAS(
                    str(ex)
                )
        else:
            data_or_iter = (
                iter(results_cursor) if to_iter else results_cursor.fetchall()
            )

        return {"data": data_or_iter, "sfqid": results_cursor.sfqid}

    def execute(
        self,
        plan: MockExecutionPlan,
        to_pandas: bool = False,
        to_iter: bool = False,
        block: bool = True,
        data_type: _AsyncResultType = _AsyncResultType.ROW,
        case_sensitive: bool = True,
        **kwargs,
    ) -> Union[
        List[Row], "pandas.DataFrame", Iterator[Row], Iterator["pandas.DataFrame"]
    ]:
        if not case_sensitive:
            raise NotImplementedError(
                "[Local Testing] Case insensitive DataFrame.collect is currently not supported."
            )
        if not block:
            raise NotImplementedError(
                "[Local Testing] Async jobs are currently not supported."
            )

        res = execute_mock_plan(plan)
        if isinstance(res, TableEmulator):
            columns = [*res.columns]
            rows = []
            for pdr in res.itertuples(index=False, name=None):
                row = Row(*pdr)
                row._fields = columns
                rows.append(row)
        elif isinstance(res, list):
            rows = res
        return rows

    @SnowflakePlan.Decorator.wrap_exception
    def get_result_set(
        self,
        plan: SnowflakePlan,
        to_pandas: bool = False,
        to_iter: bool = False,
        block: bool = True,
        data_type: _AsyncResultType = _AsyncResultType.ROW,
        **kwargs,
    ) -> Tuple[
        Dict[
            str,
            Union[
                List[Any],
                "pandas.DataFrame",
                SnowflakeCursor,
                Iterator["pandas.DataFrame"],
                str,
            ],
        ],
        List[ResultMetadata],
    ]:
        action_id = plan.session._generate_new_action_id()

        result, result_meta = None, None
        try:
            placeholders = {}
            is_batch_insert = False
            for q in plan.queries:
                if isinstance(q, BatchInsertQuery):
                    is_batch_insert = True
                    break
            # since batch insert does not support async execution (? in the query), we handle it separately here
            if len(plan.queries) > 1 and not block and not is_batch_insert:
                final_query = f"""EXECUTE IMMEDIATE $$
DECLARE
    res resultset;
BEGIN
    {";".join(q.sql for q in plan.queries[:-1])};
    res := ({plan.queries[-1].sql});
    return table(res);
END;
$$"""
                # In multiple queries scenario, we are unable to get the query id of former query, so we replace
                # place holder with fucntion last_query_id() here
                for q in plan.queries:
                    final_query = final_query.replace(
                        f"'{q.query_id_place_holder}'", "LAST_QUERY_ID()"
                    )

                result = self.run_query(
                    final_query,
                    to_pandas,
                    to_iter,
                    is_ddl_on_temp_object=plan.queries[0].is_ddl_on_temp_object,
                    block=block,
                    data_type=data_type,
                    async_job_plan=plan,
                    **kwargs,
                )

                # since we will return a AsyncJob instance, result_meta is not needed, we will create result_meta in
                # AsyncJob instance when needed
                result_meta = None
                if action_id < plan.session._last_canceled_id:
                    raise SnowparkClientExceptionMessages.SERVER_QUERY_IS_CANCELLED()
            else:
                for i, query in enumerate(plan.queries):
                    if isinstance(query, BatchInsertQuery):
                        self.run_batch_insert(query.sql, query.rows, **kwargs)
                    else:
                        is_last = i == len(plan.queries) - 1 and not block
                        final_query = query.sql
                        for holder, id_ in placeholders.items():
                            final_query = final_query.replace(holder, id_)
                        result = self.run_query(
                            final_query,
                            to_pandas,
                            to_iter and (i == len(plan.queries) - 1),
                            is_ddl_on_temp_object=query.is_ddl_on_temp_object,
                            block=not is_last,
                            data_type=data_type,
                            async_job_plan=plan,
                            **kwargs,
                        )
                        placeholders[query.query_id_place_holder] = (
                            result["sfqid"] if not is_last else result.query_id
                        )
                        result_meta = self._cursor.description
                    if action_id < plan.session._last_canceled_id:
                        raise SnowparkClientExceptionMessages.SERVER_QUERY_IS_CANCELLED()
        finally:
            # delete created tmp object
            if block:
                for action in plan.post_actions:
                    self.run_query(
                        action.sql,
                        is_ddl_on_temp_object=action.is_ddl_on_temp_object,
                        block=block,
                        **kwargs,
                    )

        if result is None:
            raise SnowparkClientExceptionMessages.SQL_LAST_QUERY_RETURN_RESULTSET()

        return result, result_meta

    def get_result_and_metadata(
        self, plan: SnowflakePlan, **kwargs
    ) -> Tuple[List[Row], List[Attribute]]:
        res = execute_mock_plan(plan)
        attrs = [
            Attribute(name=column_name, datatype=res[column_name].sf_type)
            for column_name in res.columns.tolist()
        ]

        rows = [
            Row(*[res.iloc[i, j] for j in range(len(attrs))]) for i in range(len(res))
        ]
        return rows, attrs

    def get_result_query_id(self, plan: SnowflakePlan, **kwargs) -> str:
        # get the iterator such that the data is not fetched
        result_set, _ = self.get_result_set(plan, to_iter=True, **kwargs)
        return result_set["sfqid"]


def _fix_pandas_df_integer(
    pd_df: "pandas.DataFrame", results_cursor: SnowflakeCursor
) -> "pandas.DataFrame":
    for column_metadata, pandas_dtype, pandas_col_name in zip(
        results_cursor.description, pd_df.dtypes, pd_df.columns
    ):
        if (
            FIELD_ID_TO_NAME.get(column_metadata.type_code) == "FIXED"
            and column_metadata.precision is not None
            and column_metadata.scale == 0
            and not str(pandas_dtype).startswith("int")
        ):
            pd_df[pandas_col_name] = pandas.to_numeric(
                pd_df[pandas_col_name], downcast="integer"
            )
    return pd_df