#
# Copyright (c) 2012-2023 Snowflake Computing Inc. All rights reserved.
#
import importlib
import inspect
from functools import cached_property, partial
from unittest.mock import MagicMock
from typing import Dict, List, NoReturn, Optional, Tuple, Union

import numpy as np
import pandas as pd

from snowflake.snowpark import Column
from snowflake.snowpark._internal.analyzer.analyzer_utils import (
    UNION,
    UNION_ALL,
    quote_name,
)
from snowflake.snowpark._internal.analyzer.binary_expression import (
    Add,
    And,
    BinaryExpression,
    Divide,
    EqualNullSafe,
    EqualTo,
    GreaterThan,
    GreaterThanOrEqual,
    LessThan,
    LessThanOrEqual,
    Multiply,
    NotEqualTo,
    Or,
    Subtract,
)
from snowflake.snowpark._internal.analyzer.binary_plan_node import Join
from snowflake.snowpark._internal.analyzer.expression import (
    Attribute,
    Expression,
    FunctionExpression,
    InExpression,
    Like,
    ListAgg,
    Literal,
    MultipleExpression,
    RegExp,
    Star,
    UnresolvedAttribute,
)
from snowflake.snowpark._internal.analyzer.snowflake_plan import SnowflakePlan
from snowflake.snowpark._internal.analyzer.snowflake_plan_node import (
    LogicalPlan,
    Range,
    SnowflakeValues,
)
from snowflake.snowpark._internal.analyzer.sort_expression import Ascending, NullsFirst
from snowflake.snowpark._internal.analyzer.unary_expression import (
    Alias,
    IsNaN,
    IsNotNull,
    IsNull,
    Not,
    UnresolvedAlias,
)
from snowflake.snowpark._internal.analyzer.unary_plan_node import Aggregate
from snowflake.snowpark.mock.functions import _MOCK_FUNCTION_IMPLEMENTATION_MAP
from snowflake.snowpark.mock.mock_select_statement import (
    MockSelectable,
    MockSelectExecutionPlan,
    MockSelectStatement,
    MockSetStatement,
)
from snowflake.snowpark.mock.snowflake_data_type import ColumnEmulator, TableEmulator
from snowflake.snowpark.mock.util import convert_wildcard_to_regex, custom_comparator
from snowflake.snowpark.types import LongType, _NumericType


class MockExecutionPlan(LogicalPlan):
    def __init__(
        self,
        session,
        *,
        child: Optional["MockExecutionPlan"] = None,
        source_plan: Optional[LogicalPlan] = None,
        expr_to_alias: Optional[Dict[str, str]] = None,
    ) -> NoReturn:
        super().__init__()
        self.session = session
        self.source_plan = source_plan
        self.child = child
        self.expr_to_alias = expr_to_alias if expr_to_alias is not None else {}
        mock_query = MagicMock()
        mock_query.sql = "SELECT MOCK_TEST_FAKE_QUERY()"
        self.queries = [mock_query]
        self.post_actions = []
        self.api_calls = None

    @cached_property
    def attributes(self) -> List[Attribute]:
        # output = analyze_attributes(self.schema_query, self.session)
        # self.schema_query = schema_value_statement(output)
        output = describe(self)
        return output

    @cached_property
    def output(self) -> List[Attribute]:
        return [Attribute(a.name, a.datatype, a.nullable) for a in self.attributes]


