# -*- coding: utf-8 -*-
"""
全新策略：BBI日周共振选股
【速度优化 + 公共缓存版】

条件摘要：
1. 前一日下跌/绿K
2. 当日放量红K（量比>1.1）
3. 近5日无三连红、无三连涨
4. 近10日累计涨幅 < 4%，近5日累计涨幅 < 3%
5. 当日 BBI 中轨向上（转红或连续红）
6. 前一日 MACD/KDJ 至少一绿；当日两者皆红
7. 当日 MACD 柱绝对值 ≥ 前一日绝对值
8. 模拟周线 BBI 向上，且连续抬头 ≤ 3周（约15个交易日）
9. 近三周（约15个交易日）模拟周K中，至少有一周为绿K或下跌周
10. 当日实体上沿 > 前5日中至少任意2日的实体上沿
"""
import tushare as ts
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, time as dttime
import os
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import time
import random
import glob

warnings.filterwarnings('ignore')

# ==================== 颜色输出 ====================
RED = '\033[91m'
YELLOW = '\033[93m'
GREEN = '\033[92m'
CYAN = '\033[96m'
BOLD = '\033[1m'
RESET = '\033[0m'

if os.name == 'nt':
    os.system('')
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 0x0007 | 0x0004)
    except:
        pass

class GlobalConfig:
    TUSHARE_TOKEN = 'a91652800def39d213abec3af9563e4485e83940bfbbd8276f8adaa8'  # 请确保Token有效
    OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'BBI日周共振_选股结果')
    CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股日线公共缓存')
    LOOKBACK_DAYS = 500
    USE_PARQUET = True

