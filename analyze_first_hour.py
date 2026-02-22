import pandas as pd
import numpy as np
from datetime import time

# Load the parquet file
print("Loading data...")
df = pd.read_parquet(r'd:\Folders\Work\trading-bot\data\NSE_NIFTY50_15min.parquet')

# Convert datetime column if needed
if 'datetime' in df.columns:
    df['datetime'] = pd.to_datetime(df['datetime'])
elif 'date' in df.columns:
    df['datetime'] = pd.to_datetime(df['date'])
else:
    # Check for index
    if isinstance(df.index, pd.DatetimeIndex):
        df['datetime'] = df.index
    else:
        raise ValueError("No datetime column found")

# Extract time and date components
df['time'] = df['datetime'].dt.time
df['date'] = df['datetime'].dt.date
df['day_of_week'] = df['datetime'].dt.day_name()

# Calculate candle properties
df['candle_direction'] = np.where(df['close'] > df['open'], 'bullish', 'bearish')
df['candle_size'] = abs(df['close'] - df['open'])
df['candle_range'] = df['high'] - df['low']

print(f"Total records: {len(df)}")
print(f"Date range: {df['datetime'].min()} to {df['datetime'].max()}")
print(f"\nColumns: {df.columns.tolist()}")
print(f"\nFirst few rows:")
print(df.head())

# Filter for first hour candles (9:15 AM to 10:00 AM)
first_hour_candles = df[
    (df['time'] >= time(9, 15)) & (df['time'] <= time(10, 0))
].copy()

print(f"\n{'='*80}")
print("FIRST HOUR ANALYSIS (9:15 AM - 10:00 AM)")
print(f"{'='*80}")

# Get unique dates to analyze
dates = sorted(df['date'].unique())
print(f"\nAnalyzing {len(dates)} trading days")

# ============================================================================
# 1. FIRST CANDLE (9:15) ANALYSIS
# ============================================================================
print(f"\n{'='*80}")
print("1. FIRST CANDLE (9:15 AM) ANALYSIS")
print(f"{'='*80}")

first_candles = df[df['time'] == time(9, 15)].copy()
print(f"\nTotal 9:15 candles: {len(first_candles)}")

bullish_count = (first_candles['candle_direction'] == 'bullish').sum()
bearish_count = (first_candles['candle_direction'] == 'bearish').sum()
total = len(first_candles)

print(f"\nDirection Distribution:")
print(f"  Bullish: {bullish_count} ({bullish_count/total*100:.2f}%)")
print(f"  Bearish: {bearish_count} ({bearish_count/total*100:.2f}%)")

avg_size_all = first_candles['candle_size'].mean()
avg_size_bullish = first_candles[first_candles['candle_direction'] == 'bullish']['candle_size'].mean()
avg_size_bearish = first_candles[first_candles['candle_direction'] == 'bearish']['candle_size'].mean()

print(f"\nAverage Candle Size (Close - Open):")
print(f"  Overall: {avg_size_all:.2f} points")
print(f"  Bullish: {avg_size_bullish:.2f} points")
print(f"  Bearish: {avg_size_bearish:.2f} points")

avg_range = first_candles['candle_range'].mean()
print(f"\nAverage Candle Range (High - Low): {avg_range:.2f} points")

# ============================================================================
# 2. GAP OPEN ANALYSIS
# ============================================================================
print(f"\n{'='*80}")
print("2. GAP OPEN ANALYSIS")
print(f"{'='*80}")

# Get previous day close for each 9:15 candle
gap_analysis = []
for idx, row in first_candles.iterrows():
    current_date = row['date']
    current_open = row['open']

    # Find previous trading day's last candle
    prev_day_data = df[df['date'] < current_date]
    if len(prev_day_data) > 0:
        prev_close = prev_day_data.iloc[-1]['close']
        gap = current_open - prev_close
        gap_pct = (gap / prev_close) * 100
        gap_type = 'gap_up' if gap > 0 else 'gap_down' if gap < 0 else 'flat'

        gap_analysis.append({
            'date': current_date,
            'prev_close': prev_close,
            'open': current_open,
            'gap': gap,
            'gap_pct': gap_pct,
            'gap_type': gap_type
        })

gap_df = pd.DataFrame(gap_analysis)

