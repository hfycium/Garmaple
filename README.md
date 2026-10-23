# Garmaple

**用你自己的佳明历史数据，直接向 AI 问训练问题。**

Garmaple 读取你从 Garmin 导出的数据，在本机整理后，通过 MCP 提供给 AI 助手查询。无需把 Garmin 账号密码交给 Garmaple，也不需要实时连接 Garmin。

## 你可以用它做什么

- **回顾训练：**查看一段时间跑了多少、训练量和配速怎样变化，比较不同日期或运动类型。
- **分析单次活动：**查看配速、心率、爬升、步频和 FIT 逐秒曲线；评估稳态跑的配速与心率漂移。
- **结合恢复情况：**把睡眠、压力、身体电量等记录与跑步表现放在一起，查看它们是否存在统计关联。
- **了解长期变化：**查看训练状态、最大摄氧量、比赛成绩预测和个人最佳。
- **管理跑步装备：**按关联活动估算装备里程。
- **准备一堂训练：**生成 FIT 课表文件，保存到本机后由你手动拷入手表。

你可以直接这样问 AI：

- 「最近几周训练量和配速有什么变化？」
- 「睡眠和压力与我第二天的跑步表现有没有统计上的关联？」
- 「这次跑步后半程的心率和配速是否出现明显漂移？」
- 「我的比赛预测和真实个人最佳分别是什么？」
- 「帮我生成一份包含热身、间歇和放松的时间课表。」

Garmaple 会说明数据覆盖范围和缺失项。相关性只是统计关系，不代表因果；AI 只能查询最近一次导出的数据，不能看到尚未导出的新活动。

## 开始使用

### 1. 导出佳明数据

打开对应的 Garmin 数据管理页面并登录账号，选择「导出您的数据」并提交导出请求。Garmin 准备好后会通过邮件发送下载链接；收到邮件后下载 `.zip` 压缩包。官方说明通常约需 48 小时，最长可能需要 30 天。详见 [Garmin 官方导出说明](https://support.garmin.com/en-US/?faq=W1TvTPW8JZ6LfJSfK512Q8)。

- 中国区：[Garmin 数据导出](https://www.garmin.cn/zh-CN/account/datamanagement/exportdata)
- 国际区：[Garmin 数据导出](https://www.garmin.com/account/datamanagement/exportdata)

### 2. 安装并放入导出包

需要 Python 3.10 或更新版本。先把项目下载到本机，然后在终端进入项目目录并安装：

```bash
git clone https://github.com/hfycium/Garmaple.git
cd Garmaple
pip install -e .
```

在项目目录里找到或新建 `data/` 文件夹，用文件管理器把刚下载的 `.zip` 压缩包复制进去。不要解压。然后仍在 Garmaple 项目目录的终端里运行：

```bash
garmaple index
garmaple doctor
```

目录放好后大致如下；压缩包名称可以不同，也可以把多个导出压缩包都放在 `data/` 里：

```text
Garmaple/
├── data/
│   └── Garmin 导出.zip
├── cache/                  # garmaple index 自动创建
│   └── garmaple.sqlite3    # 本地索引
└── README.md
```

`garmaple index` 会读取 `data/` 里的压缩包并建立本地索引，`garmaple doctor` 会检查是否可用。数据更新时，把新下载的压缩包也放进 `data/`，再次运行 `garmaple index`。原始压缩包不用解压或改名。

### 3. 连接 AI 助手

在支持 MCP 的 AI 客户端设置中添加 Garmaple 服务。把以下配置合并到客户端已有的 MCP 服务列表中，**不要覆盖其他服务**：

```json
{
  "mcpServers": {
    "garmaple": {
      "command": "garmaple",
      "args": ["serve"],
      "env": {
        "GARMAPLE_DATA": "C:/Users/你的用户名/Garmaple/data",
        "GARMAPLE_CACHE": "C:/Users/你的用户名/Garmaple/cache",
        "GARMAPLE_PRIVACY": "strict"
      }
    }
  }
}
```

将 `GARMAPLE_DATA` 和 `GARMAPLE_CACHE` 改成你本机对应文件夹的**完整路径**，不是 zip 文件本身。`GARMAPLE_CACHE` 必须指向运行 `garmaple index` 时使用的同一个缓存目录；按上面的步骤在项目目录运行命令时，就是项目里的 `cache/`。上例是 Windows 路径；macOS 或 Linux 用户填对应的本机路径。不同 AI 客户端的配置位置不同；保存后启用 Garmaple，并按客户端提示重启或确认连接。

连接成功后，直接在 AI 对话里用平常说话的方式提问，不需要输入命令或记住工具名称。可以先问「我的佳明数据覆盖到哪一天？」确认连通，再问「最近一个月跑了多少」「比较最近四周的训练量和配速」「睡眠和跑步表现有没有统计上的关联」。AI 会根据问题读取 Garmaple 数据；如果客户端没有显示 Garmaple 工具，先确认 `garmaple doctor` 检查通过，再重启 AI 客户端并检查 MCP 服务是否已启用。

## 隐私设置

Garmaple 在本机读取导出文件，不登录 Garmin、不向 Garmaple 服务发送数据。**连接 AI 后，工具返回的内容会进入 AI 对话上下文，并可能发送给该 AI 服务的模型提供方。**隐私设置控制 Garmaple 返回哪些信息：

| 设置 | 返回内容 |
|---|---|
| `strict`（默认） | 不返回 GPS 坐标和结构化地点字段；活动只显示日期，活动 ID 使用伪 ID。活动名、装备名等自由文本仍可能包含地点信息 |
| `normal` | 不返回 GPS 坐标；保留精确时间和原始活动 ID |
| `raw` | 返回原始位置与标识信息；只有确实需要时才开启 |

如果不确定，保留默认的 `strict`。修改设置时，将配置中的 `GARMAPLE_PRIVACY` 改成对应值。

**本地文件也要妥善保管：**隐私设置只控制 MCP 返回给 AI 的内容，不会对原始导出包或本地索引进行脱敏。`data/` 和 `cache/` 中仍可能包含 GPS 坐标、地点及原始活动标识等敏感信息；请勿将它们提交到公开仓库或分享给他人。

## 使用前了解

- **这是历史数据工具。** Garmin 导出完成之后的新活动不会自动出现；需要再次导出并运行 `garmaple index`。
- **设备记录什么，分析才能用什么。** 例如功率曲线需要活动中有功率记录；没有记录时工具会说明不可用，不会把缺失当成零。
- **部分数据无法从导出包获得。** Garmin 导出数据不包含睡眠评分数值，因此 Garmaple 不提供睡眠评分。
- **相关不等于因果。** 睡眠较好与配速较快同时出现，不足以证明睡眠变化导致了配速变化。
- **课表导入尚未实机验证。** 课表文件目前支持按时间分段，生成后需要手动拷入手表。作者使用较早的 Garmin 245，无法验证所有设备的数据与导入表现；欢迎使用其他设备的用户反馈兼容情况和导入结果。

## License

[MIT](LICENSE)
