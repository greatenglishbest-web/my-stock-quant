# -*- coding: utf-8 -*-
"""
通达信原版定制策略：SAR首红/次红 + BBIBOLL双轨吞没 + 动能反转（基于Tushare实时/盘后）
【速度优化 + 公共缓存版】
- 按交易日批量获取全市场日线
- 所有策略共用同一个日线缓存目录，避免重复下载
- LOOKBACK_DAYS = 500
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
    # 选股结果：本策略单独存放
    OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'SAR_BBIBOLL_选股结果')
    # ★ 公共缓存：所有策略共用（关键）
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
        # 检测是否支持 parquet
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
        """获取结束日期往前的 n_days 个交易日列表（升序）"""
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
        """返回本地已缓存的交易日集合"""
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
        print("正在加载股票池（上市≥7天 + 排除ST/北交所/银行/保险/证券）...")
        df = self.pro.stock_basic(list_status='L', fields='ts_code,name,list_date')
        today = datetime.now().strftime('%Y%m%d')
        filtered = []
        for _, row in df.iterrows():
            name = str(row['name'])
            if 'ST' in name or row['ts_code'].startswith('8') or '银行' in name or '保险' in name or '证券' in name:
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
        all_stocks = self.get_stock_pool()
        strong = []
        print("正在进行T日强势预筛选（上涨K线 + 涨幅≥1.2%）...")
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
                        if close > open_price and pct_chg >= 1.2:
                            strong.append(row.to_dict())
                except:
                    continue
        else:
            try:
                # 优先读公共缓存
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
                    mask = (df['close'] > df['open']) & (df['pct_chg'] >= 1.2)
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
                if i == 0 or pd.isna(res[i-1]):
                    res[i] = series.iloc[i]
                else:
                    res[i] = (m * series.iloc[i] + (n - m) * res[i-1]) / n
        return pd.Series(res, index=series.index)

    def calculate_sar(self, high, low, af=0.02, amax=0.2):
        length = len(high)
        sar = np.zeros(length)
        trend = np.zeros(length)
        ep = np.zeros(length)
        af_array = np.zeros(length)
        if length < 2:
            return pd.Series(sar, index=high.index)
        sar[0] = low[0]
        trend[0] = 1
        ep[0] = high[0]
        af_array[0] = af
        for i in range(1, length):
            if trend[i-1] == 1:
                sar[i] = sar[i-1] + af_array[i-1] * (ep[i-1] - sar[i-1])
                if i >= 2:
                    sar[i] = min(sar[i], low[i-1], low[i-2])
                else:
                    sar[i] = min(sar[i], low[i-1])
                if low[i] < sar[i]:
                    trend[i] = -1
                    sar[i] = ep[i-1]
                    ep[i] = low[i]
                    af_array[i] = af
                else:
                    trend[i] = 1
                    if high[i] > ep[i-1]:
                        ep[i] = high[i]
                        af_array[i] = min(af_array[i-1] + af, amax)
                    else:
                        ep[i] = ep[i-1]
                        af_array[i] = af_array[i-1]
            else:
                sar[i] = sar[i-1] + af_array[i-1] * (ep[i-1] - sar[i-1])
                if i >= 2:
                    sar[i] = max(sar[i], high[i-1], high[i-2])
                else:
                    sar[i] = max(sar[i], high[i-1])
                if high[i] > sar[i]:
                    trend[i] = 1
                    sar[i] = ep[i-1]
                    ep[i] = high[i]
                    af_array[i] = af
                else:
                    trend[i] = -1
                    if low[i] < ep[i-1]:
                        ep[i] = low[i]
                        af_array[i] = min(af_array[i-1] + af, amax)
                    else:
                        ep[i] = ep[i-1]
                        af_array[i] = af_array[i-1]
        return pd.Series(sar, index=high.index)

    # ==================== 增量缓存拉取 ====================
    def batch_fetch_daily_history(self, trade_date, stock_codes):
        """
        增量缓存策略（公共缓存）：
        1. 计算需要的最近 LOOKBACK_DAYS 个交易日
        2. 公共缓存已有的直接读
        3. 缺失的才调用 API 下载并写入公共缓存
        4. 合并后只保留目标股票
        """
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
        ts_code = stock_info['ts_code']
        name = stock_info.get('name', '')
        try:
            if len(df_daily) < 50:
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

            KC_MID = close_p.ewm(span=20, adjust=False).mean()
            ma3 = close_p.rolling(3).mean()
            ma6 = close_p.rolling(6).mean()
            ma12 = close_p.rolling(12).mean()
            ma24 = close_p.rolling(24).mean()
            BBI = (ma3 + ma6 + ma12 + ma24) / 4
            STD_BBI = BBI.rolling(11).std(ddof=0)
            UPR = BBI + 3 * STD_BBI
            DWN = BBI - 3 * STD_BBI
            SAR_VAL = self.calculate_sar(high_p, low_p, af=0.02, amax=0.2)

            ema8 = close_p.ewm(span=8, adjust=False).mean()
            ema20 = close_p.ewm(span=20, adjust=False).mean()
            DIF8 = ema8 - ema20
            DEA6 = DIF8.ewm(span=6, adjust=False).mean()
            MACD_BAR = 2 * (DIF8 - DEA6)

            low_min = low_p.rolling(14).min()
            high_max = high_p.rolling(14).max()
            RSV = (close_p - low_min) / (high_max - low_min + 1e-10) * 100
            K = self.TDX_SMA(RSV, 3, 1)
            D = self.TDX_SMA(K, 3, 1)
            KDJ_BAR = 2 * (K - D)

            PREV_DOWN = (close_p.shift(1) < open_p.shift(1)) | (close_p.shift(1) < close_p.shift(2))
            PREV_BELOW = low_p.shift(1) < KC_MID.shift(1)
            C_PREV = PREV_DOWN & PREV_BELOW

            SAR_RED = close_p > SAR_VAL
            SAR_RED_COUNT_5 = SAR_RED.rolling(5).sum()
            C_SAR_FIRST = SAR_RED & (SAR_RED_COUNT_5 <= 2)
            C_RED = (close_p > open_p) & (close_p > close_p.shift(1))
            VOL_OK = vol_p / (vol_p.rolling(5).mean().shift(1) + 1e-10) > 1.2
            BASE_CURR = C_RED & VOL_OK

            UPR_UP = UPR > UPR.shift(1)
            MID_UP = BBI > BBI.shift(1)
            DWN_UP = DWN > DWN.shift(1)
            I_UPR = (open_p <= UPR) & (close_p >= UPR) & UPR_UP
            I_MID = (open_p <= BBI) & (close_p >= BBI) & MID_UP
            I_DWN = (open_p <= DWN) & (close_p >= DWN) & DWN_UP
            INC_CNT = I_UPR.astype(int) + I_MID.astype(int) + I_DWN.astype(int)
            INC_COND = INC_CNT >= 2

            MACD_TURN = (MACD_BAR > 0) & (MACD_BAR.shift(1) <= 0)
            KDJ_TURN = (KDJ_BAR > 0) & (KDJ_BAR.shift(1) <= 0)
            MOMENTUM_OK = MACD_TURN | KDJ_TURN

            FINAL_SIGNAL = C_PREV & C_SAR_FIRST & BASE_CURR & INC_COND & MOMENTUM_OK

            if FINAL_SIGNAL.iloc[-1] == True:
                t_day = df_daily.iloc[-1]
                t1_day = df_daily.iloc[-2] if len(df_daily) > 1 else None
                pct_chg = (t_day['close'] / t1_day['close'] - 1) * 100 if t1_day is not None else 0
                return {
                    'ts_code': ts_code,
                    'name': name,
                    'current_price': round(float(t_day['close']), 2),
                    'pct_chg': round(pct_chg, 2),
                    'signal': f"SAR首红+BBIBOLL吞没({int(INC_CNT.iloc[-1])}轨)",
                    'inc_count': int(INC_CNT.iloc[-1]),
                    'sar_count': float(SAR_RED_COUNT_5.iloc[-1]),
                    'bbiboll_mid': round(float(BBI.iloc[-1]), 2),
                    'keltner_mid': round(float(KC_MID.iloc[-1]), 2),
                    'macd_bar': round(float(MACD_BAR.iloc[-1]), 4),
                    'kdj_bar': round(float(KDJ_BAR.iloc[-1]), 4),
                }, 'selected'
            return None, 'condition_fail'
        except Exception:
            return None, 'exception'

    def filter_market_stocks(self, trade_date, is_intraday=False):
        print(f"\n开始【SAR首红+BBIBOLL吞没】过滤，日期: {trade_date}")
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

        selected = sorted(selected, key=lambda x: (x.get('pct_chg', 0), x.get('inc_count', 0)), reverse=True)
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
        path = os.path.join(GlobalConfig.OUTPUT_DIR, f"SAR_BBIBOLL_选股_{trade_date}{mode}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"SAR首红+BBIBOLL双轨吞没 - {trade_date}\n")
            f.write(f"共选中 {len(results)} 只\n\n")
            for i, s in enumerate(results, 1):
                f.write(f"{i:3d}. {s['ts_code']} {s['name']} 价格:{s['current_price']:.2f} "
                        f"涨幅:{s['pct_chg']:+.2f}% 信号:{s['signal']} "
                        f"BBIBOLL中轨:{s['bbiboll_mid']} 吞没轨数:{s['inc_count']} "
                        f"SAR红点:{s['sar_count']:.0f} 肯特纳:{s['keltner_mid']}\n")
        print(f"结果已保存至: {path}")

    def print_console_results(self, results):
        print("\n" + "=" * 130)
        print(f"{BOLD}{CYAN}【最终选股结果】（按涨幅优先排序）- 共 {len(results)} 只{RESET}")
        print("=" * 130)
        if not results:
            print("无符合条件股票")
            return
        for i, s in enumerate(results, 1):
            color = RED if i % 2 == 1 else YELLOW
            print(f"\n{color}{BOLD}{'─' * 120}{RESET}")
            print(f"{color}{BOLD}  {i:2d}. {s['ts_code']}  {s['name']:<12}{RESET}")
            print(f"{color}      现价: {s['current_price']:8.2f}    涨幅: {s['pct_chg']:+7.2f}%    信号: {s['signal']}{RESET}")
            print(f"{color}      吞没轨数: {s['inc_count']}    SAR近5日红点数: {s['sar_count']:.0f}    "
                  f"BBIBOLL中轨: {s['bbiboll_mid']:.2f}    肯特纳中轨: {s['keltner_mid']:.2f}{RESET}")
            print(f"{color}      MACD柱: {s.get('macd_bar', 0):.4f}    KDJ柱: {s.get('kdj_bar', 0):.4f}{RESET}")
            time.sleep(0.06)
        print(f"\n{GREEN}{BOLD}{'=' * 130}{RESET}")
        print(f"{GREEN}{BOLD}选股完成，以上共 {len(results)} 只股票符合 SAR首红 + BBIBOLL双轨吞没 + 动能反转 条件{RESET}")
        print(f"{GREEN}{BOLD}{'=' * 130}{RESET}\n")

def main():
    print("=" * 110)
    print(f"{BOLD} SAR首红/次红 + BBIBOLL双轨吞没 + 动能反转 量化选股脚本 【公共缓存加速版】{RESET}")
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