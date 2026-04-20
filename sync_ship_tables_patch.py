"""
╔══════════════════════════════════════════════════════════════╗
║  hormuz-updater.py 补丁 — tableRows三者联动                   ║
║  日期: 2026-04-20                                             ║
║  目的: shipCount/barData/tableRows 永远一致，不再出现"分裂"   ║
╚══════════════════════════════════════════════════════════════╝

【问题背景】
原版脚本只维护 barData（IMF合并）和 shipCount（FORCE_SHIPCOUNT），
tableRows 是完全独立字段，手动改 shipCount 时明细表不跟着动，
导致首屏大数字是3艘，柱状图是3艘，但明细表"● 4/20"还停在5艘。

【解决方案】
新增 sync_ship_tables(data) 函数，在 write_gist() 之前调用，
以 shipCount 为真源，强制把 barData 最后一条和 tableRows 最后一条都校准。

【集成步骤】
1. 把下面的 sync_ship_tables() 函数贴到 hormuz-updater.py 中
   （建议贴在 write_gist 函数的正前方，约第222行）

2. 在 main() 函数末尾，找到这段：
       current['lastUpdate'] = datetime.now().strftime('%Y-%m-%d %H:%M')
       save_local(current)   # 先落盘本地
       write_gist(current)   # 再推到CF Worker
   改成：
       current['lastUpdate'] = datetime.now().strftime('%Y-%m-%d %H:%M')
       sync_ship_tables(current)   # ⭐ 新增：三者联动校准
       save_local(current)
       write_gist(current)
"""

from datetime import datetime, timezone, timedelta


def sync_ship_tables(data):
    """
    三者联动校准：以 shipCount 为真源，强制同步 barData 和 tableRows 的最后一条。

    副作用字段（全部以 shipCount 为基准重算）：
      - barData[-1]: totalH, count, latest=True
      - tableRows[-1]: count, warChg, warDir, pct, trend
      - shipChg: 较昨日
      - congestion: 占和平时期135艘的比例

    保留原始用户手动设置（不会改的字段）：
      - oilH（油轮数，需要独立数据源）
      - barData[:-1] 历史条目
      - tableRows[:-1] 历史条目
      - congestionNote（封锁天数/新闻描述，仍需人工维护）
    """
    ship = data.get('shipCount')
    if ship is None:
        print('⚠️ sync_ship_tables: shipCount为空，跳过联动')
        return data

    try:
        ship = int(ship)
    except (ValueError, TypeError):
        print(f'⚠️ sync_ship_tables: shipCount非法 {ship!r}，跳过')
        return data

    # 北京时间今日日期
    bj = datetime.now(timezone(timedelta(hours=8)))
    today_str = f'{bj.month}/{bj.day}'          # 例: "4/20"
    today_dot = f'● {today_str}'                # 例: "● 4/20"  tableRows的格式

    # ========== 1. 同步 barData 最后一条 ==========
    bars = data.get('barData', [])
    if isinstance(bars, list) and bars:
        # 清所有latest标记
        for b in bars:
            b['latest'] = False
        # 找今日条目；找不到就append新的
        today_bar = None
        for b in bars:
            if b.get('date') == today_str:
                today_bar = b
                break
        if today_bar is None:
            today_bar = {'date': today_str, 'oilH': 0}
            bars.append(today_bar)
        today_bar['totalH'] = ship
        today_bar['count'] = ship
        today_bar['latest'] = True
        data['barData'] = bars
        print(f'🔗 sync barData[-1]: {today_str} → {ship}艘')

    # ========== 2. 计算 shipChg（较昨日） ==========
    prev_count = None
    if isinstance(bars, list) and len(bars) >= 2:
        # 排序后倒数第二条即昨日
        def date_key(b):
            try:
                m, d = b.get('date', '0/0').split('/')
                return (int(m), int(d))
            except Exception:
                return (0, 0)
        sorted_bars = sorted(bars, key=date_key)
        if len(sorted_bars) >= 2 and sorted_bars[-1].get('date') == today_str:
            prev_bar = sorted_bars[-2]
            prev_count = prev_bar.get('count', prev_bar.get('totalH'))

    if prev_count is not None:
        try:
            prev_count = int(prev_count)
            delta = ship - prev_count
            data['shipChg'] = f'+{delta}' if delta > 0 else str(delta)
            print(f'🔗 sync shipChg: 较昨日{prev_count}艘 → {data["shipChg"]}')
        except (ValueError, TypeError):
            data['shipChg'] = '0'
    else:
        data['shipChg'] = '0'

    # ========== 3. congestion（拥堵占比）==========
    data['congestion'] = round(ship / 135 * 100)
    print(f'🔗 sync congestion: {data["congestion"]}%')

    # ========== 4. 同步 tableRows 最后一条 ==========
    rows = data.get('tableRows', [])
    if isinstance(rows, list) and rows:
        # 找 today_dot 条目（"● 4/20"格式）；找不到就找普通today_str；再找不到就append
        today_row = None
        for r in rows:
            if r.get('week') == today_dot or r.get('week') == today_str:
                today_row = r
                break

        # 计算较昨日涨跌（tableRows里昨日取倒数第二条）
        prev_row_count = None
        if len(rows) >= 2:
            # 把今日行挪到最后再取上一条
            non_today_rows = [r for r in rows if r is not today_row]
            if non_today_rows:
                prev_row_count = non_today_rows[-1].get('count')

        if prev_row_count is not None:
            try:
                prev_row_count = int(prev_row_count)
            except (ValueError, TypeError):
                prev_row_count = None

        if today_row is None:
            today_row = {'week': today_dot}
            rows.append(today_row)

        # 强制今日行的week格式为 "● M/D"（高亮标识）
        today_row['week'] = today_dot
        today_row['count'] = ship

        if prev_row_count is not None:
            delta = ship - prev_row_count
            today_row['warChg'] = f'+{delta}' if delta > 0 else str(delta)
            if delta > 0:
                today_row['warDir'] = 'up'
                today_row['trend'] = 'up'
            elif delta < 0:
                today_row['warDir'] = 'down'
                today_row['trend'] = 'down'
            else:
                today_row['warDir'] = 'flat'
                today_row['trend'] = 'flat'
        else:
            today_row['warChg'] = '0'
            today_row['warDir'] = 'flat'
            today_row['trend'] = 'flat'

        today_row['pct'] = f'{round(ship / 135 * 100, 1)}%'

        data['tableRows'] = rows
        print(f'🔗 sync tableRows[-1]: {today_dot} → {ship}艘 ({today_row["warChg"]}) {today_row["pct"]}')

    print(f'✅ sync_ship_tables 完成：shipCount={ship} | barData/tableRows/shipChg/congestion 已全部对齐')
    return data


