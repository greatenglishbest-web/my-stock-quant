# -*- coding: utf-8 -*-
"""
选股策略：BBI强斜率拐头 + 三指标共振 + 周线护航（斜率狙击版）
【基于公共缓存版脚本改造，忠实还原通达信公式】
  基础过滤：排除 ST/北交所/停牌（当日开盘价>0）
  0. 前一日绿K + 今日放量实体红K + 实体上沿抬升
     PREV_GREEN: REF(C,1)<REF(O,1)
     IS_RED_UP : (C>O) AND (C>REF(C,1))
     VOL_OK    : V / REF(MA(V,5),1) > 1.2
     EDGE_UP   : MAX(C,O) > REF(MAX(C,O),1)
  1. BBI(3,5,15,22) 严格拐头 + 强斜率狙击(0.18% < BBI_SLP < 1.5%)
     + 近10日无连续两日 BBI 单日跌超 1%（防瀑布）
  2. MACD(8,20,6)：DIF>DEA 或 金叉
  3. KDJ(14,3,3) ：K>D 或 金叉
  4. ROC(12,6)  ：ROC>MAROC 或 金叉
  5. 日线合成：以上全部 AND
  6. 防坑：近3日禁止三连红/三连涨 + 当日高开≤2% + 近5日有2日、近10日有3日最低价<BBI（探底洗盘）
  7. 周线 RSI(70) 向上（W_RSI 今日 > 5周前）
  8. 周线级别排除三连阳/三连红
"""
import tushare as ts
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import os
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import time
import random
import glob

warnings.filterwarnings('ignore')

RED = '\033[91m'
YELLOW = '\033[93m'
GREEN = '\033[92m'
CYAN = '\033[96m'
BOLD = '\033[1m'
RESET = '\033[0m'

REQUIRED_DAILY_COLS = {'ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'vol', 'pct_chg'}
REQUIRED_WEEKLY_COLS = {'ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'vol'}

if os.name == 'nt':
    os.system('')
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 0x0007 | 0x0004)
    except:
        pass


class GlobalConfig:
    TUSHARE_TOKEN = 'a91652800def39d213abec3af9563e4485e83940bfbbd8276f8adaa8'
    OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'BBI_ROC_MACD_KDJ_选股结果')
    CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股日线公共缓存')
    WEEKLY_CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股周线公共缓存')
    LOOKBACK_DAYS = 500
    LOOKBACK_WEEKS = 80
    USE_PARQUET = True
    EXCLUDE_RECENT_SUSPENSION = True
    SUSPENSION_LOOKBACK_DAYS = 20


