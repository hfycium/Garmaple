# Garmaple

**不用把佳明账号密码交给任何第三方，让 AI 分析你的训练数据。**

Garmaple 读取佳明官方「导出您的数据」归档包，在本地建立索引，通过 MCP 把分析能力暴露给 AI 助手。全程不联网、不登录、不逆向。

---

## 为什么需要这个项目

### 现有方案有一个共同的根本问题

Garmin 没有官方 MCP。目前 GitHub 上所有能用 AI 读佳明数据的项目，都走同一条路：**反向工程 Garmin Connect 的私有接口**，也就是要求你输入账号密码，换一个 token，然后拿 token 去调接口。

这条路能跑通，但代价是永久的：

- **密码或 token 必须交给第三方代码**。哪怕作者再诚实，token 泄露就等于账号泄露。
- **接口随时会变**。Garmin 改一次服务端，工具就失效一次，得等作者适配。
- **中国区基本用不了**。这是最实际的问题，下面单独说。

### 中国区：所有在线方案都没修好的地方

`connect.garmin.cn`（中国区）与 `connect.garmin.com`（国际区）是两套完全隔离的服务器，账号不互通、数据不互通。

主流项目 [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp)（1396★）的仓库里，中国区认证修复**开了三个 PR，一个都没合**：

| PR | 状态 | 说明 |
|---|---|---|
| #180 | 可合并，**未合** | +267/-0 纯新增，带 156 行单元测试，2026-07 提交 |
| #249 | **冲突** | +387/-92，与主干冲突 |
| #268 | **冲突** | +303/-59，与主干冲突 |

根因很具体：登录时先走 mobile flow，而该流程的 service URL 未做域名适配；DI OAuth token 交换硬编码全球端点 `diauth.garmin.com`，中国区应走 `diauth.garmin.cn`。

即便打了补丁，仍有人报告 ticket 换 token 时连 `mobile.integration.garmin.com:443`，TLS 在 ClientHello 之后被立刻断开（issue #257）。

**三个 PR 修同一个问题、全都挂着**，说明维护者没有中国区账号，无法验证。这个问题不会有人替你解决。

### Garmaple 的做法：不碰认证

佳明自带「导出您的数据」功能，邮件会发来一份完整归档，里面有全部原始 `.fit` 文件与结构化 JSON。这是账号自带的能力，**不需要开发者资格、不需要密码外传、不受接口改版影响**。

Garmaple 就只读这份归档。

| | 现有的 20+ 个 Garmin MCP | Garmaple |
|---|---|---|
| 认证 | 要密码 / MFA，token 会过期 | **不需要任何登录** |
| 中国区 | 认证流程有 bug，补丁未合 | **完全可用**（导出功能本身无区限制） |
| 接口改版 | 服务端一变就失效 | **不受影响**，读的是静态归档 |
| 字段完整度 | API 会剥离部分原始字段 | **原始 FIT 全字段在手** |
| 实时性 | 实时 | **需重新导出**（这是唯一代价） |

### 代价说明

Garmaple **不是实时工具**。数据新鲜度取决于你上次导出的时间。

如果你的需求是「问 AI 我今天早上的跑量」——现在这个项目给不了。如果你要的是「分析我这两年的训练规律」——离线归档比在线接口更适合，因为它是全量历史，不是分页拉取。

---

## 你会得到什么

功能分三档，按实用性排序。**已实现的会在前面打勾**，当前全部为规划中。

### 第一档：数据地基

- [ ] `list_activities` — 按日期区间、运动类型筛选活动
- [ ] `get_activity` — 单次活动的完整指标（距离、时长、配速、心率、爬升、训练效果）
- [ ] `get_daily_summary` — 每日步数、静息心率、压力、身体电量、强度分钟
- [ ] `get_sleep` — 睡眠时长、分期、评分
- [ ] `get_period_summary` — 周 / 月 / 年训练量汇总

### 第二档：训练分析

- [ ] `get_training_load` — CTL / ATL / TSB（体能、疲劳、状态）趋势
- [ ] `get_training_status` — 训练状态、负荷隧道、体能水平趋势
- [ ] `get_hr_zones` — 心率区间分布与时间占比
- [ ] `get_pace_hr_drift` — 配速心率漂移（有氧解耦）
- [ ] `get_personal_records` — 个人最佳（5K / 10K / 半马 / 全马）
- [ ] `get_race_predictions` — 赛事成绩预测趋势
- [ ] `get_vo2max_trend` — 最大摄氧量变化
- [ ] `get_recovery_correlation` — 睡眠 / 压力 / 身体电量与训练表现的相关性
- [ ] `get_gear_mileage` — 跑鞋装备里程预警

### 第三档：原始数据与写回

- [ ] `get_activity_streams` — 逐秒心率、配速、海拔、步频（降采样后返回）
- [ ] `get_power_curve` — 功率持续曲线（需功率计）
- [ ] `export_workout_fit` — 生成训练课表 `.fit`，拷入手表 `NEW_FILES` 目录即导入

### 明确不做

| 不做的事 | 原因 |
|---|---|
| 实时数据查询 | 架构上就是离线归档 |
| 修改活动（改名、改类型、RPE、感受） | 需要在线写接口，且改坏无法撤销 |
| 删除任何数据 | 只读工具不该有删除能力 |
| 云端训练计划同步 | 需 Startup 层 API 资格 |

写回能力的替代方案是 `export_workout_fit`：生成标准课表 FIT 文件，用户自己拷进手表。这不需要任何接口授权，是设备自带功能。

---

