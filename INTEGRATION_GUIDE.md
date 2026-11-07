# 激进交易系统集成指南
═══════════════════════════════════════════════════════════════════════════════

**版本**: 2.0-aggressive  
**更新时间**: 2026-10-10 12:25 UTC  
**目标**: 小本金（50-100U）高风险高收益策略  
**环境**: Hyperliquid Testnet (999U) → Mainnet (50-100U)

---

## 📦 新增文件清单

### 核心模块
1. **config_aggressive.json** - 激进风控配置文件
2. **utils_v2.py** - 优化后的指标库（向量化 + Look-ahead防护）
3. **risk_manager.py** - 风险管理模块（Kelly + Portfolio Heat + DD熔断）
4. **multi_coin_router.py** - 多币种轮动调度器
5. **walk_forward_optimizer.py** - 参数优化脚本

### 文档
6. **OPTIMIZATION_REPORT.md** - 深度优化执行报告
7. **INTEGRATION_GUIDE.md** - 本文件

---

## 🎯 核心改动总结

### 1. 杠杆策略（6-8x动态）

```python
# 原系统: 固定低杠杆（隐含约2-3x）
# 新系统: 动态杠杆 + 信号强度联动

def calculate_leverage(signal_score, macro_aligned, correlation):
    base = 6  # 基准6x
    
    if signal_score >= 85 and macro_aligned and correlation < 0.7:
        return 8  # 高确定性 → 8x
    elif signal_score >= 75:
        return 6  # 标准 → 6x
    else:
        return 4  # 不确定性高 → 4x

# 单笔风险 = leverage * stop_loss_pct
# 6x * 4% = 24% 本金风险（激进但不极端）
```

### 2. 多币种轮动（单持仓模式）

```python
# 原系统: 单币种（BTC或ETH）
# 新系统: 4币种实时竞争，100%资金投入最强信号

coins = ["BTC", "ETH", "SUI", "LINK"]
router = MultiCoinRouter()

# 每个信号节点：
best_signal = router.select_best_coin([
    evaluate(BTC),  # score=85
    evaluate(ETH),  # score=72
    evaluate(SUI),  # score=91  ← 胜出
    evaluate(LINK), # score=68
])

# 相关性软约束：
if current_position == BTC and best_signal == ETH:
    if correlation(BTC, ETH) > 0.8:
        best_signal.score *= 0.7  # 降权20-40%
```

### 3. 风控熔断（激进参数）

| 参数                  | 原值  | 新值   | 理由                    |
|-----------------------|-------|--------|-------------------------|
| Max Drawdown          | 15%   | 30%    | 小本金容忍更大回撤      |
| Daily Loss Limit      | 5%    | 15%    | 允许单日大波动          |
| Consecutive Losses    | 3次   | 4次    | 减少误触发              |
| Single Risk           | 2%    | 24%    | 6x杠杆*4%止损           |
| Kelly Fraction        | 0.25  | 0.8    | 接近Full Kelly          |

### 4. 测试网/主网切换

```python
# .env 配置
HYPE_ENV=testnet          # 或 mainnet
HYPE_BASE_URL=            # 留空自动解析
INITIAL_EQUITY=999.0      # 测试网999U / 主网50-100U

# 代码自动识别
if os.getenv("HYPE_ENV") == "testnet":
    logger.info("运行在测试网（999U）")
else:
    logger.warning("运行在主网，请谨慎操作")
```

---

## 🔧 集成步骤（分阶段）

### Phase 1: 准备与验证（1-2天）

#### 1.1 备份现有代码
```bash
# 创建时间戳备份
cp -r /workspace /workspace_backup_$(date +%s)

# 或使用Git
cd /workspace
git add .
git commit -m "备份：集成激进策略前"
git tag v1.0-conservative
```

#### 1.2 替换优化后的指标库
```bash
# 备份原文件
mv utils.py utils_old.py

# 使用优化版本
mv utils_v2.py utils.py
```

