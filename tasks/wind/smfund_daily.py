# -*- coding: utf-8 -*-
"""
Created on Fri Mar 31 10:52:30 2017

@author: Yupeng Guo - alanguoyupeng@163.com

"""
import pandas as pd
from math import isnan
import pymysql
from sqlalchemy import create_engine
from direstinvoker.iwind import APIError

from tasks import app
from tasks.backend.orm import build_primary_key
from datetime import datetime, date, timedelta
from sqlalchemy.types import String, Date, Float
from pandas.tslib import Timestamp
from sqlalchemy.dialects.mysql import DOUBLE
from tasks.utils.db_utils import with_db_session, bunch_insert_on_duplicate_update, alter_table_2_myisam
from tasks.wind import invoker
from tasks.backend import engine_md
from tasks.utils.fh_utils import STR_FORMAT_DATE, date_2_str, str_2_date
import logging
logger = logging.getLogger()
ONE_DAY = timedelta(days=1)
# 标示每天几点以后下载当日行情数据
BASE_LINE_HOUR = 16


@app.tasks
def import_smfund_daily():
    """
    :return:
    """
    table_name = "wind_smfund_daily"
    has_table = engine_md.has_table(table_name)
    col_name_param_list = {
        ('code_p', String(20)),
        ('trade_date', Date),
        ('next_pcvdate', Date),
        ('a_nav', DOUBLE),
        ('b_nav', DOUBLE),
        ('a_fs_inc', DOUBLE),
        ('b_fs_inc', DOUBLE),
        ('cur_interest', DOUBLE),
        ('next_interest', DOUBLE),
        ('ptm_year', DOUBLE),
        ('anal_pricelever', DOUBLE),
        ('anal_navlevel', DOUBLE),
        ('t1_premium', DOUBLE),
        ('t2_premium', DOUBLE),
        ('dq_status', String(50)),
        ('open', DOUBLE),
        ('high', DOUBLE),
        ('low', DOUBLE),
        ('close', DOUBLE),
        ('volume', DOUBLE),
        ('amt', DOUBLE),
        ('pct_chg', DOUBLE),
        ('open_a', DOUBLE),
        ('high_a', DOUBLE),
        ('low_a', DOUBLE),
        ('close_a', DOUBLE),
        ('volume_a', DOUBLE),
        ('amt_a', DOUBLE),
        ('pct_chg_a', DOUBLE),
        ('open_b', DOUBLE),
        ('high_b', DOUBLE),
        ('low_b', DOUBLE),
        ('close_b', DOUBLE),
        ('volume_b', DOUBLE),
        ('amt_b', DOUBLE),
        ('pct_chg_b', DOUBLE),
    }
    wind_indictor_str = ",".join([key for key, _ in col_name_param_list])
    # 设置dtype类型
    dtype = {key: val for key, val in col_name_param_list}

    date_ending = date.today() - ONE_DAY if datetime.now().hour < BASE_LINE_HOUR else date.today()
    date_ending_str = date_ending.strftime('%Y-%m-%d')
    if has_table:
        sql_str = """
            select wind_code, ifnull(trade_date_max, fund_setupdate) date_start, class_a_code, class_b_code
            from wind_smfund_info fi left outer join
            (select code_p, adddate(max(trade_date), 1) trade_date_max from wind_smfund_daily group by code_p) smd
            on fi.wind_code = smd.code_p
            where fund_setupdate is not null
            and class_a_code is not null
            and class_b_code is not null"""
    else:
        sql_str = """
            select wind_code, ifnull(trade_date_max, fund_setupdate) date_start, class_a_code, class_b_code
            from wind_smfund_info
            where fund_setupdate is not null
            and class_a_code is not null
            and class_b_code is not null"""
    df = pd.read_sql(sql_str, engine_md)
    df.set_index('wind_code', inplace=True)

    data_len = df.shape[0]
    logger.info('分级基金数量: %d', data_len)
    index_start = 1
    for data_num, wind_code in enumerate(df.index, start=1):  # 可调整 # [100:min([df_count, 200])]
        if data_num < index_start:
            continue
        logger.info('%d/%d) %s start to import', data_num, data_len, wind_code)
        date_from = df.loc[wind_code, 'date_start']
        date_from = str_2_date(date_from)
        if type(date_from) not in (date, datetime, Timestamp):
            logger.info('%d/%d) %s has no fund_setupdate will be ignored', data_num, data_len, wind_code)
            # print(df.iloc[i, :])
            continue
        date_from_str = date_from.strftime('%Y-%m-%d')
        if date_from > date_ending:
            logger.info('%d/%d) %s %s %s 跳过', data_num, data_len, wind_code, date_from_str, date_ending_str)
            continue
        field = "open,high,low,close,volume,amt,pct_chg"
        # wsd_cache(w, code, field, beginTime, today, "")
        try:
            df_p = invoker.wsd(wind_code, field, date_from_str, date_ending_str, "")
        except APIError as exp:
            logger.exception("%d/%d) %s 执行异常", data_num, data_len, wind_code)
            if exp.ret_dic.setdefault('error_code', 0) in (
                    -40520007,  # 没有可用数据
                    -40521009,  # 数据解码失败。检查输入参数是否正确，如：日期参数注意大小月月末及短二月
            ):
                continue
            else:
                break
        if df_p is None:
            continue
        df_p.rename(columns=lambda x: x.swapcase(), inplace=True)
        df_p['code_p'] = wind_code
        code_a = df.loc[wind_code, 'class_a_code']
        if code_a is None:
            print('%d %s has no code_a will be ignored' % (data_num, wind_code))
            # print(df.iloc[i, :])
            continue
        # df_a = wsd_cache(w, code_a, field, beginTime, today, "")
        df_a = invoker.wsd(code_a, field, date_from_str, date_ending_str, "")
        df_a.rename(columns=lambda x: x.swapcase() + '_a', inplace=True)
        code_b = df.loc[wind_code, 'class_b_code']
        # df_b = wsd_cache(w, code_b, field, beginTime, today, "")
        df_b = invoker.wsd(code_b, field, date_from_str, date_ending_str, "")
        df_b.columns = df_b.columns.map(lambda x: x.swapcase() + '_b')
        new_df = pd.DataFrame()
        for date_str in df_p.index:
            # time = date_str.date().strftime('%Y-%m-%d')
            field = "date=%s;windcode=%s;field=%s" % (
                date_str, wind_code, wind_indictor_str)
            # wset_cache(w, "leveragedfundinfo", field)
            temp = invoker.wset("leveragedfundinfo", field)
            temp['date'] = date_str
            new_df = new_df.append(temp)
        new_df['next_pcvdate'] = new_df['next_pcvdate'].map(lambda x: str_2_date(x) if x is not None else x)
        new_df.set_index('date', inplace=True)
        one_df = pd.concat([df_p, df_a, df_b, new_df], axis=1)
        one_df.reset_index(inplace=True)
        #    one_df['date'] = one_df['date'].map(lambda x: x.date())
        one_df.rename(columns={'date': 'trade_date'}, inplace=True)
        one_df.set_index(['code_p', 'trade_date'], inplace=True)
        # one_df.to_sql('wind_smfund_daily', engine_md, if_exists='append', index_label=['code_p', 'trade_date'],
        #               dtype={
        #                   # 'code_p': String(20),
        #                   # 'trade_date': Date,
        #                   # 'next_pcvdate': Date,
        #                   # 'a_nav': Float,
        #                   # 'b_nav': Float,
        #                   # 'a_fs_inc': Float,
        #                   # 'b_fs_inc': Float,
        #                   # 'cur_interest': Float,
        #                   # 'next_interest': Float,
        #                   # 'ptm_year': Float,
        #                   # 'anal_pricelever': Float,
        #                   # 'anal_navlevel': Float,
        #                   # 't1_premium': Float,
        #                   # 't2_premium': Float,
        #                   # 'dq_status': String(50),
        #                   # 'open': Float, 'high': Float, 'low': Float, 'close': Float,
        #                   # 'volume': Float, 'amt': Float, 'pct_chg': Float,
        #                   # 'open_a': Float, 'high_a': Float, 'low_a': Float, 'close_a': Float,
        #                   # 'volume_a': Float, 'amt_a': Float, 'pct_chg_a': Float,
        #                   # 'open_b': Float, 'high_b': Float, 'low_b': Float, 'close_b': Float,
        #                   # 'volume_b': Float, 'amt_b': Float, 'pct_chg_b': Float,
        #               })
        bunch_insert_on_duplicate_update(one_df, table_name, engine_md, dtype=dtype)
        logger.info('%d/%d) %s import success', data_num, data_len, wind_code)
        if not has_table and engine_md.has_table(table_name):
            alter_table_2_myisam(engine_md, [table_name])
            build_primary_key([table_name])
    # info_df = info_df.append(one_df)
    # dump_cache()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s: %(levelname)s [%(name)s:%(funcName)s] %(message)s')

    import_smfund_daily()