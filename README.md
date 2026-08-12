# ETF V2 动量轮动策略 · 云端自动扫描（GitHub Actions 版）

每天北京时间 **14:25**（周一至周五）由 **GitHub Actions 云端服务器**执行，
**电脑关机 / 休眠完全不影响**。扫描结果自动双通道推送：
- 💬 微信（Server酱 · 方糖公众号）
- 📧 邮箱（163 SMTP 直发，无确认环节）

## 架构

```
GitHub Actions 定时任务 (cron: 25 6 * * 1-5 UTC = 北京 14:25)
   │
   ├─ 拉取 4 只 ETF 日K线（腾讯财经接口，国内秒开）
   ├─ 计算 22日动量 + BIAS 三项检测 + V2 十二规则决策
   ├─ 持仓状态 → 提交回仓库持久化（下次运行读取）
   └─ 双通道推送 → 微信(Server酱) + 邮件(163 SMTP)
```

## 文件说明

| 文件 | 作用 |
|------|------|
| `etf_v2_scan.py` | 策略主脚本：数据源 + 决策 + 推送 |
| `runtime/etf_v2_state.json` | 持仓状态（云端每次运行后自动提交回仓库） |
| `.github/workflows/etf-scan.yml` | GitHub Actions 定时工作流 |
| `.gitignore` | 忽略敏感配置，密钥绝不入库 |

## 本地运行

```bash
python etf_v2_scan.py            # 实盘模式（更新状态+双通道推送）
python etf_v2_scan.py --dry      # 干跑（不更新不推送）
python etf_v2_scan.py --set-serverchan <KEY>   # 配置微信推送密钥
python etf_v2_scan.py --set-email <授权码>     # 配置163邮箱推送
```

## GitHub 部署步骤（一次性）

### 1. 创建仓库（已完成 ✅）
GitHub 上新建 **Private** 仓库 `etf-v2-scan`，不要勾选任何初始化选项。

### 2. 用 GitHub Desktop 发布代码
1. GitHub Desktop → 登录账号
2. `File` → `Add Local Repository` → 选择本文件夹：
   `C:\Users\w1820\WorkBuddy\2026-08-11-16-23-38`
3. `Publish repository` → 保持 **Private** → Publish

### 3. 配置两个密钥（Secrets）
仓库页面 → `Settings` → `Secrets and variables` → `Actions`：

| Name | Secret |
|------|--------|
| `SERVERCHAN_KEY` | 方糖 SendKey |
| `SMTP_PASS` | 163 SMTP 授权码 |

发件人/收件人邮箱已在工作流中写死为 `18201691896@163.com`，无需配置。

### 4. 手动触发验证
仓库页面 → `Actions` → 左侧 `ETF V2 策略扫描` → `Run workflow` → 运行成功后微信+邮箱各收一条。

之后每个交易日 14:25 自动执行，全程无需电脑开机。