#### 1.3 运行单元测试（确保兼容性）
```bash
# 测试utils.py的关键函数
python -c "
import utils
import numpy as np

# 测试ATR
h = np.array([100, 102, 101, 103, 105])
l = np.array([98, 100, 99, 101, 103])
c = np.array([99, 101, 100, 102, 104])
atr = utils.atr(h, l, c, period=3)
print(f'ATR测试: {atr:.2f}')

# 测试RSI
closes = np.array([100, 102, 101, 103, 105, 104, 106, 108])
rsi = utils.rsi(closes, period=6)
print(f'RSI测试: {rsi:.2f}')
"
```

---

### Phase 2: 集成核心模块（2-3天）

#### 2.1 修改 main.py - 初始化部分

在 `TradingBot.__init__()` 中添加：

```python
# 在现有导入后添加
from multi_coin_router import MultiCoinRouter
from risk_manager import RiskManager, RiskConfig

class TradingBot:
    def __init__(self):
        # ... 现有代码 ...
        
        # ===== 新增：多币种路由器 =====
        self.router = MultiCoinRouter(config_path="config_aggressive.json")
        logger.info("[启动] 多币种路由器已加载 | coins=%s", self.router.coins)
        
        # ===== 新增：风险管理器 =====
        risk_cfg = RiskConfig(
            kelly_fraction=0.8,
            max_single_risk_pct=24.0,
            max_drawdown_pct=30.0,
            daily_loss_limit_pct=15.0,
            max_consecutive_losses=4
        )
        initial_equity = float(os.getenv("INITIAL_EQUITY", "999.0"))
        self.risk_mgr = RiskManager(
            config=risk_cfg,
            initial_equity=initial_equity,
            persistence_file="risk_state_aggressive.json"
        )
        logger.info("[启动] 风险管理器已加载 | 初始资金=%.2fU", initial_equity)
```

#### 2.2 修改 main.py - 信号评估部分

在 `_idle_cycle()` 的信号评估处：

```python
def _idle_cycle(self):
    # ... 前面的宏观判断代码保持不变 ...
    
    # ===== 原代码（单币种）=====
    # signal_result = calculate_signal_score(
    #     df_5m, df_1h, df_4h, fr_stats, price, self.final_macro_state
    # )
    # if signal_result is None or signal_result["score"] < SIGNAL_GATE:
    #     return
    
    # ===== 新代码（多币种轮动）=====
    from strategy import calculate_signal_score, calculate_quant_trs
    
    best_signal = self.router.get_routing_decision(
        calculate_signal_fn=calculate_signal_score,
        calculate_trs_fn=calculate_quant_trs,
        macro_state=self.final_macro_state
    )
    
    if best_signal is None:
        return  # 无合格信号或不需要切换
    
    # 开仓前风控检查
    estimated_risk = self.risk_mgr.current_equity * 0.24  # 24%风险
    can_trade, reason = self.risk_mgr.can_open_position(estimated_risk)
    if not can_trade:
        logger.warning(f"[风控] 拒绝开仓 | {reason}")
        return
    
    # 如果需要切换币种，先平掉当前仓位
    if self.position is not None and best_signal.coin != self.router.current_position.coin:
        logger.info(f"[轮动] 币种切换 | {self.router.current_position.coin} → {best_signal.coin}")
        self._close_position(reason="币种切换")
        time.sleep(1)  # 短暂等待确保平仓完成
    
    # 计算动态杠杆
    leverage = self._calculate_dynamic_leverage(
        signal_score=best_signal.score,
        macro_aligned=(best_signal.macro_state == self.final_macro_state.name),
        correlation=0.5  # TODO: 从router获取实际相关性
    )
    
    # 计算开仓规模（使用动态杠杆）
    notional_size = self.risk_mgr.get_adaptive_position_size(
        stop_loss_distance=abs(best_signal.price - best_signal.sl),
        current_price=best_signal.price,
        use_kelly=True
    )
    
    # 应用杠杆调整
    notional_size = notional_size * (leverage / 3.0)  # 假设原函数基于3x杠杆
    
    # 开仓（使用best_signal的参数）
    self._open_position(
        direction=best_signal.direction,
        coin=best_signal.coin,
        price=best_signal.price,
        tp=best_signal.tp,
        sl=best_signal.sl,
        notional_size=notional_size
    )
    
    # 注册持仓到路由器
    self.router.register_position(best_signal)
    
    # 注册持仓到风控器
    self.risk_mgr.register_position(
        position_id=f"{best_signal.coin}_{int(time.time())}",
        risk_amount=estimated_risk
    )
```