## 安全

### 工具本身做什么

**只读本地文件、不联网、不上传、不调用任何远端接口。** 没有遥测、没有统计上报、没有云端同步。

（与现有方案的关键区别：它们必须联网，因为数据在佳明服务器上；Garmaple 的数据从一开始就在你硬盘里。）

### 但"工具不联网"不等于"数据不出本机"

这一点必须写清楚，否则是虚假承诺。数据链路是：

```
磁盘上的 .fit / .json
      │  解析（本机）
      ▼
   本地索引（SQLite）
      │  工具调用（本机）
      ▼
  MCP 返回值 ─────────► AI 上下文 ─────────► 模型服务商服务器
                   ↑
             真正的数据边界在这里
```

工具不联网，但**工具返回的内容会被 AI 读取，随后随对话发送给模型服务商**。所以外泄面不在文件解析环节，而在工具输出环节。

### 因此：脱敏发生在工具层

原则是**聚合优先、默认粗化**——尽可能返回统计结果，而不是原始采样点。

| 数据类型 | 默认处理 | 原因 |
|---|---|---|
| GPS 轨迹坐标 | **不返回**（`strict` 层），或降精度至约 1km 网格 | 起点终点即住址 |
| 活动开始时间 | 只到日期，可选只到「上午 / 下午 / 晚间」 | 精确时刻可反推作息 |
| Garmin activity ID | 不外露，内部映射为序号 | 可反查公开活动页 |
| 用户 ID / 设备序列号 | 永不输出 | 账号与设备标识 |
| 心率、配速等时序 | 默认返回统计量（均值 / 最大 / 分位） | 低采样率即可满足分析 |
| 距离、时长、训练负荷 | 原样返回 | 无位置与身份信息 |

三档隐私级别，通过配置切换：

- `strict`（默认）：无坐标、时间粗化、无原始 ID
- `normal`：无坐标，保留精确时间与 ID
- `raw`：全字段，需显式开启，启动时打印警告

**不要依赖 AI 侧「不会乱说」来保护数据**——一旦进入上下文就已经离开了本机。脱敏只能发生在数据出来之前。

### 自查：确认你的数据没有被提交

任何时候都可以用这两条命令确认仓库里没有你的数据：

```bash
# 确认 data/ 下文件被忽略（应输出匹配的 gitignore 规则）
git check-ignore -v data/*.zip

# 确认提交历史里只有代码文件（不应出现 .fit / .zip / .db）
git log --all --name-only --pretty=format: | sort -u
```

---

## 快速开始

### 1. 导出你的数据

登录 `connect.garmin.cn` 或 `connect.garmin.com` → 账户设置 → **导出您的数据**。

佳明会通过邮件发送下载链接（通常几分钟到几小时）。压缩包解压后放入 `data/`：

```
data/
└── <导出的数据包>.zip
```

不用手动解压内部结构，程序会递归读取。

### 2. 安装

```bash
git clone https://github.com/hfycium/Garmaple.git
cd Garmaple
pip install -e .
```

### 3. 建索引

```bash
garmaple index
```

首次运行会解析所有 FIT 与 JSON，写入 `cache/`。此目录已被 `.gitignore` 排除。

### 4. 接入 AI 助手

在 `~/.workbuddy/mcp.json`（或对应的 MCP 客户端配置）中加入：

```json
{
  "mcpServers": {
    "garmaple": {
      "command": "garmaple",
      "args": ["serve"],
      "env": {
        "GARMAPLE_DATA": "/path/to/Garmaple/data",
        "GARMAPLE_PRIVACY": "strict"
      }
    }
  }
}
```

### 5. 验证

```
你：我最近一个月跑了多少？
AI：（调用 list_activities / get_period_summary）
```

---

## 数据来源结构

导出的 zip 内部结构（供参考）：

```
<导出包>.zip
├── DI_CONNECT/DI-Connect-Fitness/
│   ├── *_summarizedActivities.json    # 全部活动摘要（主要数据源）
│   ├── *_personalRecord.json          # 个人最佳
│   ├── *_workout.json                 # 训练课表
│   └── *_gear.json                    # 装备里程
├── DI_CONNECT/DI-Connect-Wellness/    # 睡眠分期、生物指标、异常心率
├── DI_CONNECT/DI-Connect-Metrics/     # 训练历史、VO2max、赛事预测
├── DI_CONNECT/DI-Connect-Aggregator/  # 每日汇总（UDS）、饮水记录
└── DI_CONNECT/DI-Connect-Uploaded-Files/
    └── UploadedFiles_*_Part1.zip      # 全部原始 .fit 文件
```

一份约两年的记录，实测规模：活动摘要 300+ 条、原始 FIT 数千个、每日汇总 700+ 天。

---

## 与现有项目的关系

Garmaple 站在它们肩膀上：

| 项目 | 作用 |
|---|---|
| [dtcooper/python-fitparse](https://github.com/dtcooper/python-fitparse) | FIT 格式解析 |
| [fitdecode](https://github.com/polyvertex/fitdecode) | FIT 解析（本项目的选择） |
| [garmin-fit-sdk](https://pypi.org/project/garmin-fit-sdk/) | FIT 生成（课表导出） |
| [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) | 功能设计的参考对象 |

**Garmaple 不是要取代它们。** 如果你有国际区账号、需要实时数据、需要写操作，Taxuspt 的项目更合适，它已经做得非常完整（110+ 工具）。

Garmaple 解决的是另一种情况：中国区账号、不想交出密码、只做历史分析。

## License

[MIT](LICENSE)
