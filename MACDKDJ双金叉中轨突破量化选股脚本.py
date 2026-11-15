# -*- coding: utf-8 -*-
"""
双动能多头共振 + 探底抬升 + 通道反包 (四象组合终极确认版)
==========================================================
前日探底洗盘 + 近3日低点抬高 + 当日放量红K + MACD/KDJ双多头
+ 双线突破 (BBI 与 肯特纳通道 中轨, 包含/站上四种组合)
+ 防高开 + 饱满实体

[更新] 支持盘中 15 分钟自动循环扫描。
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
import glob

warnings.filterwarnings('ignore')

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


def norm_date(d):
    if d is None or (isinstance(d, float) and np.isnan(d)):
        return ''
    s = str(d).strip().replace('-', '').replace('/', '').replace('.0', '').replace(' ', '')
    return s[:8] if len(s) >= 8 else s


class GlobalConfig:
    TUSHARE_TOKEN = 'a91652800def39d213abec3af9563e4485e83940bfbbd8276f8adaa8'
    OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', '双动能通道共振_选股结果')
    CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股日线公共缓存')
    LOOKBACK_DAYS = 500
    USE_PARQUET = True


class DualMomentumBreakFilter:
    """双动能多头共振 + 探底抬升 + 通道反包 (四象组合) 选股器"""

    def __init__(self, token):
        self.pro = ts.pro_api(token)
        self.rt_data_dict = {}
        os.makedirs(GlobalConfig.CACHE_DIR, exist_ok=True)
        os.makedirs(GlobalConfig.OUTPUT_DIR, exist_ok=True)
        self.use_parquet = GlobalConfig.USE_PARQUET
        
        # 内存缓存，防止 15 分钟定时循环时频繁请求基础数据导致 Tushare 限流
        self._stock_pool_cache = None
        self._trade_dates_cache = {}

    # ---------------- 基础工具 ----------------
    def _call_with_retry(self, api_func, **kwargs):
        retries = 0
        while retries < 5:
            try:
                return api_func(**kwargs)
            except Exception as e:
                if any(kw in str(e).lower() for kw in
                       ["限流", "频率超限", "max retries", "connection", "timeout", "500", "502", "503", "429"]):
                    retries += 1
                    time.sleep(2 + retries)
                else:
                    raise
        raise Exception(f"API调用失败: {kwargs}")

    def validate_trade_date(self, input_date=''):
        if not input_date:
            input_date = datetime.now().strftime('%Y%m%d')
        input_date = norm_date(input_date)
        try:
            cal = self.pro.trade_cal(start_date=input_date, end_date=input_date)
            if not cal.empty and int(cal['is_open'].iloc[0]) == 1:
                return input_date
        except:
            pass
        date_obj = datetime.strptime(input_date, '%Y%m%d')
        for i in range(1, 40):
            prev = (date_obj - timedelta(days=i)).strftime('%Y%m%d')
            try:
                cal = self.pro.trade_cal(start_date=prev, end_date=prev)
                if not cal.empty and int(cal['is_open'].iloc[0]) == 1:
                    return prev
            except:
                continue
        return datetime.now().strftime('%Y%m%d')

    def get_trade_dates(self, end_date, n_days=500):
        # 增加内存缓存
        cache_key = f"{end_date}_{n_days}"
        if cache_key in self._trade_dates_cache:
            return self._trade_dates_cache[cache_key]

        end_date = norm_date(end_date)
        start_approx = (datetime.strptime(end_date, '%Y%m%d')
                        - timedelta(days=int(n_days * 1.8))).strftime('%Y%m%d')
        cal = self._call_with_retry(self.pro.trade_cal, start_date=start_approx,
                                    end_date=end_date, is_open='1')
        dates = sorted([norm_date(d) for d in cal['cal_date'].tolist()])
        res = dates[-n_days:] if len(dates) >= n_days else dates
        self._trade_dates_cache[cache_key] = res
        return res

    # ---------------- 缓存 ----------------
    def _cache_path(self, trade_date):
        trade_date = norm_date(trade_date)
        ext = 'parquet' if self.use_parquet else 'csv'
        return os.path.join(GlobalConfig.CACHE_DIR, f"{trade_date}.{ext}")

    def save_day_to_cache(self, trade_date, df):
        if df is None or df.empty:
            return
        trade_date = norm_date(trade_date)
        path = self._cache_path(trade_date)
        try:
            df = df.copy()
            df['trade_date'] = trade_date
            if self.use_parquet:
                df.to_parquet(path, index=False)
            else:
                df.to_csv(path, index=False, encoding='utf-8-sig')
        except:
            pass

    def load_day_from_cache(self, trade_date):
        trade_date = norm_date(trade_date)
        path = self._cache_path(trade_date)
        if not os.path.exists(path):
            return None
        try:
            df = pd.read_parquet(path) if self.use_parquet else pd.read_csv(path)
            if df is None or df.empty:
                return None
            df['trade_date'] = trade_date
            for col in ['open', 'close', 'pct_chg', 'high', 'low', 'vol', 'amount']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            return df
        except:
            return None

    def get_cached_dates(self):
        pattern = os.path.join(GlobalConfig.CACHE_DIR, '*.parquet' if self.use_parquet else '*.csv')
        dates = set()
        for f in glob.glob(pattern):
            d = norm_date(os.path.basename(f).split('.')[0])
            if len(d) == 8 and d.isdigit():
                dates.add(d)
        return dates

    # ---------------- 股票池 ----------------
    def get_stock_pool(self):
        # 增加内存缓存，循环扫描时直接复用，不再重新请求
        if self._stock_pool_cache is not None:
            return self._stock_pool_cache

        print("正在加载股票池（排除ST/北交所 + 上市≥60天）...")
        df = self.pro.stock_basic(list_status='L', fields='ts_code,name,list_date')
        today = datetime.now().strftime('%Y%m%d')
        filtered = []
        for _, row in df.iterrows():
            name, code = str(row['name']), row['ts_code']
            if name.startswith('S') or 'ST' in name or name.startswith('*'):
                continue
            if code.endswith('.BJ') or code.startswith('8') or code.startswith('4') or code.startswith('92'):
                continue
            list_date = norm_date(row['list_date'])
            if len(list_date) == 8:
                days = (datetime.strptime(today, '%Y%m%d') - datetime.strptime(list_date, '%Y%m%d')).days
                if days > 60:
                    filtered.append(row.to_dict())
        
        self._stock_pool_cache = pd.DataFrame(filtered)
        print(f"股票池加载完成，共 {len(self._stock_pool_cache)} 只")
        return self._stock_pool_cache

    # ---------------- 实时行情 ----------------
    def fetch_rt_k_batch(self):
        self.rt_data_dict = {} # 每次刷新前清空旧字典，防止脏数据
        patterns = ['60*.SH', '688*.SH', '00*.SZ', '002*.SZ', '003*.SZ', '300*.SZ', '301*.SZ']
        rt_dfs = []
        print("获取实时行情（盘中模式）...")
        for pattern in patterns:
            try:
                df = self._call_with_retry(self.pro.rt_k, ts_code=pattern)
                if not df.empty:
                    rt_dfs.append(df)
                time.sleep(0.4)
            except:
                pass
        if rt_dfs:
            full_df = pd.concat(rt_dfs, ignore_index=True).drop_duplicates(subset=['ts_code'])
            self.rt_data_dict = full_df.set_index('ts_code').to_dict('index')
            print(f"实时数据获取完成，共 {len(self.rt_data_dict)} 只")

    # ---------------- 预筛（当日红盘上涨K线） ----------------
    def get_candidate_stocks(self, trade_date, is_intraday=False):
        trade_date = norm_date(trade_date)
        all_stocks = self.get_stock_pool()
        strong = []
        print("正在预筛：当日红盘上涨K线 (T_RED: C>O 且 C>REF(C,1))...")
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
                    open_p = float(rt.get('open', 0))
                    if pre_close > 0 and close > open_p and close > pre_close:
                        strong.append(row.to_dict())
                except:
                    continue
        else:
            try:
                df = self.load_day_from_cache(trade_date)
                if df is None or df.empty:
                    df = self._call_with_retry(
                        self.pro.daily, trade_date=trade_date,
                        fields='ts_code,trade_date,open,close,pct_chg,high,low,vol,amount')
                    if not df.empty:
                        self.save_day_to_cache(trade_date, df)
                if not df.empty:
                    mask = (df['close'] > df['open']) & (df['pct_chg'] > 0)
                    codes = set(df.loc[mask, 'ts_code'].tolist())
                    strong = all_stocks[all_stocks['ts_code'].isin(codes)].to_dict('records')
                else:
                    print(f"{YELLOW}警告：未获取到 {trade_date} 的任何交易数据！{RESET}")
            except Exception as e:
                print(f"预筛失败: {e}")
        strong_df = pd.DataFrame(strong)
        print(f"预筛完成：共 {len(strong_df)} 只")
        return strong_df

    # ---------------- 历史日线加载 ----------------
    def batch_fetch_daily_history(self, trade_date, stock_codes):
        trade_date = norm_date(trade_date)
        print(f"\n{CYAN}准备日线（读取公共缓存）...{RESET}")
        trade_dates = self.get_trade_dates(trade_date, GlobalConfig.LOOKBACK_DAYS)
        if trade_date not in trade_dates:
            trade_dates.append(trade_date)
            trade_dates = sorted(set(trade_dates))

        cached = self.get_cached_dates()
        need = [d for d in trade_dates if d not in cached]
        if need:
            print(f"发现 {len(need)} 天缺失数据，极速补齐...")
            for d in need:
                try:
                    df = self._call_with_retry(
                        self.pro.daily, trade_date=d,
                        fields='ts_code,trade_date,open,high,low,close,vol,amount,pct_chg')
                    if not df.empty:
                        self.save_day_to_cache(d, df)
                    time.sleep(0.12)
                except:
                    pass

        all_dfs = []
        code_set = set(stock_codes)
        for d in tqdm(trade_dates, desc="加载日线"):
            df = self.load_day_from_cache(d)
            if df is not None and not df.empty:
                df = df[df['ts_code'].isin(code_set)]
                if not df.empty:
                    all_dfs.append(df)

        if not all_dfs:
            return {}
        full = pd.concat(all_dfs, ignore_index=True)
        full['trade_date'] = full['trade_date'].map(norm_date)
        full = full.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
        return {code: g.reset_index(drop=True) for code, g in full.groupby('ts_code')}

    # ---------------- TDX 的 SMA(X,N,M) ----------------
    def TDX_SMA(self, series, n, m):
        arr = series.values
        res = np.zeros(len(arr))
        for i in range(len(arr)):
            val = arr[i]
            if pd.isna(val) or np.isinf(val):
                res[i] = np.nan
            elif i == 0 or pd.isna(res[i - 1]):
                res[i] = val
            else:
                res[i] = (m * val + (n - m) * res[i - 1]) / n
        return pd.Series(res, index=series.index)

    # ---------------- 单只股票的核心逻辑 ----------------
    def process_stock_from_df(self, stock_info, df_daily, trade_date, is_intraday=False):
        ts_code = stock_info['ts_code']
        name = stock_info.get('name', '')
        trade_date = norm_date(trade_date)
        try:
            df_daily = df_daily.copy()
            df_daily['trade_date'] = df_daily['trade_date'].map(norm_date)
            df_daily = df_daily[df_daily['trade_date'] <= trade_date]
            df_daily = df_daily.sort_values('trade_date').reset_index(drop=True)

            if len(df_daily) < 30:
                return None, 'data_short'

            # ============ 实时行情拼接（单位：手 / 千元）============
            if is_intraday and ts_code in self.rt_data_dict:
                rt = self.rt_data_dict[ts_code]
                last_date = str(df_daily.iloc[-1]['trade_date'])
                try:
                    rt_open = float(rt.get('open', 0) or 0)
                    rt_high = float(rt.get('high', 0) or 0)
                    rt_low = float(rt.get('low', 0) or 0)
                    rt_close = float(rt.get('close', 0) or 0)
                    rt_vol = float(rt.get('vol', 0) or 0)
                    rt_amt = float(rt.get('amount', 0) or 0)
                    rt_pre = float(rt.get('pre_close', rt_close) or rt_close)
                except Exception:
                    rt_open = rt_high = rt_low = rt_close = rt_vol = rt_amt = 0.0
                    rt_pre = 0.0

                if rt_close > 0 and rt_open > 0:
                    new_row = {
                        'ts_code': ts_code, 'trade_date': trade_date,
                        'open': rt_open, 'high': rt_high, 'low': rt_low, 'close': rt_close,
                        'vol': rt_vol / 100.0,          # 股 -> 手
                        'amount': rt_amt / 1000.0,      # 元 -> 千元
                        'pct_chg': (rt_close / rt_pre - 1) * 100 if rt_pre > 0 else 0.0
                    }
                    if last_date == trade_date:
                        idx = len(df_daily) - 1
                        for k, v in new_row.items():
                            df_daily.at[idx, k] = v
                    else:
                        df_daily = pd.concat([df_daily, pd.DataFrame([new_row])], ignore_index=True)
            # ======================================================

            target_rows = df_daily.index[df_daily['trade_date'] == trade_date]
            if len(target_rows) == 0:
                return None, 'date_mismatch'
            target_idx = target_rows[-1]

            raw_open = df_daily.at[target_idx, 'open']
            if pd.isna(raw_open) or float(raw_open) <= 0:
                return None, 'suspended'

            O = pd.to_numeric(df_daily['open'], errors='coerce').ffill().bfill()
            C = pd.to_numeric(df_daily['close'], errors='coerce').ffill().bfill()
            H = pd.to_numeric(df_daily['high'], errors='coerce').ffill().bfill()
            L = pd.to_numeric(df_daily['low'], errors='coerce').ffill().bfill()
            V = pd.to_numeric(df_daily['vol'], errors='coerce').ffill().bfill()

            # ============ 1. 核心通道 ============
            BBI = (C.rolling(3).mean() + C.rolling(6).mean()
                   + C.rolling(12).mean() + C.rolling(24).mean()) / 4.0
            KCM = C.ewm(span=20, adjust=False).mean()

            # ============ 2. 前日探底洗盘 ============
            PREV_DN = (C.shift(1) < O.shift(1)) | (C.shift(1) < C.shift(2))
            PREV_BELOW = L.shift(1) < BBI.shift(1)
            PREV_COND = (PREV_DN & PREV_BELOW).fillna(False)

            # ============ 3. 近3日低点抬高 ============
            LL_3 = L.rolling(3).min()
            LL_10 = L.rolling(10).min()
            LOW_UP = (LL_3 > LL_10).fillna(False)

            # ============ 4. 当日量价 ============
            T_RED = ((C > O) & (C > C.shift(1))).fillna(False)
            V_MA5_PREV = V.rolling(5).mean().shift(1).replace(0, np.nan)
            VOL_OK = (V / V_MA5_PREV > 1.1).fillna(False)

            # ============ 5. MACD(8,20,6) ============
            DIF8 = C.ewm(span=8, adjust=False).mean() - C.ewm(span=20, adjust=False).mean()
            DEA6 = DIF8.ewm(span=6, adjust=False).mean()
            MACD_UP = (DIF8 > DEA6).fillna(False)

            # ============ 6. KDJ(9,3,3) 多头 ============
            LLV9 = L.rolling(9).min()
            HHV9 = H.rolling(9).max()
            RSV9 = (C - LLV9) / (HHV9 - LLV9 + 0.0001) * 100
            K_VAL = self.TDX_SMA(RSV9, 3, 1)
            D_VAL = self.TDX_SMA(K_VAL, 3, 1)
            KDJ_UP = (K_VAL > D_VAL).fillna(False)

            MOM_COND = MACD_UP & KDJ_UP

            # ============ 7. 双线突破组合（四选一）============
            BBI_INC = (C >= BBI) & (O <= BBI)   # 实体包含
            BBI_STD = (C >= BBI) & (O > BBI)    # 实体完全站上
            KCM_INC = (C >= KCM) & (O <= KCM)
            KCM_STD = (C >= KCM) & (O > KCM)
            INC_COND = ((BBI_INC & KCM_INC)
                        | (BBI_STD & KCM_STD)
                        | (BBI_INC & KCM_STD)
                        | (BBI_STD & KCM_INC)).fillna(False)

            # ============ 8. 附加过滤 ============
            UP_1PCT = (C > C.shift(1) * 1.01).fillna(False)

            OPEN_count = pd.Series(0, index=O.index, dtype=int)
            for i in range(1, 6):
                OPEN_count += (O > O.shift(i)).fillna(False).astype(int)
            OPEN_COND = OPEN_count >= 2

            BODY_HIGH = pd.Series(np.maximum(O.values, C.values), index=O.index)
            BH_count = pd.Series(0, index=O.index, dtype=int)
            for i in range(1, 6):
                ref_bh = pd.Series(np.maximum(O.shift(i).values, C.shift(i).values), index=O.index)
                BH_count += (BODY_HIGH > ref_bh).fillna(False).astype(int)
            BH_COND = BH_count >= 2

            RED_KCM = ((C > O) & (C >= KCM)).fillna(False)
            MAX_ONE_KCM = RED_KCM.rolling(5).sum() <= 1

            GAP_UP = (O > C.shift(1) * 1.03).fillna(False)
            NO_GAP_5 = GAP_UP.rolling(5).sum() == 0

            K_LEN = H - L + 0.0001
            BODY_LEN = C - O
            UP_SHADOW = H - C
            COND_BODY = (BODY_LEN > K_LEN * 0.60).fillna(False)
            COND_SHADOW = (UP_SHADOW <= K_LEN * 0.30).fillna(False)

            # ============ 9. 最终信号 ============
            FINAL_SIGNAL = (PREV_COND & LOW_UP & T_RED & VOL_OK
                            & MOM_COND & INC_COND
                            & UP_1PCT & OPEN_COND & BH_COND
                            & MAX_ONE_KCM & NO_GAP_5
                            & COND_BODY & COND_SHADOW)

            if bool(FINAL_SIGNAL.iloc[target_idx]):
                last_row = df_daily.iloc[target_idx]
                pct_chg = 0.0
                if 'pct_chg' in df_daily.columns and not pd.isna(last_row['pct_chg']):
                    pct_chg = float(last_row['pct_chg'])
                return {
                    'ts_code': ts_code,
                    'name': name,
                    'close': round(float(last_row['close']), 2),
                    'pct_chg': round(pct_chg, 2)
                }, 'selected'

            return None, 'condition_fail'

        except Exception:
            return None, 'exception'

    # ---------------- 主流程 ----------------
    def filter_market_stocks(self, trade_date, is_intraday=False):
        trade_date = norm_date(trade_date)
        stocks = self.get_candidate_stocks(trade_date, is_intraday)
        if len(stocks) == 0:
            return []
        if is_intraday and not self.rt_data_dict:
            self.fetch_rt_k_batch()

        stock_codes = stocks['ts_code'].tolist()
        daily_dict = self.batch_fetch_daily_history(trade_date, stock_codes)

        selected = []
        stats = {'selected': 0, 'data_short': 0, 'date_mismatch': 0,
                 'condition_fail': 0, 'exception': 0, 'no_data': 0, 'suspended': 0}

        print(f"\n{CYAN}本地计算...{RESET}")
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {}
            for _, row in stocks.iterrows():
                info = row.to_dict()
                code = info['ts_code']
                ddf = daily_dict.get(code)
                if ddf is None or ddf.empty:
                    stats['no_data'] += 1
                    continue
                futures[executor.submit(
                    self.process_stock_from_df, info, ddf, trade_date, is_intraday)] = code

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
            return
        mode = "_实时" if is_intraday else ""
        path = os.path.join(GlobalConfig.OUTPUT_DIR, f"双动能通道_{trade_date}{mode}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"双动能多头共振 + 探底抬升 + 通道反包 - {trade_date}\n")
            f.write(f"共选中 {len(results)} 只\n\n")
            for i, s in enumerate(results, 1):
                f.write(f"{i:3d}. {s['ts_code']} {s['name']} 价:{s['close']:.2f} 涨:{s['pct_chg']:+.2f}%\n")
        print(f"结果已保存: {path}")


def main():
    print("=" * 110)
    print(f"{BOLD} 双动能多头共振 + 探底抬升 + 通道反包 选股系统 {RESET}")
    print("=" * 110)
    filter_tool = DualMomentumBreakFilter(GlobalConfig.TUSHARE_TOKEN)

    date_input = input("\n输入T日日期(YYYYMMDD)，回车使用今天: ").strip()
    validated_date = filter_tool.validate_trade_date(date_input)
    print(f"使用交易日: {validated_date}")

    now = datetime.now()
    is_today = (validated_date == now.strftime('%Y%m%d'))
    trading_time = is_today and (now.weekday() < 5) and ((9 <= now.hour < 12) or (13 <= now.hour < 15))
    
    is_intraday = False
    if trading_time:
        mode_input = input("\n1=收盘模式(单次)  2=实时盘中(每15分钟自动扫描) [默认2]: ").strip()
        is_intraday = (mode_input != '1')
    else:
        is_intraday = False
        print("非交易时段，使用历史数据 (单次运行)")

    def run_scan():
        results = filter_tool.filter_market_stocks(validated_date, is_intraday)
        filter_tool.save_results(validated_date, results, is_intraday)

        if results:
            print("\n" + "=" * 120)
            print(f"{BOLD}{CYAN}【结果】双动能通道共振 - {validated_date} - 共 {len(results)} 只{RESET}")
            for i, s in enumerate(results, 1):
                color = RED if i % 2 == 1 else YELLOW
                print(f"\n{color}{BOLD}  {i:2d}. {s['ts_code']} {s['name']:<8} "
                      f"价:{s['close']:8.2f} 涨:{s['pct_chg']:+6.2f}%{RESET}")
        else:
            print(f"\n{YELLOW}本次未选出股票，请检查条件或换日期重试。{RESET}")

    if is_intraday:
        print(f"\n{BOLD}{GREEN}>>> 已开启 15分钟自动扫描任务。请保持窗口开启，按 Ctrl+C 可强制中止。{RESET}")
        try:
            while True:
                cur_now = datetime.now()
                # 检查是否还在交易时段
                if not (cur_now.weekday() < 5 and ((9 <= cur_now.hour < 12) or (13 <= cur_now.hour < 15))):
                    print(f"\n{YELLOW}当前时间已超过交易时段，自动扫描任务优雅结束。{RESET}")
                    break
                    
                print(f"\n{BOLD}{CYAN}========== 开始自动扫描 [{cur_now.strftime('%Y-%m-%d %H:%M:%S')}] =========={RESET}")
                run_scan()
                
                print(f"\n{CYAN}>>> 本轮扫描结束，{BOLD}程序将休眠 15 分钟{RESET}{CYAN}，等待下一次扫描...{RESET}")
                time.sleep(15 * 60)  # 900秒 = 15分钟
                
        except KeyboardInterrupt:
            print(f"\n\n{YELLOW}检测到 Ctrl+C，已手动终止自动扫描任务。{RESET}")
    else:
        run_scan()


if __name__ == '__main__':
    main()