#### 2.3 添加动态杠杆计算函数

在 `TradingBot` 类中添加：

```python
def _calculate_dynamic_leverage(self, signal_score: float, 
                               macro_aligned: bool,
                               correlation: float) -> int:
    """
    动态杠杆计算（6-8x）
    
    参数:
        signal_score: 信号分数（70-100）
        macro_aligned: 信号方向是否与宏观一致
        correlation: 与当前持仓的相关性（如果有）
    
    返回:
        杠杆倍数（4-8）
    """
    base_leverage = 6
    
    # 高确定性：score>=85 且 宏观同向 且 相关性低
    if signal_score >= 85 and macro_aligned and correlation < 0.7:
        leverage = 8
        logger.info(f"[杠杆] 使用最大杠杆 | score={signal_score:.1f} | leverage=8x")
    
    # 标准情况：score>=75
    elif signal_score >= 75:
        leverage = 6
        logger.info(f"[杠杆] 使用标准杠杆 | score={signal_score:.1f} | leverage=6x")
    
    # 不确定性高：score<75 或 宏观冲突
    else:
        leverage = 4
        logger.info(f"[杠杆] 使用保守杠杆 | score={signal_score:.1f} | leverage=4x")
    
    return leverage
```

#### 2.4 修改平仓逻辑

在平仓完成后添加清理：

```python
def _close_position(self, reason: str):
    # ... 现有平仓代码 ...
    
    # 记录交易到风控器
    if self.position and self.position_entry_price:
        from risk_manager import TradeRecord
        
        pnl = self._calculate_realized_pnl()  # 需要实现此函数
        
        trade = TradeRecord(
            entry_time=self.position.entry_time.isoformat(),
            exit_time=datetime.now(timezone.utc).isoformat(),
            direction=self.position_direction,
            entry_price=self.position_entry_price,
            exit_price=self.position.current_price,
            size=self.position.size,
            pnl=pnl,
            pnl_r=pnl / abs(self.position_entry_price - self.position.stop_loss),
            hold_duration_minutes=int((datetime.now(timezone.utc) - self.position.entry_time).total_seconds() / 60),
            exit_reason=reason
        )
        
        self.risk_mgr.record_trade(trade)
        logger.info(f"[风控] 交易已记录 | PnL={pnl:.2f}U | R={trade.pnl_r:.2f}R")
    
    # 清理路由器持仓记录
    self.router.clear_position()
    
    # 清理风控器持仓记录
    position_id = f"{self.position.coin}_{int(time.time())}"  # 需要保存到self.position
    self.risk_mgr.unregister_position(position_id)
```

---

### Phase 3: 测试网验证（1周）

#### 3.1 配置测试网环境

编辑 `.env`:
```bash
# Hyperliquid 配置
HYPE_ENV=testnet
HYPE_COIN=BTC  # 主要用于AI_macro，实际交易由router决定
INITIAL_EQUITY=999.0

# 杠杆与风控
BASE_LEVERAGE=6
MAX_LEVERAGE=8
SINGLE_RISK_PCT=24.0

# AI 通道（保持不变）
THIRD_PARTY_API_KEY=your_key_here
THIRD_PARTY_BASE_URL=https://api.ludies.best/v1
```

#### 3.2 启动测试