if len(gap_df) > 0:
    gap_up_count = (gap_df['gap_type'] == 'gap_up').sum()
    gap_down_count = (gap_df['gap_type'] == 'gap_down').sum()
    flat_count = (gap_df['gap_type'] == 'flat').sum()

    print(f"\nGap Distribution:")
    print(f"  Gap Up: {gap_up_count} ({gap_up_count/len(gap_df)*100:.2f}%)")
    print(f"  Gap Down: {gap_down_count} ({gap_down_count/len(gap_df)*100:.2f}%)")
    print(f"  Flat: {flat_count} ({flat_count/len(gap_df)*100:.2f}%)")

    print(f"\nAverage Gap Size:")
    print(f"  Overall: {gap_df['gap'].mean():.2f} points ({gap_df['gap_pct'].mean():.3f}%)")
    print(f"  Gap Up: {gap_df[gap_df['gap_type']=='gap_up']['gap'].mean():.2f} points ({gap_df[gap_df['gap_type']=='gap_up']['gap_pct'].mean():.3f}%)")
    print(f"  Gap Down: {gap_df[gap_df['gap_type']=='gap_down']['gap'].mean():.2f} points ({gap_df[gap_df['gap_type']=='gap_down']['gap_pct'].mean():.3f}%)")

# ============================================================================
# 3. FIRST HOUR RANGE VS REST OF DAY
# ============================================================================
print(f"\n{'='*80}")
print("3. FIRST HOUR RANGE VS REST OF DAY")
print(f"{'='*80}")

daily_stats = []
for date in dates:
    day_data = df[df['date'] == date]
    first_hour_data = day_data[(day_data['time'] >= time(9, 15)) & (day_data['time'] <= time(10, 0))]
    rest_of_day_data = day_data[day_data['time'] > time(10, 0)]

    if len(first_hour_data) > 0 and len(rest_of_day_data) > 0:
        first_hour_high = first_hour_data['high'].max()
        first_hour_low = first_hour_data['low'].min()
        first_hour_range = first_hour_high - first_hour_low

        day_high = day_data['high'].max()
        day_low = day_data['low'].min()
        day_range = day_high - day_low

        rest_high = rest_of_day_data['high'].max()
        rest_low = rest_of_day_data['low'].min()
        rest_range = rest_high - rest_low

        daily_stats.append({
            'date': date,
            'first_hour_range': first_hour_range,
            'rest_of_day_range': rest_range,
            'day_range': day_range,
            'first_hour_pct': (first_hour_range / day_range * 100) if day_range > 0 else 0
        })

daily_stats_df = pd.DataFrame(daily_stats)

if len(daily_stats_df) > 0:
    print(f"\nAverage Range:")
    print(f"  First Hour (9:15-10:00): {daily_stats_df['first_hour_range'].mean():.2f} points")
    print(f"  Rest of Day: {daily_stats_df['rest_of_day_range'].mean():.2f} points")
    print(f"  Full Day: {daily_stats_df['day_range'].mean():.2f} points")
    print(f"\nFirst Hour as % of Day Range: {daily_stats_df['first_hour_pct'].mean():.2f}%")

# ============================================================================
# 4. MEAN REVERSION ANALYSIS
# ============================================================================
print(f"\n{'='*80}")
print("4. MEAN REVERSION ANALYSIS")
print(f"{'='*80}")

mean_reversion = []
for date in dates:
    day_data = df[df['date'] == date]
    first_candle = day_data[day_data['time'] == time(9, 15)]
    candle_1000 = day_data[day_data['time'] == time(10, 0)]

    if len(first_candle) > 0 and len(candle_1000) > 0:
        first_open = first_candle.iloc[0]['open']
        first_close = first_candle.iloc[0]['close']
        first_direction = first_candle.iloc[0]['candle_direction']

        close_1000 = candle_1000.iloc[0]['close']

        # Check if reversed by 10:00
        reversed = False
        if first_direction == 'bullish' and close_1000 < first_open:
            reversed = True
        elif first_direction == 'bearish' and close_1000 > first_open:
            reversed = True

        mean_reversion.append({
            'date': date,
            'first_direction': first_direction,
            'reversed': reversed
        })

mr_df = pd.DataFrame(mean_reversion)

if len(mr_df) > 0:
    bullish_first = mr_df[mr_df['first_direction'] == 'bullish']
    bearish_first = mr_df[mr_df['first_direction'] == 'bearish']

    print(f"\nWhen 9:15 candle is BULLISH:")
    print(f"  Total days: {len(bullish_first)}")
    print(f"  Reversed by 10:00: {bullish_first['reversed'].sum()} ({bullish_first['reversed'].sum()/len(bullish_first)*100:.2f}%)")
    print(f"  Continued: {(~bullish_first['reversed']).sum()} ({(~bullish_first['reversed']).sum()/len(bullish_first)*100:.2f}%)")

    print(f"\nWhen 9:15 candle is BEARISH:")
    print(f"  Total days: {len(bearish_first)}")
    print(f"  Reversed by 10:00: {bearish_first['reversed'].sum()} ({bearish_first['reversed'].sum()/len(bearish_first)*100:.2f}%)")
    print(f"  Continued: {(~bearish_first['reversed']).sum()} ({(~bearish_first['reversed']).sum()/len(bearish_first)*100:.2f}%)")