# Wrong type hints
def execute_mock_plan(
    plan: MockExecutionPlan, expr_to_alias: Optional[Dict[str, str]] = None
) -> TableEmulator:
    if expr_to_alias is None:
        expr_to_alias = {}
    if isinstance(plan, (MockExecutionPlan, SnowflakePlan)):
        source_plan = plan.source_plan
        analyzer = plan.session._analyzer
    else:
        source_plan = plan
        analyzer = plan.analyzer
    if isinstance(source_plan, SnowflakeValues):
        table = TableEmulator(
            source_plan.data,
            columns=[x.name for x in source_plan.output],
            sf_types={x.name: x.datatype for x in source_plan.output},
            dtype=object,
        )
        for column_name in table.columns:
            sf_type = table.sf_types[column_name]
            table[column_name].set_sf_type(table.sf_types[column_name])
            if not isinstance(sf_type, _NumericType):
                table[column_name].replace(np.nan, None, inplace=True)
        return table
    if isinstance(source_plan, MockSelectExecutionPlan):
        return execute_mock_plan(source_plan.execution_plan, expr_to_alias)
    if isinstance(source_plan, MockSelectStatement):
        projection: Optional[List[Expression]] = source_plan.projection or []
        from_: Optional[MockSelectable] = source_plan.from_
        where: Optional[Expression] = source_plan.where
        order_by: Optional[List[Expression]] = source_plan.order_by
        limit_: Optional[int] = source_plan.limit_

        from_df = execute_mock_plan(from_, expr_to_alias)

        if not projection and isinstance(from_, MockSetStatement):
            projection = from_.set_operands[0].selectable.projection

        result_df = TableEmulator()

        for exp in projection:
            if isinstance(exp, Star):
                for col in from_df.columns:
                    result_df[col] = from_df[col]
            else:
                if isinstance(exp, Alias):
                    column_name = expr_to_alias.get(exp.expr_id, exp.name)
                else:
                    column_name = analyzer.analyze(exp, expr_to_alias)

                column_series = calculate_expression(
                    exp, from_df, analyzer, expr_to_alias
                )
                result_df[column_name] = column_series

                # Update expr_to_alias map for future reference
                if isinstance(exp, (Alias)):
                    if isinstance(exp.child, Attribute):
                        quoted_name = quote_name(exp.name)
                        expr_to_alias[exp.child.expr_id] = quoted_name
                    for k, v in expr_to_alias.items():
                        if v == exp.child.name:
                            expr_to_alias[k] = quoted_name
                # plan.expr_to_alias.update(expr_to_alias)

        if where:
            condition = calculate_expression(where, result_df, analyzer, expr_to_alias)
            result_df = result_df[condition]

        sort_columns_array = []
        sort_orders_array = []
        null_first_last_array = []
        if order_by:
            for exp in order_by:
                sort_columns_array.append(analyzer.analyze(exp.child, expr_to_alias))
                sort_orders_array.append(isinstance(exp.direction, Ascending))
                null_first_last_array.append(
                    isinstance(exp.null_ordering, NullsFirst)
                    or exp.null_ordering == NullsFirst
                )

        if sort_columns_array:
            kk = reversed(
                list(zip(sort_columns_array, sort_orders_array, null_first_last_array))
            )
            for column, ascending, null_first in kk:
                comparator = partial(custom_comparator, ascending, null_first)
                result_df = result_df.sort_values(by=column, key=comparator)

        if limit_ is not None:
            result_df = result_df.head(n=limit_)

        return result_df
    if isinstance(source_plan, MockSetStatement):
        first_operand = source_plan.set_operands[0]
        res_df = execute_mock_plan(
            MockExecutionPlan(
                source_plan.analyzer.session, source_plan=first_operand.selectable
            ),
            expr_to_alias,
        )
        for operand in source_plan.set_operands[1:]:
            operator = operand.operator
            if operator in (UNION, UNION_ALL):
                cur_df = execute_mock_plan(
                    MockExecutionPlan(
                        source_plan.analyzer.session, source_plan=operand.selectable
                    ),
                    expr_to_alias,
                )
                res_df = pd.concat([res_df, cur_df], ignore_index=True)
                res_df = (
                    res_df.drop_duplicates().reset_index(drop=True)
                    if operator == UNION
                    else res_df
                )
            else:
                raise NotImplementedError("Set statement not implemented")
        return res_df

    if isinstance(source_plan, Aggregate):
        child_rf = execute_mock_plan(source_plan.child, expr_to_alias)
        column_exps = [
            plan.session._analyzer.analyze(exp, expr_to_alias)
            for exp in source_plan.grouping_expressions
        ]

        # Aggregate may not have column_exps, which is allowed in the case of `Dataframe.agg`, in this case we pass
        # lambda x: True as the `by` parameter
        children_dfs = child_rf.groupby(by=column_exps or (lambda x: True), sort=False)
        # we first define the returning DataFrame with its column names
        result_df = TableEmulator(
            columns=[
                plan.session._analyzer.analyze(exp, expr_to_alias)
                for exp in source_plan.aggregate_expressions
            ]
        )
        for group_keys, indices in children_dfs.indices.items():
            # we construct row by row
            cur_group = child_rf.iloc[indices]
            # each row starts with group keys/column expressions, if there is no group keys/column expressions
            # it means aggregation without group (Datagrame.agg)
            values = (
                (list(group_keys) if isinstance(group_keys, tuple) else [group_keys])
                if column_exps
                else []
            )
            # the first len(column_exps) items of calculate_expression are the group_by column expressions,
            # the remaining are the aggregation function expressions
            for exp in source_plan.aggregate_expressions[len(column_exps) :]:
                cal_exp_res = calculate_expression(
                    exp, cur_group, analyzer, expr_to_alias
                )
                # and then append the calculated value
                values.append(cal_exp_res.iat[0])
            result_df.loc[len(result_df.index)] = values
        return result_df
    if isinstance(source_plan, Range):
        col = pd.Series(
            data=[
                num
                for num in range(source_plan.start, source_plan.end, source_plan.step)
            ]
        )
        result_df = TableEmulator(
            col,
            columns=['"ID"'],
            sf_types={'"ID"': LongType()},
            dtype=object,
        )
        return result_df

    if isinstance(source_plan, Join):
        L_expr_to_alias = {}
        R_expr_to_alias = {}
        left = execute_mock_plan(source_plan.left, L_expr_to_alias).reset_index(
            drop=True
        )
        right = execute_mock_plan(source_plan.right, R_expr_to_alias).reset_index(
            drop=True
        )
        # Processing ON clause
        using_columns = getattr(source_plan.join_type, "using_columns", None)
        on = using_columns
        if isinstance(on, list):  # USING a list of columns
            if on:
                on = [quote_name(x.upper()) for x in on]
            else:
                on = None
        elif isinstance(on, Column):  # ON a single column
            on = on.name
        elif isinstance(
            on, BinaryExpression
        ):  # ON a condition, apply where to a Cartesian product
            on = None
        else:  # ON clause not specified, SF returns a Cartesian product
            on = None

        # Processing the join type
        how = source_plan.join_type.sql
        if how.startswith("USING "):
            how = how[6:]
        if how.startswith("NATURAL "):
            how = how[8:]
        if how == "LEFT OUTER":
            how = "LEFT"
        elif how == "RIGHT OUTER":
            how = "RIGHT"
        elif "FULL" in how:
            how = "OUTER"
        elif "SEMI" in how:
            how = "INNER"
        elif "ANTI" in how:
            how = "CROSS"

        if (
            "NATURAL" in source_plan.join_type.sql and on is None
        ):  # natural joins use the list of common names as keys
            on = left.columns.intersection(right.columns).values.tolist()

        if on is None:
            how = "CROSS"

        result_df = left.merge(
            right,
            on=on,
            how=how.lower(),
        )

        # Restore sf_types information after merging, there should be better way to do this
        result_df.sf_types.update(left.sf_types)
        result_df.sf_types.update(right.sf_types)

        if on:
            result_df = result_df.reset_index(drop=True)
            if isinstance(on, list):
                # Reorder columns for JOINS with USING clause, where Snowflake puts the key columns to the left
                reordered_cols = on + [
                    col for col in result_df.columns.tolist() if col not in on
                ]
                result_df = result_df[reordered_cols]

        common_columns = set(L_expr_to_alias.keys()).intersection(
            R_expr_to_alias.keys()
        )
        new_expr_to_alias = {
            k: v
            for k, v in {
                **L_expr_to_alias,
                **R_expr_to_alias,
            }.items()
            if k not in common_columns
        }
        expr_to_alias.update(new_expr_to_alias)

        if source_plan.condition:
            condition = calculate_expression(
                source_plan.condition, result_df, analyzer, expr_to_alias
            )

            if "SEMI" in source_plan.join_type.sql:  # left semi
                result_df = left[
                    left.apply(tuple, 1).isin(
                        result_df[condition][left.columns].apply(tuple, 1)
                    )
                ].dropna()
            elif "ANTI" in source_plan.join_type.sql:  # left anti
                result_df = left[
                    ~(
                        left.apply(tuple, 1).isin(
                            result_df[condition][left.columns].apply(tuple, 1)
                        )
                    )
                ].dropna()
            elif "LEFT" in source_plan.join_type.sql:  # left outer join
                # rows from LEFT that did not get matched
                unmatched_left = left[
                    ~left.apply(tuple, 1).isin(
                        result_df[condition][left.columns].apply(tuple, 1)
                    )
                ]
                unmatched_left[right.columns] = None
                result_df = pd.concat([result_df[condition], unmatched_left])
            elif "RIGHT" in source_plan.join_type.sql:  # right outer join
                # rows from RIGHT that did not get matched
                unmatched_right = right[
                    ~right.apply(tuple, 1).isin(
                        result_df[condition][right.columns].apply(tuple, 1)
                    )
                ]
                unmatched_right[left.columns] = None
                result_df = pd.concat([result_df[condition], unmatched_right])
            elif "OUTER" in source_plan.join_type.sql:  # full outer join
                # rows from LEFT that did not get matched
                unmatched_left = left[
                    ~left.apply(tuple, 1).isin(
                        result_df[condition][left.columns].apply(tuple, 1)
                    )
                ]
                unmatched_left[right.columns] = None
                # rows from RIGHT that did not get matched
                unmatched_right = right[
                    ~right.apply(tuple, 1).isin(
                        result_df[condition][right.columns].apply(tuple, 1)
                    )
                ]
                unmatched_right[left.columns] = None
                result_df = pd.concat(
                    [result_df[condition], unmatched_left, unmatched_right]
                )
            else:
                result_df = result_df[condition]

        return result_df.where(result_df.notna(), None)  # Swap np.nan with None