class CustomStrategyFilter:
    def __init__(self, token):
        self.pro = ts.pro_api(token)
        self.rt_data_dict = {}
        self.level = 1
        self.verbose = True
        self._rate_limited = False
        os.makedirs(GlobalConfig.CACHE_DIR, exist_ok=True)
        os.makedirs(GlobalConfig.WEEKLY_CACHE_DIR, exist_ok=True)
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
                    print(f"{YELLOW}未检测到 pyarrow/fastparquet，缓存使用 CSV{RESET}")

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
                        print(f"{YELLOW}[警告] API限流，等待 {wait}s...{RESET}")
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
        cal = self._call_with_retry(self.pro.trade_cal, start_date=start_approx, end_date=end_date, is_open='1')
        dates = sorted(cal['cal_date'].tolist())
        return dates[-n_days:] if len(dates) >= n_days else dates

    # ---------- 日线缓存 ----------
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
            print(f"  保存日线缓存 {trade_date} 失败: {e}")

    def load_day_from_cache(self, trade_date):
        path = self._cache_path(trade_date)
        if not os.path.exists(path):
            return None
        try:
            df = pd.read_parquet(path) if self.use_parquet else pd.read_csv(path)
            if not REQUIRED_DAILY_COLS.issubset(df.columns):
                print(f"  日线缓存 {trade_date} 字段不完整，删除重建")
                try:
                    os.remove(path)
                except Exception:
                    pass
                return None
            return df
        except Exception as e:
            print(f"  读取日线缓存 {trade_date} 失败: {e}")
            return None

    def get_cached_dates(self):
        pattern = os.path.join(GlobalConfig.CACHE_DIR, '*.parquet' if self.use_parquet else '*.csv')
        dates = set()
        for f in glob.glob(pattern):
            d = os.path.basename(f).split('.')[0]
            if len(d) == 8 and d.isdigit():
                dates.add(d)
        return dates

    # ---------- 周线缓存 ----------
    def _weekly_cache_path(self, week_end_date):
        ext = 'parquet' if self.use_parquet else 'csv'
        return os.path.join(GlobalConfig.WEEKLY_CACHE_DIR, f"{week_end_date}.{ext}")

    def save_week_to_cache(self, week_end_date, df):
        if df is None or df.empty:
            return
        path = self._weekly_cache_path(week_end_date)
        try:
            if self.use_parquet:
                df.to_parquet(path, index=False)
            else:
                df.to_csv(path, index=False, encoding='utf-8-sig')
        except Exception as e:
            print(f"  保存周线缓存 {week_end_date} 失败: {e}")

    def load_week_from_cache(self, week_end_date):
        path = self._weekly_cache_path(week_end_date)
        if not os.path.exists(path):
            return None
        try:
            df = pd.read_parquet(path) if self.use_parquet else pd.read_csv(path)
            if not REQUIRED_WEEKLY_COLS.issubset(df.columns):
                print(f"  周线缓存 {week_end_date} 字段不完整，删除重建")
                try:
                    os.remove(path)
                except Exception:
                    pass
                return None
            return df
        except Exception as e:
            print(f"  读取周线缓存 {week_end_date} 失败: {e}")
            return None

    def get_cached_week_dates(self):
        pattern = os.path.join(GlobalConfig.WEEKLY_CACHE_DIR, '*.parquet' if self.use_parquet else '*.csv')
        dates = set()
        for f in glob.glob(pattern):
            d = os.path.basename(f).split('.')[0]
            if len(d) == 8 and d.isdigit():
                dates.add(d)
        return dates

    def get_week_end_dates(self, end_date, n_weeks=80):
        start_approx = (datetime.strptime(end_date, '%Y%m%d') - timedelta(days=int(n_weeks * 10))).strftime('%Y%m%d')
        cal = self._call_with_retry(
            self.pro.trade_cal, start_date=start_approx, end_date=end_date, is_open='1'
        )
        cal = cal.sort_values('cal_date')
        cal['dt'] = pd.to_datetime(cal['cal_date'])
        cal['week'] = cal['dt'].dt.to_period('W-FRI')
        week_ends = cal.groupby('week')['cal_date'].max().tolist()
        week_ends = sorted([d for d in week_ends if d <= end_date])
        return week_ends[-n_weeks:] if len(week_ends) >= n_weeks else week_ends

    def get_stock_pool(self):
        print("正在加载股票池（排除ST/北交所 + 上市≥7天）...")
        df = self.pro.stock_basic(list_status='L', fields='ts_code,name,list_date')
        today = datetime.now().strftime('%Y%m%d')
        filtered = []
        for _, row in df.iterrows():
            name, code = str(row['name']), row['ts_code']
            if 'ST' in name or code.startswith('8'):
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
                print(f"获取 {pattern} 失败: {str(e)[:50]}")
                time.sleep(3)
        if rt_dfs:
            full_df = pd.concat(rt_dfs, ignore_index=True).drop_duplicates(subset=['ts_code'])
            self.rt_data_dict = full_df.set_index('ts_code').to_dict('index')
            print(f"实时数据获取完成，共 {len(self.rt_data_dict)} 只")

    def get_candidate_stocks(self, trade_date, is_intraday=False):
        all_stocks = self.get_stock_pool()
        strong = []
        print("正在预筛：当日涨幅>0 ...")
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
                    if pre_close > 0 and (close - pre_close) / pre_close * 100 > 0:
                        strong.append(row.to_dict())
                except:
                    continue
        else:
            try:
                df = self.load_day_from_cache(trade_date)
                if df is None or df.empty:
                    df = self._call_with_retry(
                        self.pro.daily, trade_date=trade_date,
                        fields='ts_code,trade_date,open,close,pct_chg,high,low,vol'
                    )
                    if df is not None and not df.empty:
                        self.save_day_to_cache(trade_date, df)
                if df is not None and not df.empty:
                    df['pct_chg'] = pd.to_numeric(df.get('pct_chg', np.nan), errors='coerce')
                    mask = df['pct_chg'] > 0
                    codes = [c for c in df.loc[mask, 'ts_code'].tolist() if c in set(all_stocks['ts_code'])]
                    strong = all_stocks[all_stocks['ts_code'].isin(codes)].to_dict('records')
            except Exception as e:
                print(f"预筛失败: {e}")
        strong_df = pd.DataFrame(strong)
        print(f"预筛完成：共 {len(strong_df)} 只")
        return strong_df

    def TDX_SMA(self, series, n, m):
        res = np.zeros(len(series))
        for i in range(len(series)):
            if pd.isna(series.iloc[i]):
                res[i] = np.nan
            elif i == 0 or pd.isna(res[i - 1]):
                res[i] = series.iloc[i]
            else:
                res[i] = (m * series.iloc[i] + (n - m) * res[i - 1]) / n
        return pd.Series(res, index=series.index)

    def _download_daily_dates(self, dates):
        failed = []
        for i, d in enumerate(tqdm(dates, desc="日线增量")):
            try:
                df = self._call_with_retry(
                    self.pro.daily, trade_date=d,
                    fields='ts_code,trade_date,open,high,low,close,vol,pct_chg'
                )
                if df is not None and not df.empty:
                    self.save_day_to_cache(d, df)
                time.sleep(1.2 if (i + 1) % 20 == 0 else 0.2)
            except Exception as e:
                print(f"  下载日线 {d} 失败: {str(e)[:60]}")
                failed.append(d)
                time.sleep(1)
        for rnd in range(3):
            if not failed:
                break
            print(f"{YELLOW}日线重试第 {rnd + 1} 轮，剩余 {len(failed)} 天{RESET}")
            time.sleep(5 * (rnd + 1))
            still = []
            for d in failed:
                try:
                    df = self._call_with_retry(
                        self.pro.daily, trade_date=d,
                        fields='ts_code,trade_date,open,high,low,close,vol,pct_chg'
                    )
                    if df is not None and not df.empty:
                        self.save_day_to_cache(d, df)
                    else:
                        still.append(d)
                except Exception:
                    still.append(d)
                time.sleep(0.5)
            failed = still
        return failed

    def batch_fetch_daily_history(self, trade_date, stock_codes):
        print(f"\n{CYAN}准备日线（公共缓存 + 增量）...{RESET}")
        trade_dates = self.get_trade_dates(trade_date, GlobalConfig.LOOKBACK_DAYS)
        if not trade_dates:
            return {}, set()
        print(f"日线范围: {trade_dates[0]} ~ {trade_dates[-1]}，共 {len(trade_dates)} 天")
        cached = self.get_cached_dates()
        need = [d for d in trade_dates if d not in cached]
        print(f"日线缓存已有 {len(trade_dates) - len(need)} | 需下载 {len(need)}")
        if need:
            failed = self._download_daily_dates(need)
            if failed:
                print(f"{YELLOW}警告：日线下载最终失败日期 {len(failed)} 个: {failed[:10]}...{RESET}")

        all_dfs = []
        loaded_dates = []
        code_set = set(stock_codes)
        for d in tqdm(trade_dates, desc="加载日线"):
            df = self.load_day_from_cache(d)
            if df is not None and not df.empty:
                loaded_dates.append(d)
                df = df[df['ts_code'].isin(code_set)]
                if not df.empty:
                    all_dfs.append(df)
        if not all_dfs:
            return {}, set()

        full = pd.concat(all_dfs, ignore_index=True)
        full['trade_date'] = full['trade_date'].astype(str)
        full = full.sort_values(['ts_code', 'trade_date'])

        daily_dict = {}
        for code, g in full.groupby('ts_code'):
            daily_dict[code] = g.reset_index(drop=True)

        suspended_codes = set()
        if GlobalConfig.EXCLUDE_RECENT_SUSPENSION and loaded_dates:
            recent_dates = set(loaded_dates[-GlobalConfig.SUSPENSION_LOOKBACK_DAYS:])
            for code, g in daily_dict.items():
                present = set(g['trade_date'])
                if any(d not in present for d in recent_dates):
                    suspended_codes.add(code)
            if suspended_codes:
                print(f"{YELLOW}近期停牌/复牌股 {len(suspended_codes)} 只将被排除{RESET}")

        return daily_dict, suspended_codes

    def _download_weekly_dates(self, dates):
        failed = []
        for i, d in enumerate(tqdm(dates, desc="周线增量")):
            try:
                df = self._call_with_retry(
                    self.pro.weekly, trade_date=d,
                    fields='ts_code,trade_date,open,high,low,close,vol'
                )
                if df is not None and not df.empty:
                    self.save_week_to_cache(d, df)
                time.sleep(1.5 if (i + 1) % 10 == 0 else 0.35)
            except Exception as e:
                print(f"  下载周线 {d} 失败: {str(e)[:60]}")
                failed.append(d)
                time.sleep(1)
        for rnd in range(3):
            if not failed:
                break
            print(f"{YELLOW}周线重试第 {rnd + 1} 轮，剩余 {len(failed)} 周{RESET}")
            time.sleep(5 * (rnd + 1))
            still = []
            for d in failed:
                try:
                    df = self._call_with_retry(
                        self.pro.weekly, trade_date=d,
                        fields='ts_code,trade_date,open,high,low,close,vol'
                    )
                    if df is not None and not df.empty:
                        self.save_week_to_cache(d, df)
                    else:
                        still.append(d)
                except Exception:
                    still.append(d)
                time.sleep(0.5)
            failed = still
        return failed

    def batch_fetch_weekly_history(self, trade_date, stock_codes):
        print(f"\n{CYAN}准备周线（pro.weekly + 公共缓存）...{RESET}")
        week_ends = self.get_week_end_dates(trade_date, GlobalConfig.LOOKBACK_WEEKS)
        if not week_ends:
            return {}, []
        # 关键修复：始终排除 T 日当周，本周 K 由日线聚合
        complete_ends = [d for d in week_ends if d < trade_date]
        if not complete_ends:
            return {}, []
        cached = self.get_cached_week_dates()
        need = [d for d in complete_ends if d not in cached]
        print(f"完整周结束日 {len(complete_ends)} | 缓存 {len(complete_ends) - len(need)} | 需下载 {len(need)}")
        if need:
            failed = self._download_weekly_dates(need)
            if failed:
                print(f"{YELLOW}警告：周线下载最终失败日期 {len(failed)} 个: {failed[:10]}...{RESET}")

        all_dfs = []
        code_set = set(stock_codes)
        for d in tqdm(complete_ends, desc="加载周线"):
            df = self.load_week_from_cache(d)
            if df is not None and not df.empty:
                df = df[df['ts_code'].isin(code_set)]
                if not df.empty:
                    all_dfs.append(df)
        weekly_dict = {}
        if all_dfs:
            full = pd.concat(all_dfs, ignore_index=True)
            full['trade_date'] = full['trade_date'].astype(str)
            full = full.sort_values(['ts_code', 'trade_date'])
            weekly_dict = {code: g.reset_index(drop=True) for code, g in full.groupby('ts_code')}
        print(f"{GREEN}完整周线就绪，共 {len(weekly_dict)} 只{RESET}")
        return weekly_dict, complete_ends

    def build_weekly_with_current(self, df_weekly_complete, df_daily, trade_date, last_complete_week_end):
        cols = ['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'vol']
        if df_weekly_complete is not None and not df_weekly_complete.empty:
            w = df_weekly_complete[[c for c in cols if c in df_weekly_complete.columns]].copy()
            w = w.sort_values('trade_date').reset_index(drop=True)
        else:
            w = pd.DataFrame(columns=cols)
        d = df_daily.copy()
        d['trade_date'] = d['trade_date'].astype(str)
        mask = (d['trade_date'] > str(last_complete_week_end)) & (d['trade_date'] <= str(trade_date))
        cur = d.loc[mask]
        if cur.empty:
            return w
        if not w.empty and str(w['trade_date'].iloc[-1]) >= str(trade_date):
            return w
        partial = {
            'ts_code': cur['ts_code'].iloc[-1] if 'ts_code' in cur.columns else '',
            'trade_date': str(trade_date),
            'open': float(cur['open'].iloc[0]),
            'high': float(cur['high'].max()),
            'low': float(cur['low'].min()),
            'close': float(cur['close'].iloc[-1]),
            'vol': float(cur['vol'].sum()),
        }
        return pd.concat([w, pd.DataFrame([partial])], ignore_index=True)

    def process_stock_from_df(self, stock_info, df_daily, df_weekly_complete,
                              trade_date, last_complete_week_end, is_intraday=False):
        ts_code = stock_info['ts_code']
        name = stock_info.get('name', '')
        try:
            if len(df_daily) < 130:
                return None, 'data_short'

            # 盘中模式：用实时行情覆盖/追加 T 日
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
                    pre_close = float(rt.get('pre_close', rt['close']))
                    df_daily.at[idx, 'pct_chg'] = (float(rt['close']) / pre_close - 1) * 100 if pre_close > 0 else 0.0
                else:
                    pre_close = float(rt.get('pre_close', rt['close']))
                    new_row = {
                        'ts_code': ts_code, 'trade_date': trade_date,
                        'open': float(rt['open']), 'high': float(rt['high']),
                        'low': float(rt['low']), 'close': float(rt['close']),
                        'vol': float(rt['vol']),
                        'pct_chg': (float(rt['close']) / pre_close - 1) * 100 if pre_close > 0 else 0.0
                    }
                    df_daily = pd.concat([df_daily, pd.DataFrame([new_row])], ignore_index=True)

            if str(df_daily.iloc[-1]['trade_date']) != str(trade_date):
                return None, 'no_data'

            # 基础过滤：当日开盘价 > 0（PT）
            t_day = df_daily.iloc[-1]
            if not (float(t_day['open']) > 0):
                return None, 'condition_fail'

            close_p = pd.Series(df_daily['close'].values.astype(float))
            high_p = pd.Series(df_daily['high'].values.astype(float))
            low_p = pd.Series(df_daily['low'].values.astype(float))
            open_p = pd.Series(df_daily['open'].values.astype(float))
            vol_p = pd.Series(df_daily['vol'].values.astype(float))

            # ========== 0. 前绿 + 今日实体红K放量上攻 ==========
            prev_green = close_p.shift(1) < open_p.shift(1)
            is_red_up = (close_p > open_p) & (close_p > close_p.shift(1))
            ma5_v = vol_p.rolling(5).mean()
            vol_ok = vol_p / ma5_v.shift(1) > 1.2
            edge_today = np.maximum(close_p.values, open_p.values)
            edge_prev = pd.Series(edge_today).shift(1)
            edge_up = pd.Series(edge_today) > edge_prev

            # ========== 1. BBI(3,5,15,22) 严格拐头 + 强斜率 + 防瀑布 ==========
            bbi = (close_p.rolling(3).mean() + close_p.rolling(5).mean() +
                   close_p.rolling(15).mean() + close_p.rolling(22).mean()) / 4
            bbi_up = bbi > bbi.shift(1)
            bbi_turn = bbi_up & (bbi.shift(1) <= bbi.shift(2))
            bbi_slp = (bbi - bbi.shift(1)) / bbi.shift(1) * 100
            bbi_strong = (bbi_slp > 0.18) & (bbi_slp < 1.5)
            steep_dn = bbi_slp < -1.0
            consec_dn = steep_dn & steep_dn.shift(1).fillna(False)
            no_consec_drop = consec_dn.rolling(10, min_periods=1).sum() == 0

            # ========== 2. MACD(8,20,6)：DIF>DEA 或 金叉 ==========
            dif = close_p.ewm(span=8, adjust=False).mean() - close_p.ewm(span=20, adjust=False).mean()
            dea = dif.ewm(span=6, adjust=False).mean()
            macd_bar = 2 * (dif - dea)
            macd_cross = (dif > dea) & (dif.shift(1) <= dea.shift(1))
            macd_ok = (dif > dea) | macd_cross

            # ========== 3. KDJ(14,3,3)：K>D 或 金叉 ==========
            llv = low_p.rolling(14).min()
            hhv = high_p.rolling(14).max()
            rsv = (close_p - llv) / (hhv - llv + 1e-10) * 100
            k_line = self.TDX_SMA(rsv, 3, 1)
            d_line = self.TDX_SMA(k_line, 3, 1)
            kdj_cross = (k_line > d_line) & (k_line.shift(1) <= d_line.shift(1))
            kdj_ok = (k_line > d_line) | kdj_cross

            # ========== 4. ROC(12,6)：ROC>MAROC 或 金叉 ==========
            roc = (close_p / close_p.shift(12) - 1.0) * 100
            maroc = roc.rolling(6).mean()
            roc_cross = (roc > maroc) & (roc.shift(1) <= maroc.shift(1))
            roc_ok = (roc > maroc) | roc_cross

            # ========== 5. 日线技术合成 ==========
            daily_ok = bbi_turn & bbi_strong & no_consec_drop & macd_ok & kdj_ok & roc_ok

            # ========== 6. 防坑与探底 ==========
            no_3_red = (close_p > open_p).rolling(3).sum() < 3
            no_3_up = (close_p > close_p.shift(1)).rolling(3).sum() < 3
            no_triple = no_3_red & no_3_up
            no_high_open = open_p <= close_p.shift(1) * 1.02
            low_below_5 = (low_p < bbi).rolling(5, min_periods=5).sum() >= 2
            low_below_10 = (low_p < bbi).rolling(10, min_periods=10).sum() >= 3

            # 首轮过滤（日线层面，不含周线）
            first_check = (prev_green & is_red_up & vol_ok & edge_up &
                           daily_ok & no_triple & no_high_open &
                           low_below_5 & low_below_10)
            if not bool(first_check.iloc[-1]):
                return None, 'condition_fail'

            # ========== 7. 周线 RSI(70) 向上 + 排除周线三连阳/三连红 ==========
            wdf = self.build_weekly_with_current(
                df_weekly_complete, df_daily, trade_date, last_complete_week_end
            )
            if wdf is None or len(wdf) < 10:
                return None, 'data_short'

            wc = pd.Series(wdf['close'].values.astype(float))
            wo = pd.Series(wdf['open'].values.astype(float))

            # W_RSI := SMA(MAX(C-REF(C,1),0),70,1) / SMA(ABS(C-REF(C,1)),70,1) * 100
            w_diff = wc.diff()
            w_up = w_diff.clip(lower=0).fillna(0.0)
            w_abs = w_diff.abs().fillna(0.0)
            # SMA(X,70,1) 等价于 ewm(alpha=1/70, adjust=False)
            w_up_s = w_up.ewm(alpha=1.0 / 70.0, adjust=False).mean()
            w_abs_s = w_abs.ewm(alpha=1.0 / 70.0, adjust=False).mean()
            w_rsi = w_up_s / (w_abs_s + 1e-10) * 100
            if len(w_rsi) <= 5:
                return None, 'data_short'
            w_rsi_up = float(w_rsi.iloc[-1]) > float(w_rsi.iloc[-6])

            # 周线三连阳/三连红（需最近4根周线）
            if len(wdf) < 4:
                return None, 'data_short'
            wk1_c, wk1_o = float(wc.iloc[-1]), float(wo.iloc[-1])
            wk2_c, wk2_o = float(wc.iloc[-2]), float(wo.iloc[-2])
            wk3_c, wk3_o = float(wc.iloc[-3]), float(wo.iloc[-3])
            wk4_c = float(wc.iloc[-4])
            consec_3_red = (wk1_c > wk1_o) and (wk2_c > wk2_o) and (wk3_c > wk3_o)
            consec_3_up = (wk1_c > wk2_c) and (wk2_c > wk3_c) and (wk3_c > wk4_c)
            no_wk_3_triple = not (consec_3_red or consec_3_up)

            if not (w_rsi_up and no_wk_3_triple):
                return None, 'condition_fail'

            # ========== 输出 ==========
            pct_chg = float(t_day['pct_chg']) if pd.notna(t_day.get('pct_chg')) else 0.0

            sig_list = ["BBI强拐"]
            if macd_cross.iloc[-1]:
                sig_list.append("MACD金叉")
            if kdj_cross.iloc[-1]:
                sig_list.append("KDJ金叉")
            if roc_cross.iloc[-1]:
                sig_list.append("ROC金叉")
            sig_list.append("周RSI↑")
            sig_str = "+".join(sig_list)

            return {
                'ts_code': ts_code,
                'name': name,
                'current_price': round(float(t_day['close']), 2),
                'pct_chg': round(pct_chg, 2),
                'signal': sig_str,
                'daily_sig': sig_str,
                'bbi': round(float(bbi.iloc[-1]), 2),
                'bbi_slp': round(float(bbi_slp.iloc[-1]), 4),
                'roc': round(float(roc.iloc[-1]), 4),
                'maroc': round(float(maroc.iloc[-1]), 4),
                'w_rsi': round(float(w_rsi.iloc[-1]), 2),
                'w_rsi_prev': round(float(w_rsi.iloc[-6]), 2),
                'macd_bar': round(float(macd_bar.iloc[-1]), 4),
                'dif': round(float(dif.iloc[-1]), 4),
                'dea': round(float(dea.iloc[-1]), 4),
                'k': round(float(k_line.iloc[-1]), 2),
                'd': round(float(d_line.iloc[-1]), 2),
                'vol_ratio': round(float(vol_p.iloc[-1] / (ma5_v.iloc[-2] + 1e-10)), 3) if len(ma5_v) >= 2 and pd.notna(ma5_v.iloc[-2]) else 0,
            }, 'selected'
        except Exception:
            return None, 'exception'

    def filter_market_stocks(self, trade_date, is_intraday=False):
        print(f"\n开始【BBI强斜率拐头+三指标共振+周线护航】过滤，日期: {trade_date}")
        stocks = self.get_candidate_stocks(trade_date, is_intraday)
        if len(stocks) == 0:
            print("候选池为空")
            return []
        if is_intraday and not self.rt_data_dict:
            self.fetch_rt_k_batch()

        stock_codes = stocks['ts_code'].tolist()
        daily_dict, suspended_codes = self.batch_fetch_daily_history(trade_date, stock_codes)
        weekly_dict, complete_ends = self.batch_fetch_weekly_history(trade_date, stock_codes)
        last_complete = complete_ends[-1] if complete_ends else ''
        print(f"上一完整周结束日: {last_complete} | 本周聚合至: {trade_date}")

        selected = []
        stats = {'selected': 0, 'data_short': 0, 'condition_fail': 0,
                 'exception': 0, 'no_data': 0, 'suspended': 0}

        print(f"\n{CYAN}本地计算...{RESET}")
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {}
            for _, row in stocks.iterrows():
                info = row.to_dict()
                code = info['ts_code']
                if code in suspended_codes:
                    stats['suspended'] += 1
                    continue
                ddf = daily_dict.get(code)
                if ddf is None or ddf.empty:
                    stats['no_data'] += 1
                    continue
                wdf = weekly_dict.get(code)
                futures[executor.submit(
                    self.process_stock_from_df, info, ddf, wdf,
                    trade_date, last_complete, is_intraday
                )] = code
            for future in tqdm(as_completed(futures), total=len(futures), desc="选股进度"):
                res, reason = future.result()
                if res:
                    selected.append(res)
                    stats['selected'] += 1
                else:
                    stats[reason] = stats.get(reason, 0) + 1

        selected = sorted(selected, key=lambda x: x.get('pct_chg', 0), reverse=True)
        print(f"\n{GREEN}筛选完成！共 {len(selected)} 只{RESET}")
        for k, v in stats.items():
            print(f"  {k}: {v}")
        return selected

    def save_results(self, trade_date, results, is_intraday=False):
        if not results:
            print("无符合条件股票")
            return
        os.makedirs(GlobalConfig.OUTPUT_DIR, exist_ok=True)
        mode = "_实时" if is_intraday else ""
        path = os.path.join(GlobalConfig.OUTPUT_DIR, f"BBI强斜率三共振周RSI_{trade_date}{mode}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"BBI强斜率拐头+三指标共振+周线护航 - {trade_date}\n共选中 {len(results)} 只\n\n")
            for i, s in enumerate(results, 1):
                f.write(f"{i:3d}. {s['ts_code']} {s['name']} 价:{s['current_price']:.2f} "
                        f"涨:{s['pct_chg']:+.2f}% 量比:{s['vol_ratio']} "
                        f"信号:{s['signal']} BBI:{s['bbi']} 斜率:{s['bbi_slp']:.3f}% "
                        f"ROC:{s['roc']:.3f} 周RSI:{s['w_rsi']:.1f}\n")
        print(f"结果已保存: {path}")

    def print_console_results(self, results):
        print("\n" + "=" * 130)
        print(f"{BOLD}{CYAN}【结果】BBI强斜率拐头+三指标共振+周线护航 - 共 {len(results)} 只{RESET}")
        print("=" * 130)
        if not results:
            print("无符合条件股票")
            return
        for i, s in enumerate(results, 1):
            color = RED if i % 2 == 1 else YELLOW
            print(f"\n{color}{BOLD}{'─' * 120}{RESET}")
            print(f"{color}{BOLD}  {i:2d}. {s['ts_code']}  {s['name']:<12}{RESET}")
            print(f"{color}      现价: {s['current_price']:8.2f}  涨幅: {s['pct_chg']:+7.2f}%  "
                  f"量比(对比MA5): {s['vol_ratio']:.3f}{RESET}")
            print(f"{color}      信号: {s['signal']}{RESET}")
            print(f"{color}      BBI: {s['bbi']:.2f}  斜率: {s['bbi_slp']:+.4f}%  "
                  f"ROC: {s['roc']:.4f}  MAROC: {s['maroc']:.4f}{RESET}")
            print(f"{color}      DIF: {s['dif']:.4f}  DEA: {s['dea']:.4f}  MACD柱: {s['macd_bar']:.4f}  "
                  f"K: {s['k']:.2f}  D: {s['d']:.2f}{RESET}")
            print(f"{color}      周RSI: {s['w_rsi']:.2f} (5周前: {s['w_rsi_prev']:.2f}){RESET}")
            time.sleep(0.05)
        print(f"\n{GREEN}{BOLD}{'=' * 130}{RESET}")
        print(f"{GREEN}{BOLD}完成，共 {len(results)} 只{RESET}\n")


def main():
    print("=" * 110)
    print(f"{BOLD} BBI强斜率拐头+三指标共振+周线护航（斜率狙击版）【公共缓存】{RESET}")
    print("=" * 110)
    filter_tool = CustomStrategyFilter(GlobalConfig.TUSHARE_TOKEN)
    filter_tool.set_level(1)

    date_input = input("\n输入T日日期(YYYYMMDD)，回车使用今天: ").strip()
    validated_date = filter_tool.validate_trade_date(date_input)
    print(f"使用交易日: {validated_date}")
    print(f"日线缓存: {GlobalConfig.CACHE_DIR}")
    print(f"周线缓存: {GlobalConfig.WEEKLY_CACHE_DIR}")
    print(f"结果目录: {GlobalConfig.OUTPUT_DIR}")

    now = datetime.now()
    is_today = (validated_date == now.strftime('%Y%m%d'))
    trading_time = is_today and (now.weekday() < 5) and ((9 <= now.hour < 12) or (13 <= now.hour < 15))
    if trading_time:
        mode_input = input("\n1=收盘模式  2=实时盘中(默认2): ").strip()
        is_intraday = (mode_input != '1')
    else:
        is_intraday = False
        print("非交易时段，使用历史数据")

    results = filter_tool.filter_market_stocks(validated_date, is_intraday)
    filter_tool.save_results(validated_date, results, is_intraday)
    filter_tool.print_console_results(results)


if __name__ == '__main__':
    main()