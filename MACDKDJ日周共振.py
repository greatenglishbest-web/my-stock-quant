# -*- coding: utf-8 -*-
"""
选股策略：MACD(8,20,6) + KDJ(14,3,3) 日周同步首金叉/多头
【速度优化 + 公共缓存版】

核心逻辑：
1. 日线：MACD 或 KDJ 为「近3日首次转红/金叉多头」
2. 周线（日线聚合模拟）：MACD 或 KDJ 为「本周首次转红/多头」
3. 日线与周线同步满足（可交叉）
4. 预筛：当日涨幅 > 0；排除 ST、北交所
5. 当日：红涨K + 量比>1.2；近3日无三连红、无三连涨
6. 周线KDJ：聚合后 J 向上且多头（上一周 J 可向下多头）
7. 周线 BBIBOLL 中轨必须向上（红）
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
    OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'MACD_KDJ日周同步_选股结果')
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
        print("正在加载股票池（排除ST/北交所 + 上市≥7天）...")
        df = self.pro.stock_basic(list_status='L', fields='ts_code,name,list_date')
        today = datetime.now().strftime('%Y%m%d')
        filtered = []
        for _, row in df.iterrows():
            name = str(row['name'])
            code = row['ts_code']
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
                print(f"获取 {pattern} 实时行情失败: {str(e)[:50]}")
                time.sleep(3)
        if rt_dfs:
            full_df = pd.concat(rt_dfs, ignore_index=True).drop_duplicates(subset=['ts_code'])
            self.rt_data_dict = full_df.set_index('ts_code').to_dict('index')
            print(f"实时数据获取完成，共 {len(self.rt_data_dict)} 只股票")

    def get_candidate_stocks(self, trade_date, is_intraday=False):
        """候选池：当日涨幅 > 0，排除 ST / 北交所"""
        all_stocks = self.get_stock_pool()
        strong = []
        print("正在预筛：当日涨幅 > 0 ...")
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
                    if pre_close > 0:
                        pct_chg = (close - pre_close) / pre_close * 100
                        if pct_chg > 0:
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
                    mask = df['pct_chg'] > 0
                    codes = df.loc[mask, 'ts_code'].tolist()
                    valid = set(all_stocks['ts_code'])
                    codes = [c for c in codes if c in valid]
                    strong = all_stocks[all_stocks['ts_code'].isin(codes)].to_dict('records')
            except Exception as e:
                print(f"批量获取T日数据失败: {e}")
        strong_df = pd.DataFrame(strong)
        print(f"预筛完成：涨幅>0 共 {len(strong_df)} 只")
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
        """MACD + KDJ 日周同步 + 量比红K + 周J多头 + 周BBI向上"""
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

            # ========== 当日：红涨K + 量比>1.2 ==========
            is_red = close_p > open_p
            is_up = close_p > close_p.shift(1)
            red_up = is_red & is_up
            vol_ok = vol_p / (vol_p.rolling(5).mean().shift(1) + 1e-10) > 1.2

            # ========== 近3日无三连红、无三连涨 ==========
            no_3_red = is_red.rolling(3).sum() < 3
            no_3_up = is_up.rolling(3).sum() < 3
            no_triple = no_3_red & no_3_up

            # ========== 日线 MACD(8,20,6) ==========
            ema8 = close_p.ewm(span=8, adjust=False).mean()
            ema20 = close_p.ewm(span=20, adjust=False).mean()
            dif = ema8 - ema20
            dea = dif.ewm(span=6, adjust=False).mean()
            macd_bar = 2 * (dif - dea)
            macd_cross = (dif > dea) & (dif.shift(1) <= dea.shift(1))
            macd_bull = macd_bar > 0
            d_macd_first = (
                (macd_cross & (macd_cross.astype(int).rolling(3).sum() == 1)) |
                (macd_bull & (macd_bull.astype(int).rolling(3).sum() == 1))
            )

            # ========== 日线 KDJ(14,3,3) ==========
            llv = low_p.rolling(14).min()
            hhv = high_p.rolling(14).max()
            rsv = (close_p - llv) / (hhv - llv + 1e-10) * 100
            k_line = self.TDX_SMA(rsv, 3, 1)
            d_line = self.TDX_SMA(k_line, 3, 1)
            j_line = 3 * k_line - 2 * d_line
            kdj_bar = 2 * (k_line - d_line)
            kdj_cross = (k_line > d_line) & (k_line.shift(1) <= d_line.shift(1))
            kdj_bull = kdj_bar > 0
            d_kdj_first = (
                (kdj_cross & (kdj_cross.astype(int).rolling(3).sum() == 1)) |
                (kdj_bull & (kdj_bull.astype(int).rolling(3).sum() == 1))
            )

            daily_ok = d_macd_first | d_kdj_first

            # ========== 周线模拟（参数×5） ==========
            w_ema_f = close_p.ewm(span=40, adjust=False).mean()
            w_ema_s = close_p.ewm(span=100, adjust=False).mean()
            w_dif = w_ema_f - w_ema_s
            w_dea = w_dif.ewm(span=30, adjust=False).mean()
            w_macd_bar = 2 * (w_dif - w_dea)
            w_macd_cross = (w_dif > w_dea) & (w_dif.shift(5) <= w_dea.shift(5))
            w_macd_bull = w_macd_bar > 0
            w_macd_first = (
                (w_macd_bull & (~w_macd_bull.shift(5).fillna(False))) |
                (w_macd_cross)
            )

            w_k_up = k_line >= k_line.shift(5)
            w_kdj_bull = kdj_bar > 0
            w_kdj_first = (
                (w_kdj_bull & (~w_kdj_bull.shift(5).fillna(False))) |
                (w_k_up & (~w_k_up.shift(5).fillna(False)))
            )

            weekly_ok = w_macd_first | w_kdj_first

            # ========== 周线 J：聚合后向上且多头 ==========
            w_j_bull = (j_line > d_line) | (kdj_bar > 0) | (k_line > d_line)
            w_j_up = j_line >= j_line.shift(5)
            w_j_ok = w_j_up & w_j_bull

            # ========== 周线 BBIBOLL 中轨必须向上（红） ==========
            # 日线参数×5：3→15, 6→30, 12→60, 24→120
            w_bbi = (close_p.rolling(15).mean() + close_p.rolling(30).mean() +
                     close_p.rolling(60).mean() + close_p.rolling(120).mean()) / 4
            w_bbi_up = w_bbi > w_bbi.shift(5)   # 严格向上为红

            # ========== 最终 ==========
            final = (daily_ok & weekly_ok & red_up & vol_ok &
                     no_triple & w_j_ok & w_bbi_up)

            if final.iloc[-1] == True:
                t_day = df_daily.iloc[-1]
                t1_day = df_daily.iloc[-2] if len(df_daily) > 1 else None
                pct_chg = (float(t_day['close']) / float(t1_day['close']) - 1) * 100 if t1_day is not None else 0
                vol_ratio = float(vol_p.iloc[-1] / (vol_p.iloc[-6:-1].mean() + 1e-10)) if len(vol_p) >= 6 else 0

                d_list = []
                if d_macd_first.iloc[-1]:
                    d_list.append("日MACD")
                if d_kdj_first.iloc[-1]:
                    d_list.append("日KDJ")
                d_str = "+".join(d_list) if d_list else "-"

                w_list = []
                if w_macd_first.iloc[-1]:
                    w_list.append("周MACD")
                if w_kdj_first.iloc[-1]:
                    w_list.append("周KDJ")
                w_str = "+".join(w_list) if w_list else "-"

                return {
                    'ts_code': ts_code,
                    'name': name,
                    'current_price': round(float(t_day['close']), 2),
                    'pct_chg': round(pct_chg, 2),
                    'signal': 'MACD/KDJ日周同步',
                    'daily_sig': d_str,
                    'weekly_sig': w_str,
                    'dif': round(float(dif.iloc[-1]), 4),
                    'dea': round(float(dea.iloc[-1]), 4),
                    'macd_bar': round(float(macd_bar.iloc[-1]), 4),
                    'k': round(float(k_line.iloc[-1]), 2),
                    'd': round(float(d_line.iloc[-1]), 2),
                    'j': round(float(j_line.iloc[-1]), 2),
                    'kdj_bar': round(float(kdj_bar.iloc[-1]), 4),
                    'w_macd_bar': round(float(w_macd_bar.iloc[-1]), 4),
                    'w_bbi': round(float(w_bbi.iloc[-1]), 2),
                    'vol_ratio': round(vol_ratio, 2),
                }, 'selected'
            return None, 'condition_fail'
        except Exception:
            return None, 'exception'

    def filter_market_stocks(self, trade_date, is_intraday=False):
        print(f"\n开始【MACD+KDJ 日周同步首金叉】过滤，日期: {trade_date}")
        stocks = self.get_candidate_stocks(trade_date, is_intraday)
        if len(stocks) == 0:
            print("候选池为空，结束运行")
            return []

        if is_intraday and not self.rt_data_dict:
            self.fetch_rt_k_batch()

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
        path = os.path.join(GlobalConfig.OUTPUT_DIR, f"MACD_KDJ日周同步_{trade_date}{mode}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"MACD+KDJ 日周同步首金叉/多头 - {trade_date}\n")
            f.write(f"共选中 {len(results)} 只\n\n")
            for i, s in enumerate(results, 1):
                f.write(f"{i:3d}. {s['ts_code']} {s['name']} 价格:{s['current_price']:.2f} "
                        f"涨幅:{s['pct_chg']:+.2f}% 量比:{s['vol_ratio']} "
                        f"日线:{s['daily_sig']} 周线:{s['weekly_sig']} "
                        f"MACD柱:{s['macd_bar']} KDJ柱:{s['kdj_bar']} J:{s['j']} 周BBI:{s['w_bbi']}\n")
        print(f"结果已保存至: {path}")

    def print_console_results(self, results):
        print("\n" + "=" * 120)
        print(f"{BOLD}{CYAN}【最终选股结果】MACD+KDJ 日周同步（按涨幅排序）- 共 {len(results)} 只{RESET}")
        print("=" * 120)
        if not results:
            print("无符合条件股票")
            return
        for i, s in enumerate(results, 1):
            color = RED if i % 2 == 1 else YELLOW
            print(f"\n{color}{BOLD}{'─' * 110}{RESET}")
            print(f"{color}{BOLD}  {i:2d}. {s['ts_code']}  {s['name']:<12}{RESET}")
            print(f"{color}      现价: {s['current_price']:8.2f}    涨幅: {s['pct_chg']:+7.2f}%    量比: {s['vol_ratio']:.2f}{RESET}")
            print(f"{color}      日线首红/金叉: {s['daily_sig']:<20}  周线首红/多头: {s['weekly_sig']}{RESET}")
            print(f"{color}      DIF: {s['dif']:.4f}  DEA: {s['dea']:.4f}  MACD柱: {s['macd_bar']:.4f}{RESET}")
            print(f"{color}      K: {s['k']:.2f}  D: {s['d']:.2f}  J: {s['j']:.2f}  KDJ柱: {s['kdj_bar']:.4f}{RESET}")
            print(f"{color}      周MACD柱: {s['w_macd_bar']:.4f}  周BBI: {s['w_bbi']:.2f}{RESET}")
            time.sleep(0.05)
        print(f"\n{GREEN}{BOLD}{'=' * 120}{RESET}")
        print(f"{GREEN}{BOLD}选股完成，以上共 {len(results)} 只股票符合全部条件{RESET}")
        print(f"{GREEN}{BOLD}{'=' * 120}{RESET}\n")

def main():
    print("=" * 100)
    print(f"{BOLD} MACD(8,20,6) + KDJ(14,3,3) 日周同步首金叉/多头 【公共缓存加速版】{RESET}")
    print("=" * 100)
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