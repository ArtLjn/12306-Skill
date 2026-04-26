---
name: train
type: skill
dispatch: train_store
triggers:
  - 火车票
  - 高铁票
  - 查票
  - 买票
  - 火车
  - 高铁
  - 动车
  - 车次
  - 列车
  - 余票
  - 有票吗
  - 直达
  - 中转
  - 换乘
description: 12306 火车票查询助手，支持直达票和中转票查询，包含余票、时刻、席别信息
tools:
  - train_store
---

# 12306 查票 Skill — 主 Agent 参数提取指南

本 Skill 由主 Agent 负责理解用户意图并提取参数，直接传 JSON 调用，无需 LLM 循环。

## 参数 Schema

所有调用通过 `action` 字段指定操作类型：

### query — 直达票查询

```json
{"action": "query", "date": "2026-05-01", "from_station": "北京", "to_station": "上海"}
```
```json
{"action": "query", "date": "明天", "from_station": "广州", "to_station": "深圳", "train_type": "高铁"}
```
- `date` 必填，支持：YYYY-MM-DD、明天、后天、大后天、M月D日
- `from_station` 必填，中文站名（如"北京"、"上海虹桥"）
- `to_station` 必填，中文站名
- `train_type` 可选，过滤车次类型：高铁、动车、直达、特快、快速

### transfer_query — 中转票查询

```json
{"action": "transfer_query", "date": "2026-05-01", "from_station": "北京", "to_station": "苏州北"}
```
```json
{"action": "transfer_query", "date": "后天", "from_station": "睢宁", "to_station": "北京", "middle_station": "徐州"}
```
- 参数同 query，额外支持可选的 `middle_station` 指定中转站
- 不传 `middle_station` 时系统自动推荐中转方案

## 意图 → 参数映射

| 用户说 | action | 参数 |
|--------|--------|------|
| 查一下明天北京到上海的高铁票 | query | {date: "明天", from_station: "北京", to_station: "上海", train_type: "高铁"} |
| 5月1号广州到深圳有票吗 | query | {date: "5月1号", from_station: "广州", to_station: "深圳"} |
| 后天北京到苏州北怎么走 | transfer_query | {date: "后天", from_station: "北京", to_station: "苏州北"} |
| 有没有经过徐州中转的方案 | transfer_query | {date: "...", from_station: "...", to_station: "...", middle_station: "徐州"} |
| 查火车票/查高铁 | query | 根据上下文提取 from/to/date |

## 关键语义区分规则

- 用户只说"查票"没有具体站名 → 追问出发站和到达站
- "直达" → action=query（查直达票）
- "中转/换乘/转车/怎么走" → action=transfer_query
- "高铁/动车" → action=query + train_type 过滤
- 日期提取：识别"明天"、"后天"、"X月X号"、"X月X日"、"XXXX年X月X日"
- 站名映射：模糊匹配（"北京"匹配"北京"/"北京南"/"北京西"等）

## 回复格式

用自然语言回复，禁止 Markdown 表格/代码块/标题/加粗。回复会通过语音播报，直接说人话。

## 限制

- 仅支持查询，不支持购票/抢票
- 查询结果为实时数据，余票随时变化
- 建议用户到 12306 官方渠道购票