```bash
# 首次运行建议启用详细日志
export LOG_LEVEL=DEBUG

# 运行机器人
python main.py

# 实时监控（另一个终端）
tail -f trading_bot.log | grep -E "\[轮动\]|\[风控\]|\[杠杆\]"
```

#### 3.3 验证检查项

**日志检查**:
```bash
# 检查多币种是否正常扫描
grep "\[MultiCoin\]" trading_bot.log | tail -20

# 检查杠杆是否动态调整
grep "\[杠杆\]" trading_bot.log | tail -10

# 检查风控是否生效
grep "\[风控\]" trading_bot.log | tail -10
```

**状态文件检查**:
```bash
# 查看风险状态
cat risk_state_aggressive.json | jq '.metrics'

# 查看当前持仓
cat state_aggressive.json | jq '.position'
```

**预期行为**:
- [ ] 每个信号节点扫描4个币种
- [ ] 选出最高分币种开仓
- [ ] 杠杆在4-8x之间动态调整
- [ ] 单笔风险约24%本金
- [ ] 相关性>0.7时降权生效

---

### Phase 4: 参数优化（可选，2-3天）

#### 4.1 准备历史数据

下载BTC/ETH/SUI/LINK的历史K线（至少6个月）：

```python
# download_data.py
from exchange import MarketData
import pandas as pd

coins = ["BTC", "ETH", "SUI", "LINK"]
timeframes = ["5m", "1h", "4h"]

for coin in coins:
    market = MarketData(coin=coin)
    for tf in timeframes:
        df = market.get_ohlcv(tf, lookback_bars=10000)
        df.to_csv(f"historical_data/{coin}_{tf}.csv", index=False)
        print(f"已保存 {coin} {tf} | {len(df)} 根K线")
```

#### 4.2 运行Walk-Forward优化

```bash
# 创建输出目录
mkdir -p wf_results

# 运行优化（预计2-3小时）
python walk_forward_optimizer.py \
    --start 2024-01-01 \
    --end 2024-07-01 \
    --output wf_results \
    --processes 8

# 查看结果
cat wf_results/recommended_params.json
```

#### 4.3 应用优化参数

根据`recommended_params.json`更新`config_aggressive.json`:

```json
{
  "indicators": {
    "ema_period_1h": 200,      // ← 来自优化结果
    "ema_period_4h": 250,      // ← 来自优化结果
    "atr_sl_multiplier": 2.0,  // ← 来自优化结果
    "rsi_oversold": 35,        // ← 来自优化结果
    "rsi_overbought": 65       // ← 来自优化结果
  },
  "position_sizing": {
    "kelly_fraction": 0.5      // ← 来自优化结果
  }
}
```

重启机器人使配置生效。

---

### Phase 5: 主网部署（慎重！）

#### 5.1 最终检查清单

- [ ] 测试网运行 >= 1 周无异常
- [ ] 至少完成 10 笔交易，验证逻辑正确
- [ ] 风控指标健康：Win Rate > 40%, Max DD < 30%
- [ ] 相关性降权机制生效（查看日志）
- [ ] 动态杠杆正常工作（6-8x之间）

#### 5.2 切换主网配置

**重要**: 小额测试！

编辑 `.env`:
```bash
HYPE_ENV=mainnet          # ← 切换主网
INITIAL_EQUITY=50.0       # ← 小额起步（50U）

# 其他保持不变
```

#### 5.3 资金管理建议

| 阶段      | 本金   | 目标                    | 风险提示                |
|-----------|--------|-------------------------|-------------------------|
| 第1周     | 50U    | 验证策略可行性          | 可能归零，做好心理准备  |
| 第2-4周   | 50-100U| 积累交易数据            | 连败3-4次可能-50%       |
| 第2个月   | 100-200U| 参数微调与优化          | 回撤30%是正常波动       |
| 第3个月+  | 200U+  | 稳定盈利/复利增长       | 定期提现利润降低风险    |

#### 5.4 止损规则（人工干预）

**强制离场条件**:
- 本金亏损超过50%（50U → 25U）
- 单日亏损超过20%（极端行情）
- 连续5笔交易全部止损