# ============================================================================
# 5. OPENING DRIVE ANALYSIS (9:15 to 9:30)
# ============================================================================
print(f"\n{'='*80}")
print("5. OPENING DRIVE ANALYSIS (9:15 to 9:30)")
print(f"{'='*80}")

opening_drive = []
for date in dates:
    day_data = df[df['date'] == date]
    candle_915 = day_data[day_data['time'] == time(9, 15)]
    candle_930 = day_data[day_data['time'] == time(9, 30)]
    candle_1000 = day_data[day_data['time'] == time(10, 0)]

    if len(candle_915) > 0 and len(candle_930) > 0 and len(candle_1000) > 0:
        open_915 = candle_915.iloc[0]['open']
        close_930 = candle_930.iloc[0]['close']
        close_1000 = candle_1000.iloc[0]['close']

        move_930 = close_930 - open_915
        move_1000 = close_1000 - close_930

        # Define "strong move" as > 0.2% (adjust threshold as needed)
        threshold = open_915 * 0.002

        if abs(move_930) > threshold:
            drive_direction = 'up' if move_930 > 0 else 'down'
            continued = (move_930 > 0 and move_1000 > 0) or (move_930 < 0 and move_1000 < 0)

            opening_drive.append({
                'date': date,
                'drive_direction': drive_direction,
                'move_930': move_930,
                'move_1000': move_1000,
                'continued': continued
            })

od_df = pd.DataFrame(opening_drive)

if len(od_df) > 0:
    up_drive = od_df[od_df['drive_direction'] == 'up']
    down_drive = od_df[od_df['drive_direction'] == 'down']

    print(f"\nStrong moves by 9:30: {len(od_df)} days")

    print(f"\nUpward drive by 9:30:")
    print(f"  Total: {len(up_drive)}")
    print(f"  Continued by 10:00: {up_drive['continued'].sum()} ({up_drive['continued'].sum()/len(up_drive)*100:.2f}%)")
    print(f"  Reversed by 10:00: {(~up_drive['continued']).sum()} ({(~up_drive['continued']).sum()/len(up_drive)*100:.2f}%)")

    print(f"\nDownward drive by 9:30:")
    print(f"  Total: {len(down_drive)}")
    print(f"  Continued by 10:00: {down_drive['continued'].sum()} ({down_drive['continued'].sum()/len(down_drive)*100:.2f}%)")
    print(f"  Reversed by 10:00: {(~down_drive['continued']).sum()} ({(~down_drive['continued']).sum()/len(down_drive)*100:.2f}%)")

# ============================================================================
# 6. DAY-OF-WEEK PATTERNS
# ============================================================================
print(f"\n{'='*80}")
print("6. DAY-OF-WEEK PATTERNS FOR FIRST HOUR")
print(f"{'='*80}")

dow_analysis = []
for date in dates:
    day_data = df[df['date'] == date]
    first_hour_data = day_data[(day_data['time'] >= time(9, 15)) & (day_data['time'] <= time(10, 0))]

    if len(first_hour_data) > 0:
        dow = first_hour_data.iloc[0]['day_of_week']
        first_hour_high = first_hour_data['high'].max()
        first_hour_low = first_hour_data['low'].min()
        first_hour_range = first_hour_high - first_hour_low
        first_open = first_hour_data.iloc[0]['open']
        last_close = first_hour_data.iloc[-1]['close']
        net_change = last_close - first_open

        dow_analysis.append({
            'day_of_week': dow,
            'first_hour_range': first_hour_range,
            'net_change': net_change
        })

dow_df = pd.DataFrame(dow_analysis)

if len(dow_df) > 0:
    dow_stats = dow_df.groupby('day_of_week').agg({
        'first_hour_range': ['mean', 'count'],
        'net_change': 'mean'
    }).round(2)

    print("\nDay of Week Statistics:")
    print(dow_stats)

# ============================================================================
# 7. CORRELATION: FIRST CANDLE SIZE VS DAY RANGE
# ============================================================================
print(f"\n{'='*80}")
print("7. CORRELATION: FIRST CANDLE SIZE VS DAY RANGE")
print(f"{'='*80}")

correlation_data = []
for date in dates:
    day_data = df[df['date'] == date]
    first_candle = day_data[day_data['time'] == time(9, 15)]

    if len(first_candle) > 0 and len(day_data) > 1:
        first_candle_size = first_candle.iloc[0]['candle_size']
        first_candle_range = first_candle.iloc[0]['candle_range']
        day_range = day_data['high'].max() - day_data['low'].min()

        correlation_data.append({
            'first_candle_size': first_candle_size,
            'first_candle_range': first_candle_range,
            'day_range': day_range
        })

