# -*- coding: utf-8 -*-
"""
双策略选股：AVWAP三线共振反包 (Python 量化重构加固版)
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

if os.name == 'nt':
    os.system('')
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 0x0007 | 0x0004)
    except:
        pass

class GlobalConfig:
    # ================= 替换为你的有效 Token =================
    TUSHARE_TOKEN = 'a91652800def39d213abec3af9563e4485e83940bfbbd8276f8adaa8'  
    OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'AVWAP双策略_选股结果')
    CACHE_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'A股日线公共缓存')
    LOOKBACK_DAYS = 500
    USE_PARQUET = True

class MultiLineBreakoutFilter:
    def __init__(self, token):
        self.pro = ts.pro_api(token)
        self.rt_data_dict = {}
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
                    print(f"{YELLOW}未检测到 pyarrow/fastparquet，缓存使用 CSV{RESET}")

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

    def _cache_path(self, trade_date):
        ext = 'parquet' if self.use_parquet else 'csv'
        return os.path.join(GlobalConfig.CACHE_DIR, f"{trade_date}.{ext}")

    def save_day_to_cache(self, trade_date, df):
        if df is None or df.empty: return
        path = self._cache_path(trade_date)
        try:
            if self.use_parquet: df.to_parquet(path, index=False)
            else: df.to_csv(path, index=False, encoding='utf-8-sig')
        except: pass

    def load_day_from_cache(self, trade_date):
        path = self._cache_path(trade_date)
        if not os.path.exists(path): return None
        try:
            return pd.read_parquet(path) if self.use_parquet else pd.read_csv(path)
        except: return None

    def get_cached_dates(self):
        pattern = os.path.join(GlobalConfig.CACHE_DIR, '*.parquet' if self.use_parquet else '*.csv')
        dates = set()
        for f in glob.glob(pattern):
            d = os.path.basename(f).split('.')[0]
            if len(d) == 8 and d.isdigit(): dates.add(d)
        return dates

    def get_stock_pool(self):
        print("正在加载股票池（排除ST/北交所 + 上市≥60天）...")
        df = self.pro.stock_basic(list_status='L', fields='ts_code,name,list_date')
        today = datetime.now().strftime('%Y%m%d')
        filtered = []
        for _, row in df.iterrows():
            name, code = str(row['name']), row['ts_code']
            if 'ST' in name or code.endswith('.BJ') or code.startswith('8'): 
                continue
            list_date = str(row['list_date'])
            if len(list_date) == 8:
                days = (datetime.strptime(today, '%Y%m%d') - datetime.strptime(list_date, '%Y%m%d')).days
                if days > 60: filtered.append(row.to_dict())
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
                if not df.empty: rt_dfs.append(df)
                time.sleep(1.0)
            except: pass
        if rt_dfs:
            full_df = pd.concat(rt_dfs, ignore_index=True).drop_duplicates(subset=['ts_code'])
            self.rt_data_dict = full_df.set_index('ts_code').to_dict('index')
            print(f"实时数据获取完成，共 {len(self.rt_data_dict)} 只")

    def get_candidate_stocks(self, trade_date, is_intraday=False):
        all_stocks = self.get_stock_pool()
        strong = []
        print("正在预筛：当日红盘上涨K线 ...")
        if is_intraday:
            self.fetch_rt_k_batch()
            for _, row in all_stocks.iterrows():
                ts_code = row['ts_code']
                if ts_code not in self.rt_data_dict: continue
                rt = self.rt_data_dict[ts_code]
                try:
                    pre_close = float(rt.get('pre_close', 0))
                    close = float(rt.get('close', 0))
                    open_p = float(rt.get('open', 0))
                    if pre_close > 0 and close > open_p and close > pre_close:
                        strong.append(row.to_dict())
                except: continue
        else:
            try:
                df = self.load_day_from_cache(trade_date)
                if df is None or df.empty:
                    df = self._call_with_retry(
                        self.pro.daily, trade_date=trade_date,
                        fields='ts_code,open,close,pct_chg,high,low,vol,amount'
                    )
                    if not df.empty: self.save_day_to_cache(trade_date, df)
                if not df.empty:
                    df['pct_chg'] = pd.to_numeric(df['pct_chg'], errors='coerce')
                    mask = (df['close'] > df['open']) & (df['pct_chg'] > 0)
                    codes = [c for c in df.loc[mask, 'ts_code'].tolist() if c in set(all_stocks['ts_code'])]
                    strong = all_stocks[all_stocks['ts_code'].isin(codes)].to_dict('records')
            except Exception as e:
                print(f"预筛失败: {e}")
        strong_df = pd.DataFrame(strong)
        print(f"预筛完成：共 {len(strong_df)} 只")
        return strong_df

    def batch_fetch_daily_history(self, trade_date, stock_codes):
        print(f"\n{CYAN}准备日线（公共缓存 + 增量）...{RESET}")
        trade_dates = self.get_trade_dates(trade_date, GlobalConfig.LOOKBACK_DAYS)
        print(f"日线范围: {trade_dates[0]} ~ {trade_dates[-1]}，共 {len(trade_dates)} 天")
        cached = self.get_cached_dates()
        need = [d for d in trade_dates if d not in cached]
        print(f"日线缓存已有 {len(trade_dates)-len(need)} | 需下载 {len(need)}")
        if need:
            for i, d in enumerate(tqdm(need, desc="日线增量")):
                try:
                    df = self._call_with_retry(
                        self.pro.daily, trade_date=d,
                        fields='ts_code,trade_date,open,high,low,close,vol,amount,pct_chg'
                    )
                    if not df.empty: self.save_day_to_cache(d, df)
                    time.sleep(1.2 if (i + 1) % 20 == 0 else 0.2)
                except: time.sleep(3)
        all_dfs = []
        code_set = set(stock_codes)
        for d in tqdm(trade_dates, desc="加载日线"):
            df = self.load_day_from_cache(d)
            if df is not None and not df.empty:
                df = df[df['ts_code'].isin(code_set)]
                if not df.empty: all_dfs.append(df)
        if not all_dfs: return {}
        full = pd.concat(all_dfs, ignore_index=True).sort_values(['ts_code', 'trade_date'])
        return {code: g.reset_index(drop=True) for code, g in full.groupby('ts_code')}

    def process_stock_from_df(self, stock_info, df_daily, trade_date, is_intraday=False):
        ts_code = stock_info['ts_code']
        name = stock_info.get('name', '')
        try:
            df_daily = df_daily[df_daily['trade_date'] <= trade_date].copy()
            if len(df_daily) < 70: return None, 'data_short'

            # 实时数据修复
            if is_intraday and ts_code in self.rt_data_dict:
                rt = self.rt_data_dict[ts_code]
                last_date = str(df_daily.iloc[-1]['trade_date'])
                if last_date == trade_date:
                    idx = len(df_daily) - 1
                    df_daily.at[idx, 'close'] = float(rt['close'])
                    df_daily.at[idx, 'high'] = max(float(df_daily.at[idx, 'high']), float(rt['high']))
                    df_daily.at[idx, 'low'] = min(float(df_daily.at[idx, 'low']), float(rt['low']))
                    if float(rt.get('open', 0)) > 0: df_daily.at[idx, 'open'] = float(rt['open'])
                    df_daily.at[idx, 'vol'] = float(rt['vol'])
                    if 'amount' in df_daily.columns: df_daily.at[idx, 'amount'] = float(rt.get('amount', df_daily.at[idx, 'amount']))
                else:
                    new_row = {
                        'ts_code': ts_code, 'trade_date': trade_date,
                        'open': float(rt['open']), 'high': float(rt['high']),
                        'low': float(rt['low']), 'close': float(rt['close']),
                        'vol': float(rt['vol']), 'amount': float(rt.get('amount', 0)),
                        'pct_chg': (float(rt['close']) / float(rt.get('pre_close', rt['close'])) - 1) * 100
                    }
                    df_daily = pd.concat([df_daily, pd.DataFrame([new_row])], ignore_index=True)

            # ================= 绝对安全的数据类型强转 =================
            O = pd.to_numeric(df_daily['open'], errors='coerce').ffill().bfill()
            C = pd.to_numeric(df_daily['close'], errors='coerce').ffill().bfill()
            H = pd.to_numeric(df_daily['high'], errors='coerce').ffill().bfill()
            L = pd.to_numeric(df_daily['low'], errors='coerce').ffill().bfill()
            V = pd.to_numeric(df_daily['vol'], errors='coerce').ffill().bfill()
            
            AMT = pd.to_numeric(df_daily['amount'], errors='coerce').ffill().bfill() * 1000 if 'amount' in df_daily.columns else C * V * 100
            VOL_SHARES = V * 100
            
            # ================= 1. AVWAP 计算 =================
            v_roll3 = VOL_SHARES.rolling(3).sum().replace(0, np.nan)
            v_roll22 = VOL_SHARES.rolling(22).sum().replace(0, np.nan)
            v_roll66 = VOL_SHARES.rolling(66).sum().replace(0, np.nan)
            
            AVWAP3  = (AMT.rolling(3).sum() / v_roll3).replace([np.inf, -np.inf], np.nan).bfill().fillna(C)
            AVWAP22 = (AMT.rolling(22).sum() / v_roll22).replace([np.inf, -np.inf], np.nan).bfill().fillna(C)
            AVWAP66 = (AMT.rolling(66).sum() / v_roll66).replace([np.inf, -np.inf], np.nan).bfill().fillna(C)
            
            # ================= 2. BBIBOLL / KC 中轨 =================
            BBIBOLL_MID = (C.rolling(3).mean() + C.rolling(6).mean() + C.rolling(12).mean() + C.rolling(24).mean()) / 4
            KC_MID = C.ewm(span=20, adjust=False).mean()

            # ================= 3. MACD 量柱计算 (12, 26, 9) =================
            DIF = C.ewm(span=12, adjust=False).mean() - C.ewm(span=26, adjust=False).mean()
            DEA = DIF.ewm(span=9, adjust=False).mean()
            MACD_BAR = 2 * (DIF - DEA)
            prev_macd_bar = MACD_BAR.shift(1)

            # MACD过滤逻辑
            cond_red = (prev_macd_bar > 0) & (MACD_BAR > prev_macd_bar)
            cond_green = (prev_macd_bar <= 0) & (
                (MACD_BAR > 0) | 
                ((MACD_BAR < 0) & (MACD_BAR.abs() <= prev_macd_bar.abs() * 0.7))
            )
            MACD_COND = (cond_red | cond_green).fillna(False).astype(bool)

            # ================= 4. 排除高开3%以上 =================
            NOT_HIGH_OPEN = (O < C.shift(1) * 1.03).fillna(False).astype(bool)

            # ================= 5. K线实体与分位计算 =================
            body_high = pd.concat([O, C], axis=1).max(axis=1)
            body_low = pd.concat([O, C], axis=1).min(axis=1)
            k_len = H - L
            lower_70 = L + k_len * 0.7
            lower_50 = L + k_len * 0.5

            # 方向判定 (严格向上)
            A3_UP  = (AVWAP3 > AVWAP3.shift(1)).fillna(False)
            BBI_UP = (BBIBOLL_MID > BBIBOLL_MID.shift(1)).fillna(False)
            KC_UP  = (KC_MID > KC_MID.shift(1)).fillna(False)
            A22_UP = (AVWAP22 > AVWAP22.shift(1)).fillna(False)
            A66_UP = (AVWAP66 > AVWAP66.shift(1)).fillna(False)

            # ================= 策略一逻辑 =================
            prev_down_s1 = ((C.shift(1) < O.shift(1)) | (C.shift(1) < C.shift(2))).fillna(False)
            prev_low_below_mid = ((L.shift(1) < BBIBOLL_MID.shift(1)) | (L.shift(1) < KC_MID.shift(1))).fillna(False)
            up_day = ((C > O) & (C > C.shift(1))).fillna(False)
            
            vol_ma5 = V.rolling(5).mean().shift(1).replace(0, np.nan)
            vol_ratio = (V / vol_ma5).replace([np.inf, -np.inf], np.nan).fillna(0)
            vol_cond = (vol_ratio > 1.1).fillna(False)
            
            # 组合A
            cond_A = (A3_UP & BBI_UP).astype(bool)
            max_A = pd.concat([AVWAP3, BBIBOLL_MID], axis=1).max(axis=1).fillna(0)
            min_A = pd.concat([AVWAP3, BBIBOLL_MID], axis=1).min(axis=1).fillna(0)
            inc_or_stand_A = (
                cond_A & (
                    (L >= max_A) | 
                    ((body_low <= min_A) & (max_A <= body_high)) |
                    ((L <= min_A) & (max_A <= lower_70))
                )
            ).fillna(False)

            # 组合B
            cond_B = (KC_UP & A22_UP).astype(bool)
            max_B = pd.concat([KC_MID, AVWAP22], axis=1).max(axis=1).fillna(0)
            min_B = pd.concat([KC_MID, AVWAP22], axis=1).min(axis=1).fillna(0)
            inc_or_stand_B = (
                cond_B & (
                    (L >= max_B) | 
                    ((body_low <= min_B) & (max_B <= body_high)) |
                    ((L <= min_B) & (max_B <= lower_70))
                )
            ).fillna(False)

            # 组合C (AVWAP66)
            inc_or_stand_C = (
                (L >= AVWAP66) |
                ((body_low <= AVWAP66) & (AVWAP66 <= body_high)) |
                ((L <= AVWAP66) & (AVWAP66 <= lower_70))
            ).fillna(False)

            # 必须是红色K线（避免非红K误选）
            is_red = (C > O).fillna(False)
            pattern_s1 = (is_red & (inc_or_stand_A | inc_or_stand_B | inc_or_stand_C)).fillna(False).astype(bool)
            
            shift1_s1 = pattern_s1.shift(1).fillna(False).astype(bool)
            shift2_s1 = pattern_s1.shift(2).fillna(False).astype(bool)
            first_3_s1 = pattern_s1 & (~shift1_s1) & (~shift2_s1)

            green_or_down = ((C < O) | (C < C.shift(1))).fillna(False)
            past_5_cond = (green_or_down.rolling(5).sum() >= 2).fillna(False)

            # 【新增】策略一要求：当日AVWAP3至少大于等于AVWAP22或AVWAP66中的一个
            avwap_3_cond = ((AVWAP3 >= AVWAP22) | (AVWAP3 >= AVWAP66)).fillna(False)

            # 策略一最终判定
            LOGIC_1 = prev_down_s1 & prev_low_below_mid & up_day & vol_cond & first_3_s1 & past_5_cond & MACD_COND & NOT_HIGH_OPEN & avwap_3_cond

            # ================= 策略二逻辑 =================
            prev_down_s2 = ((C.shift(1) < O.shift(1)) | (C.shift(1) < C.shift(2))).fillna(False)
            prev_low_below_a3 = (L.shift(1) < AVWAP3.shift(1)).fillna(False)
            
            bull_A = ((AVWAP3 > BBIBOLL_MID) & (BBIBOLL_MID > AVWAP22) & A3_UP & BBI_UP & A22_UP).fillna(False)
            bull_B = ((AVWAP3 > BBIBOLL_MID) & (BBIBOLL_MID > AVWAP66) & A3_UP & BBI_UP & A66_UP).fillna(False)
            bull_align = bull_A | bull_B

            # 策略二：AVWAP3 的包含逻辑
            inc_or_stand_a3 = (
                (C > O) & (
                    (L >= AVWAP3) |
                    ((body_low <= AVWAP3) & (AVWAP3 <= body_high)) |
                    ((L <= AVWAP3) & (AVWAP3 <= lower_50))
                )
            ).fillna(False)

            is_3_red = ((C > O).rolling(3).sum() == 3).fillna(False)
            is_3_up = ((C > C.shift(1)).rolling(3).sum() == 3).fillna(False)
            has_3_red_up = is_3_red | is_3_up
            no_3_red_up_in_5 = (has_3_red_up.rolling(5).sum() == 0).fillna(False)

            # 策略二最终判定
            LOGIC_2 = prev_down_s2 & prev_low_below_a3 & bull_align & inc_or_stand_a3 & no_3_red_up_in_5 & MACD_COND & NOT_HIGH_OPEN

            # ================= 最终结果 =================
            if bool(LOGIC_1.iloc[-1]) or bool(LOGIC_2.iloc[-1]):
                last_row = df_daily.iloc[-1]
                pct_chg = (last_row['close'] / df_daily.iloc[-2]['close'] - 1) * 100 if len(df_daily) > 1 else 0
                
                sig_list = []
                if bool(LOGIC_1.iloc[-1]): sig_list.append("策略一")
                if bool(LOGIC_2.iloc[-1]): sig_list.append("策略二")
                sig_type = "+".join(sig_list)
                
                return {
                    'ts_code': ts_code,
                    'name': name,
                    'close': round(last_row['close'], 2),
                    'pct_chg': round(pct_chg, 2),
                    'signal_type': sig_type,
                    'vol_ratio': round(vol_ratio.iloc[-1], 2),
                }, 'selected'
                
            return None, 'condition_fail'
            
        except Exception as e:
            print(f"\n[{ts_code}] 运行异常: {e}")
            return None, 'exception'

    def filter_market_stocks(self, trade_date, is_intraday=False):
        print(f"\n开始【AVWAP双策略共振反包】过滤，日期: {trade_date}")
        stocks = self.get_candidate_stocks(trade_date, is_intraday)
        if len(stocks) == 0:
            print("候选池为空")
            return []
        if is_intraday and not self.rt_data_dict:
            self.fetch_rt_k_batch()

        stock_codes = stocks['ts_code'].tolist()
        daily_dict = self.batch_fetch_daily_history(trade_date, stock_codes)

        selected = []
        stats = {'selected': 0, 'data_short': 0, 'condition_fail': 0, 'exception': 0, 'no_data': 0}

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
                    self.process_stock_from_df, info, ddf, trade_date, is_intraday
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
        path = os.path.join(GlobalConfig.OUTPUT_DIR, f"AVWAP双策略_{trade_date}{mode}.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"AVWAP双策略共振反包 - {trade_date}\n共选中 {len(results)} 只\n\n")
            for i, s in enumerate(results, 1):
                f.write(f"{i:3d}. {s['ts_code']} {s['name']} 价:{s['close']:.2f} "
                        f"涨:{s['pct_chg']:+.2f}% 命中:{s['signal_type']} 量比:{s['vol_ratio']}\n")
        print(f"结果已保存: {path}")

    def print_console_results(self, results):
        print("\n" + "=" * 130)
        print(f"{BOLD}{CYAN}【结果】AVWAP双策略共振反包 - 共 {len(results)} 只{RESET}")
        print("=" * 130)
        if not results:
            print("无符合条件股票")
            return
        for i, s in enumerate(results, 1):
            color = RED if i % 2 == 1 else YELLOW
            print(f"\n{color}{BOLD}{'─' * 120}{RESET}")
            print(f"{color}{BOLD}  {i:2d}. {s['ts_code']}  {s['name']:<12}{RESET}")
            print(f"{color}      现价: {s['close']:8.2f}  涨幅: {s['pct_chg']:+7.2f}%  命中: {s['signal_type']}{RESET}")
            print(f"{color}      当日量比: {s['vol_ratio']:.2f}{RESET}")
            time.sleep(0.05)
        print(f"\n{GREEN}{BOLD}{'=' * 130}{RESET}")
        print(f"{GREEN}{BOLD}完成，共 {len(results)} 只{RESET}\n")

def main():
    print("=" * 110)
    print(f"{BOLD} AVWAP双策略共振反包 选股系统 【缓存 + 并发版】{RESET}")
    print("=" * 110)
    filter_tool = MultiLineBreakoutFilter(GlobalConfig.TUSHARE_TOKEN)

    date_input = input("\n输入T日日期(YYYYMMDD)，回车使用今天: ").strip()
    validated_date = filter_tool.validate_trade_date(date_input)
    print(f"使用交易日: {validated_date}")
    print(f"日线缓存: {GlobalConfig.CACHE_DIR}")

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