class CustomStrategyFilter:
    def __init__(self, token):
        self.pro = ts.pro_api(token)
        self.rt_data_dict = {}
        self.level = 1
        self.verbose = True
        self._rate_limited = False
        os.makedirs(GlobalConfig.CACHE_DIR, exist_ok=True)
        os.makedirs(GlobalConfig.OUTPUT_DIR, exist_ok=True)
        self.use_parquet = GlobalConfig.USE_PARQUET
        if self.use_parquet:
            try:
                import pyarrow  # noqa
            except ImportError:
                try:
                    import fastparquet  # noqa
                except ImportError:
                    self.use_parquet = False
                    print(f"{YELLOW}未检测到 pyarrow/fastparquet，缓存将使用 CSV（稍慢但兼容）{RESET}")

    def set_level(self, level):
        self.level = level

    def _call_with_retry(self, api_func, **kwargs):
        retries = 0
        max_retries = 8
        while retries < max_retries:
            try:
                return api_func(**kwargs)
            except Exception as e:
                err_str = str(e).lower()
                if any(kw in err_str for kw in ["限流", "频率超限", "max retries", "connection", "timeout", "500", "502", "503", "429"]):
                    retries += 1
                    wait = 15 + random.randint(5, 20)
                    if not self._rate_limited:
                        print(f"{YELLOW}[警告] API限流/连接错误，等待 {wait} 秒后重试...{RESET}")
                        self._rate_limited = True
                    time.sleep(wait)
                else:
                    print(f"其他错误: {str(e)[:120]}")
                    raise
        raise Exception(f"API调用失败多次: {kwargs}")

    def validate_trade_date(self, input_date=''):
        if not input_date:
            input_date = datetime.now().strftime('%Y%m%d')
        try:
            cal = self.pro.trade_cal(start_date=input_date, end_date=input_date)
            if not cal.empty and cal['is_open'].iloc[0] == 1:
                return input_date
        except:
            pass
        date_obj = datetime.strptime(input_date, '%Y%m%d')
        for i in range(1, 40):
            prev = (date_obj - timedelta(days=i)).strftime('%Y%m%d')
            try:
                cal = self.pro.trade_cal(start_date=prev, end_date=prev)
                if not cal.empty and cal['is_open'].iloc[0] == 1:
                    return prev
            except:
                continue
        return datetime.now().strftime('%Y%m%d')

    def get_trade_dates(self, end_date, n_days=500):
        start_approx = (datetime.strptime(end_date, '%Y%m%d') - timedelta(days=int(n_days * 1.8))).strftime('%Y%m%d')
        cal = self._call_with_retry(
            self.pro.trade_cal,
            start_date=start_approx,
            end_date=end_date,
            is_open='1'
        )
        dates = sorted(cal['cal_date'].tolist())
        return dates[-n_days:] if len(dates) >= n_days else dates

    # ==================== 缓存读写 ====================
    def _cache_path(self, trade_date):
        ext = 'parquet' if self.use_parquet else 'csv'
        return os.path.join(GlobalConfig.CACHE_DIR, f"{trade_date}.{ext}")

    def save_day_to_cache(self, trade_date, df):
        if df is None or df.empty:
            return
        path = self._cache_path(trade_date)
        try:
            if self.use_parquet:
                df.to_parquet(path, index=False)
            else:
                df.to_csv(path, index=False, encoding='utf-8-sig')
        except Exception as e:
            print(f"  保存缓存 {trade_date} 失败: {e}")

    def load_day_from_cache(self, trade_date):
        path = self._cache_path(trade_date)
        if not os.path.exists(path):
            return None
        try:
            if self.use_parquet:
                return pd.read_parquet(path)
            else:
                return pd.read_csv(path)
        except Exception as e:
            print(f"  读取缓存 {trade_date} 失败: {e}")
            return None

    def get_cached_dates(self):
        pattern = os.path.join(GlobalConfig.CACHE_DIR, '*.parquet' if self.use_parquet else '*.csv')
        files = glob.glob(pattern)
        dates = set()
        for f in files:
            name = os.path.basename(f)
            d = name.split('.')[0]
            if len(d) == 8 and d.isdigit():
                dates.add(d)
        return dates

    def get_stock_pool(self):
        print("正在加载股票池（排除ST/北交所/银行/保险/证券 + 上市≥7天）...")
        df = self.pro.stock_basic(list_status='L', fields='ts_code,name,list_date')
        today = datetime.now().strftime('%Y%m%d')
        filtered = []
        for _, row in df.iterrows():
            name = str(row['name'])
            code = row['ts_code']
            if 'ST' in name or code.startswith('8') or '银行' in name or '保险' in name or '证券' in name:
                continue
            list_date = str(row['list_date'])
            if len(list_date) == 8:
                days = (datetime.strptime(today, '%Y%m%d') - datetime.strptime(list_date, '%Y%m%d')).days
                if days > 7:
                    filtered.append(row.to_dict())
        stocks = pd.DataFrame(filtered)
        print(f"股票池加载完成，共 {len(stocks)} 只")
        return stocks

    def fetch_rt_k_batch(self):
        patterns = ['60*.SH', '688*.SH', '00*.SZ', '002*.SZ', '003*.SZ', '300*.SZ', '301*.SZ']
        rt_dfs = []
        print("获取实时行情（盘中模式）...")
        for pattern in patterns:
            try:
                df = self._call_with_retry(self.pro.rt_k, ts_code=pattern)
                if not df.empty:
                    rt_dfs.append(df)
                time.sleep(1.0)
            except Exception as e:
                print(f"获取 {pattern} 实时行情失败: {str(e)[:50]}")
                time.sleep(3)
        if rt_dfs:
            full_df = pd.concat(rt_dfs, ignore_index=True).drop_duplicates(subset=['ts_code'])
            self.rt_data_dict = full_df.set_index('ts_code').to_dict('index')
            print(f"实时数据获取完成，共 {len(self.rt_data_dict)} 只股票")

    def is_intraday_now(self, trade_date):
        now = datetime.now()
        today_str = now.strftime('%Y%m%d')
        if trade_date != today_str:
            return False
        try:
            cal = self.pro.trade_cal(start_date=today_str, end_date=today_str)
            if cal.empty or cal['is_open'].iloc[0] != 1:
                return False
        except:
            return False
        current_time = now.time()
        return (dttime(9, 30) <= current_time < dttime(11, 30)) or (dttime(13, 0) <= current_time < dttime(15, 0))

    def get_strong_rise_stocks(self, trade_date, is_intraday=False):
        """预筛选：当日上涨K + 涨幅≥0.5%"""
        all_stocks = self.get_stock_pool()
        strong = []
        print("正在进行T日强势预筛选（上涨K线 + 涨幅≥0.5%）...")
        if is_intraday:
            self.fetch_rt_k_batch()
            for _, row in all_stocks.iterrows():
                ts_code = row['ts_code']
                if ts_code not in self.rt_data_dict:
                    continue
                rt = self.rt_data_dict[ts_code]
                try:
                    pre_close = float(rt.get('pre_close', 0))
                    close = float(rt.get('close', 0))
                    open_price = float(rt.get('open', 0))
                    if pre_close > 0:
                        pct_chg = (close - pre_close) / pre_close * 100
                        if close > open_price and pct_chg >= 0.5:
                            strong.append(row.to_dict())
                except:
                    continue
        else:
            try:
                df = self.load_day_from_cache(trade_date)
                if df is None or df.empty:
                    df = self._call_with_retry(
                        self.pro.daily,
                        trade_date=trade_date,
                        fields='ts_code,open,close,pct_chg,high,low,vol'
                    )
                    if not df.empty:
                        self.save_day_to_cache(trade_date, df)
                if not df.empty:
                    if 'pct_chg' not in df.columns:
                        df['pct_chg'] = np.nan
                    df['pct_chg'] = pd.to_numeric(df['pct_chg'], errors='coerce')
                    mask = (df['close'] > df['open']) & (df['pct_chg'] >= 0.5)
                    surging_codes = df.loc[mask, 'ts_code'].tolist()
                    valid_codes = set(all_stocks['ts_code'])
                    surging_codes = [c for c in surging_codes if c in valid_codes]
                    strong = all_stocks[all_stocks['ts_code'].isin(surging_codes)].to_dict('records')
            except Exception as e:
                print(f"批量获取T日数据失败: {e}")
        strong_df = pd.DataFrame(strong)
        print(f"T日粗筛完成：共 {len(strong_df)} 只股票符合基础条件")
        return strong_df

    # ==================== 通达信指标复刻 ====================
    def TDX_SMA(self, series, n, m):
        res = np.zeros(len(series))
        for i in range(len(series)):
            if pd.isna(series.iloc[i]):
                res[i] = np.nan
            else:
                if i == 0 or pd.isna(res[i - 1]):
                    res[i] = series.iloc[i]
                else:
                    res[i] = (m * series.iloc[i] + (n - m) * res[i - 1]) / n
        return pd.Series(res, index=series.index)

    def tdx_barslast(self, condition):
        """通达信 BARSLAST(X)"""
        cond = condition.fillna(False).astype(bool).values
        n = len(cond)
        result = np.full(n, np.nan)
        last_true = -1
        for i in range(n):
            if cond[i]:
                last_true = i
                result[i] = 0
            else:
                if last_true >= 0:
                    result[i] = i - last_true
                else:
                    result[i] = np.nan
        return pd.Series(result, index=condition.index)

    # ==================== 增量缓存拉取 ====================
    def batch_fetch_daily_history(self, trade_date, stock_codes):
        print(f"\n{CYAN}正在准备历史日线数据（公共缓存 + 增量更新）...{RESET}")
        trade_dates = self.get_trade_dates(trade_date, GlobalConfig.LOOKBACK_DAYS)
        print(f"需要交易日范围: {trade_dates[0]} ~ {trade_dates[-1]}，共 {len(trade_dates)} 天")

        cached = self.get_cached_dates()
        need_download = [d for d in trade_dates if d not in cached]
        already_have = [d for d in trade_dates if d in cached]
        print(f"公共缓存已有: {len(already_have)} 天 | 需要下载: {len(need_download)} 天")

        if need_download:
            print(f"{YELLOW}开始增量下载缺失交易日...{RESET}")
            for i, d in enumerate(tqdm(need_download, desc="增量下载")):
                try:
                    df = self._call_with_retry(
                        self.pro.daily,
                        trade_date=d,
                        fields='ts_code,trade_date,open,high,low,close,vol,pct_chg'
                    )
                    if not df.empty:
                        self.save_day_to_cache(d, df)
                    if (i + 1) % 20 == 0:
                        time.sleep(1.2)
                    else:
                        time.sleep(0.2)
                except Exception as e:
                    print(f"  下载 {d} 失败: {str(e)[:60]}")
                    time.sleep(3)
            print(f"{GREEN}增量下载完成{RESET}")
        else:
            print(f"{GREEN}全部交易日均已在公共缓存，无需下载{RESET}")

        print(f"{CYAN}从公共缓存加载数据...{RESET}")
        all_dfs = []
        code_set = set(stock_codes)
        for d in tqdm(trade_dates, desc="加载缓存"):
            df = self.load_day_from_cache(d)
            if df is not None and not df.empty:
                df = df[df['ts_code'].isin(code_set)]
                if not df.empty:
                    all_dfs.append(df)

        if not all_dfs:
            print("未获取到任何历史日线数据！")
            return {}

        full = pd.concat(all_dfs, ignore_index=True)
        full = full.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
        data_dict = {code: group.reset_index(drop=True)
                     for code, group in full.groupby('ts_code')}
        print(f"{GREEN}历史数据就绪，共 {len(data_dict)} 只股票有效数据{RESET}")
        return data_dict

    def process_stock_from_df(self, stock_info, df_daily, trade_date, is_intraday=False):
        """BBI日周共振选股（含新增过滤）"""
        ts_code = stock_info['ts_code']
        name = stock_info.get('name', '')
        try:
            if len(df_daily) < 130:
                return None, 'data_short'

            if is_intraday and ts_code in self.rt_data_dict:
                rt = self.rt_data_dict[ts_code]
                last_date = str(df_daily.iloc[-1]['trade_date'])
                if last_date == trade_date:
                    idx = len(df_daily) - 1
                    df_daily = df_daily.copy()
                    df_daily.at[idx, 'close'] = float(rt['close'])
                    df_daily.at[idx, 'high'] = max(float(df_daily.at[idx, 'high']), float(rt['high']))
                    df_daily.at[idx, 'low'] = min(float(df_daily.at[idx, 'low']), float(rt['low']))
                    if float(rt.get('open', 0)) > 0:
                        df_daily.at[idx, 'open'] = float(rt['open'])
                    df_daily.at[idx, 'vol'] = float(rt['vol'])
                else:
                    new_row = {
                        'ts_code': ts_code, 'trade_date': trade_date,
                        'open': float(rt['open']), 'high': float(rt['high']),
                        'low': float(rt['low']), 'close': float(rt['close']),
                        'vol': float(rt['vol']),
                        'pct_chg': (float(rt['close']) / float(rt.get('pre_close', rt['close'])) - 1) * 100
                    }
                    df_daily = pd.concat([df_daily, pd.DataFrame([new_row])], ignore_index=True)

            close_p = pd.Series(df_daily['close'].values.astype(float))
            high_p = pd.Series(df_daily['high'].values.astype(float))
            low_p = pd.Series(df_daily['low'].values.astype(float))
            open_p = pd.Series(df_daily['open'].values.astype(float))
            vol_p = pd.Series(df_daily['vol'].values.astype(float))

            # ========== 指标 ==========
            bbi = (close_p.rolling(3).mean() + close_p.rolling(6).mean() +
                   close_p.rolling(12).mean() + close_p.rolling(24).mean()) / 4

            ema8 = close_p.ewm(span=8, adjust=False).mean()
            ema20 = close_p.ewm(span=20, adjust=False).mean()
            dif8 = ema8 - ema20
            dea6 = dif8.ewm(span=6, adjust=False).mean()
            macd_bar = 2 * (dif8 - dea6)

            low_min = low_p.rolling(14).min()
            high_max = high_p.rolling(14).max()
            rsv = (close_p - low_min) / (high_max - low_min + 1e-10) * 100
            k = self.TDX_SMA(rsv, 3, 1)
            d = self.TDX_SMA(k, 3, 1)
            kdj_bar = 2 * (k - d)

            w_bbi = (close_p.rolling(15).mean() + close_p.rolling(30).mean() +
                     close_p.rolling(60).mean() + close_p.rolling(120).mean()) / 4

            # ========== 1. 前一日下跌/绿K ==========
            prev_down = (close_p.shift(1) < open_p.shift(1)) | (close_p.shift(1) < close_p.shift(2))

            # ========== 2. 当日放量红K（量比>1.1） ==========
            today_red = (close_p > open_p) & (close_p > close_p.shift(1))
            vol_ok = vol_p / (vol_p.rolling(5).mean().shift(1) + 1e-10) > 1.1
            base_curr = today_red & vol_ok

            # ========== 3. 近5日无三连红、无三连涨 ==========
            is_red = close_p > open_p
            is_up = close_p > close_p.shift(1)
            three_red = is_red.rolling(3).sum() == 3
            three_up = is_up.rolling(3).sum() == 3
            no_3_red = three_red.rolling(5).sum() == 0
            no_3_up = three_up.rolling(5).sum() == 0
            no_3_filter = no_3_red & no_3_up

            # ========== 4. 累计涨幅限制（不含当日） ==========
            rise_10 = (close_p.shift(1) - close_p.shift(11)) / (close_p.shift(11) + 1e-10) * 100 < 4
            rise_5 = (close_p.shift(1) - close_p.shift(6)) / (close_p.shift(6) + 1e-10) * 100 < 3
            rise_filter = rise_10 & rise_5

            # ========== 5. 当日 BBI 中轨向上 ==========
            bbi_red = bbi > bbi.shift(1)

            # ========== 6. 动能条件 ==========
            prev_macd_green = macd_bar.shift(1) < 0
            prev_kdj_green = kdj_bar.shift(1) < 0
            prev_momentum_green = prev_macd_green | prev_kdj_green
            today_macd_red = macd_bar > 0
            today_kdj_red = kdj_bar > 0
            today_both_red = today_macd_red & today_kdj_red
            macd_abs_ok = macd_bar.abs() >= macd_bar.shift(1).abs()
            momentum_ok = prev_momentum_green & today_both_red & macd_abs_ok

            # ========== 7. 周线 BBI 向上，连续抬头 ≤ 3周 ==========
            w_up = w_bbi >= w_bbi.shift(5)
            w_down = w_bbi < w_bbi.shift(5)
            w_days = self.tdx_barslast(w_down)
            w_limit = w_up & (w_days <= 15)

            # ========== 8. 近三周至少有一周绿K或下跌周（模拟周K） ==========
            # 用约5个交易日为一周，取最近完整的三周（不含当日）
            # 周1: shift(5)~shift(1)  → 开盘 open.shift(5), 收盘 close.shift(1)
            # 周2: shift(10)~shift(6)
            # 周3: shift(15)~shift(11)
            w1_green = close_p.shift(1) < open_p.shift(5)
            w1_down = close_p.shift(1) < close_p.shift(6)
            w2_green = close_p.shift(6) < open_p.shift(10)
            w2_down = close_p.shift(6) < close_p.shift(11)
            w3_green = close_p.shift(11) < open_p.shift(15)
            w3_down = close_p.shift(11) < close_p.shift(16)
            week_has_pullback = (
                (w1_green | w1_down) |
                (w2_green | w2_down) |
                (w3_green | w3_down)
            )

            # ========== 9. 当日实体上沿 > 前5日中至少2日的实体上沿 ==========
            body_top = np.maximum(close_p, open_p)
            above_cnt = (
                (body_top > body_top.shift(1)).astype(int) +
                (body_top > body_top.shift(2)).astype(int) +
                (body_top > body_top.shift(3)).astype(int) +
                (body_top > body_top.shift(4)).astype(int) +
                (body_top > body_top.shift(5)).astype(int)
            )
            body_break = above_cnt >= 2

            # ========== 最终信号 ==========
            final = (prev_down & base_curr & no_3_filter & rise_filter &
                     bbi_red & momentum_ok & w_limit &
                     week_has_pullback & body_break)

            if final.iloc[-1] == True:
                t_day = df_daily.iloc[-1]
                t1_day = df_daily.iloc[-2] if len(df_daily) > 1 else None
                pct_chg = (float(t_day['close']) / float(t1_day['close']) - 1) * 100 if t1_day is not None else 0

                r10 = (float(close_p.iloc[-2]) - float(close_p.iloc[-12])) / (float(close_p.iloc[-12]) + 1e-10) * 100 if len(close_p) >= 12 else 0
                r5 = (float(close_p.iloc[-2]) - float(close_p.iloc[-7])) / (float(close_p.iloc[-7]) + 1e-10) * 100 if len(close_p) >= 7 else 0

                w_days_val = w_days.iloc[-1]
                w_days_str = f"{int(w_days_val)}天" if pd.notna(w_days_val) else "N/A"

                return {
                    'ts_code': ts_code,
                    'name': name,
                    'current_price': round(float(t_day['close']), 2),
                    'pct_chg': round(pct_chg, 2),
                    'signal': 'BBI日周共振',
                    'bbi': round(float(bbi.iloc[-1]), 2),
                    'w_bbi': round(float(w_bbi.iloc[-1]), 2),
                    'w_days': w_days_str,
                    'rise_10': round(r10, 2),
                    'rise_5': round(r5, 2),
                    'macd_bar': round(float(macd_bar.iloc[-1]), 4),
                    'macd_prev': round(float(macd_bar.iloc[-2]), 4) if len(macd_bar) > 1 else 0,
                    'kdj_bar': round(float(kdj_bar.iloc[-1]), 4),
                    'body_above_cnt': int(above_cnt.iloc[-1]),
                    'vol_ratio': round(float(vol_p.iloc[-1] / (vol_p.iloc[-6:-1].mean() + 1e-10)), 2) if len(vol_p) >= 6 else 0,
                }, 'selected'
            return None, 'condition_fail'
        except Exception:
            return None, 'exception'

    def filter_market_stocks(self, trade_date, is_intraday=False):
        print(f"\n开始【BBI日周共振】过滤，日期: {trade_date}")
        stocks = self.get_strong_rise_stocks(trade_date, is_intraday)
        if len(stocks) == 0:
            print("T日强势预筛选后无股票，结束运行")
            return []

        stock_codes = stocks['ts_code'].tolist()
        data_dict = self.batch_fetch_daily_history(trade_date, stock_codes)

        selected = []
        filter_stats = {'selected': 0, 'data_short': 0, 'condition_fail': 0, 'exception': 0, 'no_data': 0}

        print(f"\n{CYAN}开始本地计算指标并筛选信号...{RESET}")
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {}
            for _, row in stocks.iterrows():
                info = row.to_dict()
                code = info['ts_code']
                df = data_dict.get(code)
                if df is None or df.empty:
                    filter_stats['no_data'] += 1
                    continue
                futures[executor.submit(self.process_stock_from_df, info, df, trade_date, is_intraday)] = code

            for future in tqdm(as_completed(futures), total=len(futures), desc="选股进度"):
                res, reason = future.result()
                if res:
                    selected.append(res)
                    filter_stats['selected'] += 1
                else:
                    filter_stats[reason] = filter_stats.get(reason, 0) + 1

        selected = sorted(selected, key=lambda x: x.get('pct_chg', 0), reverse=True)
        print(f"\n{GREEN}筛选完成！共选中 {len(selected)} 只股票{RESET}")
        for k, v in filter_stats.items():
            print(f"  {k}: {v}")
        return selected

    def save_results(self, trade_date, results, is_intraday=False):
        if not results:
            print("无符合条件股票")
            return
        os.makedirs(GlobalConfig.OUTPUT_DIR, exist_ok=True)
        mode = "_实时" if is_intraday else ""
        path = os.path.join(GlobalConfig.OUTPUT_DIR, f"BBI日周共振_选股_{trade_date}{mode}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"BBI日周共振选股 - {trade_date}\n")
            f.write(f"共选中 {len(results)} 只\n\n")
            for i, s in enumerate(results, 1):
                f.write(f"{i:3d}. {s['ts_code']} {s['name']} 价格:{s['current_price']:.2f} "
                        f"涨幅:{s['pct_chg']:+.2f}% 日BBI:{s['bbi']} 周BBI:{s['w_bbi']} "
                        f"周抬头:{s['w_days']} 近10日涨:{s['rise_10']}% 近5日涨:{s['rise_5']}% "
                        f"实体上破前5日数:{s['body_above_cnt']} MACD:{s['macd_bar']} KDJ:{s['kdj_bar']}\n")
        print(f"结果已保存至: {path}")

    def print_console_results(self, results):
        print("\n" + "=" * 130)
        print(f"{BOLD}{CYAN}【最终选股结果】BBI日周共振（按涨幅排序）- 共 {len(results)} 只{RESET}")
        print("=" * 130)
        if not results:
            print("无符合条件股票")
            return
        for i, s in enumerate(results, 1):
            color = RED if i % 2 == 1 else YELLOW
            print(f"\n{color}{BOLD}{'─' * 120}{RESET}")
            print(f"{color}{BOLD}  {i:2d}. {s['ts_code']}  {s['name']:<12}{RESET}")
            print(f"{color}      现价: {s['current_price']:8.2f}    涨幅: {s['pct_chg']:+7.2f}%    信号: {s['signal']}{RESET}")
            print(f"{color}      日BBI: {s['bbi']:.2f}    周BBI: {s['w_bbi']:.2f}    周线抬头持续: {s['w_days']}{RESET}")
            print(f"{color}      近10日涨幅: {s['rise_10']:+.2f}%    近5日涨幅: {s['rise_5']:+.2f}%    "
                  f"实体上破前5日: {s['body_above_cnt']}/5    量比: {s['vol_ratio']:.2f}{RESET}")
            print(f"{color}      MACD柱: {s['macd_bar']:.4f} (昨:{s['macd_prev']:.4f})    KDJ柱: {s['kdj_bar']:.4f}{RESET}")
            time.sleep(0.06)
        print(f"\n{GREEN}{BOLD}{'=' * 130}{RESET}")
        print(f"{GREEN}{BOLD}选股完成，以上共 {len(results)} 只股票符合 BBI日周共振 全部条件{RESET}")
        print(f"{GREEN}{BOLD}{'=' * 130}{RESET}\n")

def main():
    print("=" * 110)
    print(f"{BOLD} BBI日周共振 量化选股脚本 【公共缓存加速版】{RESET}")
    print("=" * 110)
    filter_tool = CustomStrategyFilter(GlobalConfig.TUSHARE_TOKEN)
    filter_tool.set_level(1)

    date_input = input("\n输入T日日期(YYYYMMDD)，回车使用今天: ").strip()
    validated_date = filter_tool.validate_trade_date(date_input)
    print(f"使用交易日: {validated_date}")
    print(f"公共缓存目录: {GlobalConfig.CACHE_DIR}")
    print(f"选股结果目录: {GlobalConfig.OUTPUT_DIR}")

    now = datetime.now()
    is_today = (validated_date == now.strftime('%Y%m%d'))
    trading_time = is_today and (now.weekday() < 5) and ((9 <= now.hour < 12) or (13 <= now.hour < 15))

    if trading_time:
        print("\n当前为交易时间，是否使用实时盘中数据？")
        mode_input = input("请输入 1=收盘模式  2=实时盘中模式 (默认2): ").strip()
        is_intraday = (mode_input != '1')
    else:
        is_intraday = False
        print("当前日期非交易日或不在交易时间，使用历史数据")

    results = filter_tool.filter_market_stocks(validated_date, is_intraday)
    filter_tool.save_results(validated_date, results, is_intraday)
    filter_tool.print_console_results(results)

if __name__ == '__main__':
    main()