corr_df = pd.DataFrame(correlation_data)

if len(corr_df) > 1:
    corr_size = corr_df['first_candle_size'].corr(corr_df['day_range'])
    corr_range = corr_df['first_candle_range'].corr(corr_df['day_range'])

    print(f"\nCorrelation between:")
    print(f"  First Candle Size (|Close-Open|) and Day Range: {corr_size:.4f}")
    print(f"  First Candle Range (High-Low) and Day Range: {corr_range:.4f}")

    print(f"\nInterpretation:")
    if abs(corr_size) > 0.5:
        print(f"  STRONG correlation - larger first candles tend to predict larger day ranges")
    elif abs(corr_size) > 0.3:
        print(f"  MODERATE correlation - some predictive value")
    else:
        print(f"  WEAK correlation - first candle size is not a strong predictor of day range")

# ============================================================================
# 8. FIRST HOUR HIGH/LOW AS DAY'S HIGH/LOW
# ============================================================================
print(f"\n{'='*80}")
print("8. FIRST HOUR HIGH/LOW AS DAY'S HIGH/LOW")
print(f"{'='*80}")

extreme_analysis = []
for date in dates:
    day_data = df[df['date'] == date]
    first_hour_data = day_data[(day_data['time'] >= time(9, 15)) & (day_data['time'] <= time(10, 0))]

    if len(first_hour_data) > 0 and len(day_data) > 4:
        first_hour_high = first_hour_data['high'].max()
        first_hour_low = first_hour_data['low'].min()
        day_high = day_data['high'].max()
        day_low = day_data['low'].min()

        is_day_high = abs(first_hour_high - day_high) < 0.01
        is_day_low = abs(first_hour_low - day_low) < 0.01

        extreme_analysis.append({
            'date': date,
            'is_day_high': is_day_high,
            'is_day_low': is_day_low,
            'both': is_day_high and is_day_low
        })

extreme_df = pd.DataFrame(extreme_analysis)

if len(extreme_df) > 0:
    day_high_count = extreme_df['is_day_high'].sum()
    day_low_count = extreme_df['is_day_low'].sum()
    both_count = extreme_df['both'].sum()

    print(f"\nOut of {len(extreme_df)} trading days:")
    print(f"  First hour HIGH = Day's HIGH: {day_high_count} ({day_high_count/len(extreme_df)*100:.2f}%)")
    print(f"  First hour LOW = Day's LOW: {day_low_count} ({day_low_count/len(extreme_df)*100:.2f}%)")
    print(f"  Both HIGH and LOW set in first hour: {both_count} ({both_count/len(extreme_df)*100:.2f}%)")

# ============================================================================
# 9. VOLUME PATTERNS
# ============================================================================
print(f"\n{'='*80}")
print("9. VOLUME PATTERNS: FIRST HOUR VS REST OF DAY")
print(f"{'='*80}")

if 'volume' in df.columns:
    volume_analysis = []
    for date in dates:
        day_data = df[df['date'] == date]
        first_hour_data = day_data[(day_data['time'] >= time(9, 15)) & (day_data['time'] <= time(10, 0))]
        rest_of_day_data = day_data[day_data['time'] > time(10, 0)]

        if len(first_hour_data) > 0 and len(rest_of_day_data) > 0:
            first_hour_vol = first_hour_data['volume'].sum()
            rest_vol = rest_of_day_data['volume'].sum()
            day_vol = day_data['volume'].sum()

            volume_analysis.append({
                'date': date,
                'first_hour_volume': first_hour_vol,
                'rest_of_day_volume': rest_vol,
                'day_volume': day_vol,
                'first_hour_pct': (first_hour_vol / day_vol * 100) if day_vol > 0 else 0
            })

    vol_df = pd.DataFrame(volume_analysis)

    if len(vol_df) > 0:
        print(f"\nAverage Volume:")
        print(f"  First Hour (9:15-10:00): {vol_df['first_hour_volume'].mean():,.0f}")
        print(f"  Rest of Day: {vol_df['rest_of_day_volume'].mean():,.0f}")
        print(f"  Full Day: {vol_df['day_volume'].mean():,.0f}")
        print(f"\nFirst Hour as % of Day Volume: {vol_df['first_hour_pct'].mean():.2f}%")
else:
    print("\nVolume column not found in data")

print(f"\n{'='*80}")
print("ANALYSIS COMPLETE")
print(f"{'='*80}")
