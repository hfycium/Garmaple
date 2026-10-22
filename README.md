# Garmaple

**不用把佳明账号密码交给任何第三方，让 AI 分析你的训练数据。**

Garmaple 读取佳明官方「导出您的数据」归档包，在本地建立索引，通过 MCP 把分析能力暴露给 AI 助手。全程不联网、不登录、不逆向。

---

## 为什么需要这个项目

### 在线方案的三点取舍

Garmin 没有官方 MCP。目前能用 AI 读佳明数据的项目，普遍走同一条路：请求你输入账号密码，换一个 token，再拿 token 去调 Garmin Connect 的接口。

这条路能跑通，只是有三点代价：

- **凭据要交出去。** 密码或 token 一旦泄露就等于账号泄露。
- **依赖服务端接口。** Garmin 调整一次接口，工具就失效一次，得等作者适配。
- **中国区适配不完整。** 见下一节。

### 中国区与在线方案的天然错位

`connect.garmin.cn`（中国区）与 `connect.garmin.com`（国际区）是两套完全隔离的服务器，账号不互通、数据不互通。

在线方案必须先登录才能拿到数据，而登录流程涉及 SSO 域名、OAuth token 端点、mobile flow 的一系列域名映射，中国区与全球的域名并不一致（`diauth.garmin.cn` 与 `diauth.garmin.com`、`connectapi.garmin.cn` 与 `connectapi.garmin.com` 等）。任何一处没有做适配，中国区账号就会在这一步失败。

这是在线架构的固有成本：**每增加一个区域，就多一套要逐一适配的域名。**

### Garmaple 的做法：不碰认证

佳明自带「导出您的数据」功能，邮件会发来一份完整归档，里面有全部原始 `.fit` 文件与结构化 JSON。这是账号自带的能力，**不需要开发者资格、不需要密码外传、不受接口改版影响**。

Garmaple 就只读这份归档。

| | 在线登录型工具 | Garmaple |
|---|---|---|
| 数据从哪来 | 佳明服务器（需登录） | 你自己导出的归档 |
| 认证 | 账号密码 / MFA / token | **不需要任何登录** |
| 中国区 | 取决于各项目对域名的适配程度 | **导出功能本身无区限制** |
| 接口改版 | 跟随服务端调整 | **不依赖接口** |
| 字段完整度 | 服务端愿意返回的字段 | **原始 FIT 全字段在手** |
| 实时性 | 实时 | **需重新导出**（这是唯一代价） |

### 代价说明

Garmaple **不是实时工具**。数据新鲜度取决于你上次导出的时间。

如果你的需求是「问 AI 我今天早上的跑量」——现在这个项目给不了。如果你要的是「分析我这两年的训练规律」——离线归档反而更合适，因为它是全量历史，不是分页拉取。

---

## 你会得到什么

功能分三档，按实用性排序。**已实现的打勾**。

### 第一档：数据地基

- [x] `get_data_overview` — 数据覆盖范围、运动类型、索引时间
- [x] `list_activities` — 按日期区间、运动类型筛选活动
- [x] `get_activity` — 单次活动的完整指标（距离、时长、配速、心率、爬升、训练效果）
- [x] `get_period_summary` — 区间训练量汇总（含加权配速、最快 / 最长单次）
- [ ] `get_daily_summary` — 每日步数、静息心率、压力、身体电量、强度分钟
- [ ] `get_sleep` — 睡眠时长、分期、评分

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

## 快速开始

### 方式一：让 AI 帮你装（不用碰命令行）

你只需要做两步。

**第一步，导出数据。**

打开下面的地址，登录，点「导出您的数据」，等佳明的邮件（几分钟到几小时不等），下载压缩包：

```
https://www.garmin.cn/zh-CN/account/datamanagement/exportdata
```

国际区账号用这个地址：

```
https://www.garmin.com/account/datamanagement/exportdata
```

**第二步，把下面这段话复制给你的 AI 助手**（WorkBuddy、Claude、Cursor、Codex 等都可以），只改一处——把最后那行换成你下载的压缩包路径：

```text
帮我安装 Garmaple 并接入你，之后我就能直接问你我的佳明训练数据。

请按顺序执行，遇到问题自己解决或直接告诉我该运行什么命令，不要让我去读文档：

1. 检查本机 Python 版本，需要 3.10 或以上；不满足就帮我装一个。
2. 把 https://github.com/hfycium/Garmaple 克隆到本地一个合适的目录
   （不要放进已有的 git 仓库里，避免和别人的版本控制混在一起）。
3. 进入该目录，执行 pip install -e . 安装。
4. 我有一个佳明官方导出的数据包，请把它放到项目的 data/ 目录下：
   <把压缩包路径填在这里>
5. 执行 garmaple index 建立本地索引。
6. 执行 garmaple doctor 自检，把输出结果完整告诉我。
7. 把 garmaple serve 注册为你的 MCP 服务：
   - 先读一遍现有的 MCP 配置文件，保留里面已有的服务，只做合并，不要覆盖
   - 命令用 garmaple，参数 serve
   - 环境变量 GARMAPLE_DATA 指向该项目的 data 目录
   - 环境变量 GARMAPLE_PRIVACY 设为 strict（默认的最保守脱敏级别）
8. 装好后用 get_data_overview 试一次，确认能读到我自己的数据。
```

之后你就能直接问它「我上个月跑了多少」「最近三个月训练量趋势怎么样」这类问题。

**关于隐私级别**：`strict` 是默认值，不会把 GPS 坐标、精确时间、原始活动 ID 交给 AI。如果你希望 AI 看到更细的时间信息，可以改成 `normal`；`raw` 会输出全部字段，一般不建议。详见文末的安全章节。

### 方式二：手动安装

```bash
git clone https://github.com/hfycium/Garmaple.git
cd Garmaple
pip install -e .

# 把导出包放进 data/
garmaple index     # 建索引，写入 cache/（已被 .gitignore 排除）
garmaple doctor    # 自检：数据包、分片数量、索引状态、隐私级别
garmaple serve     # 启动 MCP 服务
```

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

写完配置后，多数客户端需要在连接器 / MCP 管理界面手动启用或确认信任一次。

### 验证

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

归档里有多少活动、覆盖多长时间，取决于你手表记录了多少年——导出包里有什么，工具就读什么。

---

## 与现有项目的关系

Garmaple 站在它们肩膀上：

| 项目 | 作用 |
|---|---|
| [fitdecode](https://github.com/polyvertex/fitdecode) | FIT 解析（本项目的选择） |
| [dtcooper/python-fitparse](https://github.com/dtcooper/python-fitparse) | 另一套成熟的 FIT 解析实现 |
| [garmin-fit-sdk](https://pypi.org/project/garmin-fit-sdk/) | FIT 生成（课表导出） |
| [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) | 功能设计的参考对象 |

在线方案和离线方案解决的不是同一个问题。如果你有国际区账号、需要实时数据、需要写操作，那些项目更合适，它们在这条路上已经走了很远。

Garmaple 面向的是另一种情况：中国区账号、不想交出密码、只做历史分析。

---

## 安全

### 工具本身做什么

**只读本地文件、不联网、不上传、不调用任何远端接口。** 没有遥测、没有统计上报、没有云端同步。

### 但「工具不联网」不等于「数据不出本机」

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
| Garmin activity ID | 不外露，内部映射为伪 ID | 可反查公开活动页 |
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

## License

[MIT](LICENSE)