def describe(plan: MockExecutionPlan):
    result = execute_mock_plan(plan)
    return [Attribute(result[c].name, result[c].sf_type) for c in result.columns]


def calculate_expression(
    exp: Expression,
    input_data: Union[TableEmulator, ColumnEmulator],
    analyzer,
    expr_to_alias: Dict[str, str],
) -> Tuple[ColumnEmulator]:
    """Returns the calculated expression evaluated based on input table/column"""
    if isinstance(exp, Attribute):
        return input_data[expr_to_alias.get(exp.expr_id, exp.name)]
    if isinstance(exp, (UnresolvedAttribute, Attribute)):
        return input_data[exp.name]
    if isinstance(exp, (UnresolvedAlias, Alias)):
        return calculate_expression(exp.child, input_data, analyzer, expr_to_alias)
    if isinstance(exp, FunctionExpression):
        # evaluated_children maps to parameters passed to the function call
        evaluated_children = [
            calculate_expression(c, input_data, analyzer, expr_to_alias)
            for c in exp.children
        ]
        original_func = getattr(
            importlib.import_module("snowflake.snowpark.functions"), exp.name
        )
        signatures = inspect.signature(original_func)
        spec = inspect.getfullargspec(original_func)
        if exp.name not in _MOCK_FUNCTION_IMPLEMENTATION_MAP:
            raise NotImplementedError(
                f"Mock function {exp.name} has not been implemented yet."
            )
        to_pass_args = []
        for idx, key in enumerate(signatures.parameters):
            if key == spec.varargs:
                to_pass_args.extend(evaluated_children[idx:])
            else:
                try:
                    to_pass_args.append(evaluated_children[idx])
                except IndexError:
                    to_pass_args.append(None)
        if (
            exp.name == "count"
            and isinstance(exp.children[0], Literal)
            and exp.children[0].sql == "LITERAL()"
        ):
            to_pass_args[0] = input_data
        if exp.name == "array_agg":
            to_pass_args[-1] = exp.is_distinct
        return _MOCK_FUNCTION_IMPLEMENTATION_MAP[exp.name](*to_pass_args)
    if isinstance(exp, ListAgg):
        column = calculate_expression(exp.col, input_data, analyzer, expr_to_alias)
        return _MOCK_FUNCTION_IMPLEMENTATION_MAP["listagg"](
            column, is_distinct=exp.is_distinct, delimiter=exp.delimiter
        )
    if isinstance(exp, IsNull):
        child_column = calculate_expression(
            exp.child, input_data, analyzer, expr_to_alias
        )
        return ColumnEmulator(data=[bool(data is None) for data in child_column])
    if isinstance(exp, IsNotNull):
        child_column = calculate_expression(
            exp.child, input_data, analyzer, expr_to_alias
        )
        return ColumnEmulator(data=[bool(data is not None) for data in child_column])
    if isinstance(exp, IsNaN):
        child_column = calculate_expression(
            exp.child, input_data, analyzer, expr_to_alias
        )
        return child_column.isna()
    if isinstance(exp, Not):
        child_column = calculate_expression(
            exp.child, input_data, analyzer, expr_to_alias
        )
        return ~child_column
    if isinstance(exp, UnresolvedAttribute):
        return analyzer.analyze(exp, expr_to_alias)
    if isinstance(exp, Literal):
        return exp.value
    if isinstance(exp, BinaryExpression):
        new_column = None
        left = calculate_expression(exp.left, input_data, analyzer, expr_to_alias)
        right = calculate_expression(exp.right, input_data, analyzer, expr_to_alias)
        if isinstance(exp, Multiply):
            new_column = left * right
        if isinstance(exp, Divide):
            new_column = left / right
        if isinstance(exp, Add):
            new_column = left + right
        if isinstance(exp, Subtract):
            new_column = left - right
        if isinstance(exp, EqualTo):
            new_column = left == right
        if isinstance(exp, NotEqualTo):
            new_column = left != right
        if isinstance(exp, GreaterThanOrEqual):
            new_column = left >= right
        if isinstance(exp, GreaterThan):
            new_column = left > right
        if isinstance(exp, LessThanOrEqual):
            new_column = left <= right
        if isinstance(exp, LessThan):
            new_column = left < right
        if isinstance(exp, And):
            new_column = (
                (left & right)
                if isinstance(input_data, TableEmulator) or not input_data
                else (left & right) & input_data
            )
        if isinstance(exp, Or):
            new_column = (
                (left | right)
                if isinstance(input_data, TableEmulator) or not input_data
                else (left | right) & input_data
            )
        if isinstance(exp, EqualNullSafe):
            new_column = (
                (left == right)
                | (left.isna() & right.isna())
                | (left.isnull() & right.isnull())
            )
        return new_column
    if isinstance(exp, RegExp):
        column = calculate_expression(exp.expr, input_data, analyzer, expr_to_alias)
        pattern = str(analyzer.analyze(exp.pattern, expr_to_alias))
        pattern = f"^{pattern}" if not pattern.startswith("^") else pattern
        pattern = f"{pattern}$" if not pattern.endswith("$") else pattern
        return column.str.match(pattern)
    if isinstance(exp, Like):
        column = calculate_expression(exp.expr, input_data, analyzer, expr_to_alias)
        pattern = convert_wildcard_to_regex(
            str(analyzer.analyze(exp.pattern, expr_to_alias))
        )
        return column.str.match(pattern)
    if isinstance(exp, InExpression):
        column = calculate_expression(exp.columns, input_data, analyzer, expr_to_alias)
        values = [
            calculate_expression(expression, input_data, analyzer, expr_to_alias)
            for expression in exp.values
        ]
        return column.isin(values)
    if isinstance(exp, MultipleExpression):
        raise NotImplementedError("MultipleExpression is to be implemented")