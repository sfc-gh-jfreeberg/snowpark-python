#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2012-2021 Snowflake Computing Inc. All rights reserved.
#
from snowflake.snowpark import Column, DataFrame
from snowflake.snowpark.functions import builtin


class Imputer:
    __DATABASE = "hayu"
    __SCHEMA = "imputer"
    __BUNDLE = f"{__DATABASE}.{__SCHEMA}"

    def __init__(self, session=None, input_col=None):
        self.session = session
        self.input_col = input_col

    def fit(self, input_df: DataFrame, isNumerical: bool) -> str:
        query = input_df.select(self.input_col)._DataFrame__plan.queries[-1].sql
        res = self.session.sql(
            f"call {self.__BUNDLE}.fit($${query}$$, {isNumerical})"
        ).collect()
        return res[0][0]

    def transform(self, col: Column) -> Column:
        return builtin(f"{self.__BUNDLE}.transform")(col)