# ============ 单元测试 ============
if __name__ == '__main__':
    # 模拟一份数据测试
    test_data = {
        'shipCount': 3,
        'barData': [
            {'date': '4/18', 'totalH': 8, 'count': 8, 'oilH': 2, 'latest': False},
            {'date': '4/19', 'totalH': 5, 'count': 5, 'oilH': 2, 'latest': False},
            {'date': '4/20', 'totalH': 5, 'count': 5, 'oilH': 0, 'latest': True},  # 这条要被改成3
        ],
        'tableRows': [
            {'week': '4/18', 'count': 8, 'warDir': 'down', 'warChg': '-7', 'pct': '5.9%', 'trend': 'down'},
            {'week': '4/19', 'count': 5, 'warDir': 'down', 'warChg': '-3', 'pct': '3.7%', 'trend': 'down'},
            {'week': '● 4/20', 'count': 5, 'warDir': 'down', 'warChg': '0', 'pct': '3.7%', 'trend': 'down'},
        ],
    }

    print('=' * 60)
    print('测试前:')
    print(f"  shipCount: {test_data['shipCount']}")
    print(f"  barData[-1]: {test_data['barData'][-1]}")
    print(f"  tableRows[-1]: {test_data['tableRows'][-1]}")
    print()

    sync_ship_tables(test_data)

    print()
    print('测试后:')
    print(f"  shipCount: {test_data['shipCount']}")
    print(f"  shipChg: {test_data.get('shipChg')}")
    print(f"  congestion: {test_data.get('congestion')}%")
    print(f"  barData[-1]: {test_data['barData'][-1]}")
    print(f"  tableRows[-1]: {test_data['tableRows'][-1]}")
    print()
    # 断言
    assert test_data['barData'][-1]['totalH'] == 3
    assert test_data['barData'][-1]['count'] == 3
    assert test_data['barData'][-1]['latest'] == True
    assert test_data['tableRows'][-1]['count'] == 3
    assert test_data['tableRows'][-1]['warChg'] == '-2'
    assert test_data['shipChg'] == '-2'
    print('✅ 所有断言通过！')
