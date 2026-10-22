# Garmaple

把 Garmin 手表的数据变成 AI 可查询、可分析的本地知识库。

## 为什么是"本地数据"路线

佳明中国区账号（`connect.garmin.cn`）与国际区（`connect.garmin.com`）是两套完全隔离的服务器：

- 国际区已开放官方 Personal API（2026-10 重开，免费自助、只读）
- 中国区 `personal.connectapi.garmin.cn` 域名已预留但后端未投产（实测返回 Cloudflare 526）
- 逆向 `sso.garmin.cn` 可行但违反 ToS，有封号风险

因此本项目走第三条路：**使用佳明官方"导出您的数据"功能拿到的完整归档**。这是账号自带的功能，合规、零风险、字段比官方 API 更全（官方 API 会剥离 VO2max、恢复时间、performance condition 等字段）。

## 数据来源

在 `connect.garmin.cn` 账户管理中心发起"导出您的数据"，邮件会收到一个 zip，解压后放入 `data/`：

```
data/
└── <导出的数据包>.zip
    ├── DI_CONNECT/DI-Connect-Fitness/      # 活动摘要、个人最佳、训练计划
    ├── DI_CONNECT/DI-Connect-Wellness/     # 睡眠、生物指标、异常心率
    ├── DI_CONNECT/DI-Connect-Metrics/      # 训练历史、最大摄氧量、赛事预测
    ├── DI_CONNECT/DI-Connect-Aggregator/   # 每日汇总（UDS）、饮水记录
    ├── DI_CONNECT/DI-Connect-Uploaded-Files/
    │   └── UploadedFiles_0-_Part1.zip      # 全部原始 .fit（数千个）
    └── customer_data/                      # 账户资料
```

原始数据**不入版本库**（见 `.gitignore`）。

## 目录约定

| 路径 | 用途 | 是否入库 |
|---|---|---|
| `data/` | 佳明导出原始包 | 否 |
| `cache/` | 解析后的索引（SQLite） | 否 |
| `out/` | 生成的运动课表 `.fit` | 否 |
| `garmaple/` | 主代码 | 是 |
| `tests/` | 测试与合成样本 | 是 |

## 数据安全

`data/` 全程被 `.gitignore` 排除。项目本身不含任何用户数据，也不会上传任何数据——所有解析都在本地完成。

如果你要 fork 或参与开发，请用自己的导出包，**不要提交任何 `.fit` 文件或导出 zip**。运动轨迹包含精确的家庭住址与作息规律，泄露风险远高于一般个人数据。

## 状态

仓库骨架阶段。解析、索引、查询能力待建设。

## License

[MIT](LICENSE)