**这时应该**:
1. 停止交易1-2周
2. 复盘所有交易记录
3. 检查策略是否失效（市场环境变化）
4. 考虑降低杠杆到4x重新开始

---

## 📊 监控与告警

### 关键指标看板

每日检查：

```bash
# 生成风险报告
python -c "
from risk_manager import RiskManager
rm = RiskManager(persistence_file='risk_state_aggressive.json')
print(rm.get_risk_report())
"
```

输出示例：
```
═══════════════════════════════════════════════════════════
                    风险管理报告
═══════════════════════════════════════════════════════════
总交易次数: 15
胜率: 53.33%  (8W / 7L)
平均盈利: $12.30  |  平均亏损: $8.50
Profit Factor: 1.73

总盈亏: $26.50  (+26.50%)
当前资金: $126.50  |  初始: $100.00

最大回撤: 18.20%
当前回撤: 5.30%
峰值资金: $133.20

Sharpe Ratio: 1.42
Sortino Ratio: 1.89

最大连胜: 4 次
最大连败: 3 次
当前状态: 连胜 2 次

活跃持仓数: 1
组合风险敞口: $24.00
═══════════════════════════════════════════════════════════
```

### Telegram告警（可选）

编辑 `alerts.py` 添加：

```python
def send_telegram_alert(message: str):
    """发送Telegram告警"""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if not bot_token or not chat_id:
        return
    
    import requests
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": message})

# 在关键位置调用
send_telegram_alert(f"🚨 触发回撤熔断 | 当前DD={current_dd:.1f}%")
```

---

## 🐛 常见问题排查

### Q1: 多币种扫描很慢，每轮要30秒

**原因**: 4个币种串行拉取行情  
**解决**: 使用多线程并行

```python
from concurrent.futures import ThreadPoolExecutor

def evaluate_all_coins_parallel(self):
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(self.evaluate_coin_signal, coin): coin 
            for coin in self.coins
        }
        signals = [f.result() for f in futures.values() if f.result()]
    return signals
```

### Q2: 相关性一直是0.0

**原因**: 价格历史数据不足（<20个样本）  
**解决**: 等待24小时积累数据，或手动导入历史价格

### Q3: 杠杆总是4x，从不用8x

**原因**: 信号分数很少超过85分  
**解决**: 
1. 检查`signal_gate`是否过高（建议70）
2. 降低85分阈值到80分
3. 查看信号评分日志，分析为什么分数低

### Q4: 风控总是拒绝开仓

**原因**: 触发某个熔断条件  
**解决**: 查看日志中的拒绝原因

```bash
grep "风控拒绝" trading_bot.log | tail -5
# 可能输出: "触发最大回撤熔断 (28.5% >= 30%)"
```

---

## 📚 下一步建议

1. **实盘前必做**：
   - 在测试网至少跑满100笔交易
   - 验证Win Rate > 40%, Calmar > 2.0
   - 手动复盘每笔交易的逻辑

2. **主网初期**：
   - 每日查看风险报告
   - 记录每笔交易的心理感受（贪婪/恐惧）
   - 不要频繁调参（至少1个月验证周期）

3. **长期优化**：
   - 每月运行一次Walk-forward优化
   - 监控市场regime变化（牛市/熊市切换）
   - 考虑加入止盈后加仓逻辑（金字塔）

---

## ⚠️ 免责声明

**本系统为高风险量化策略，存在本金归零可能性。**

- 6-8x杠杆在极端行情下可能快速爆仓
- 小本金翻倍的同时也可能48小时归零
- 加密货币市场7x24h交易，无涨跌停保护
- AI宏观判断可能失效，造成方向性错误

**请确保**:
- 投入资金为可承受全损金额
- 理解Kelly Criterion与破产概率
- 有能力承担连败4次后的心理压力
- 不依赖此策略作为主要收入来源

**使用即代表你已充分理解风险。Good Luck! 🚀**
