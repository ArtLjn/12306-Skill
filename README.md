<p align="center">
  <img src="docs/banner.png" alt="12306 查票 Skill" width="100%">
</p>

<p align="center">
  <strong>智能火车票查询助手</strong> — 直达票 & 中转票 & 智能推荐，实时余票，零外部依赖
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/依赖-零外部库-green.svg" alt="零依赖">
  <img src="https://img.shields.io/badge/API-12306-red.svg" alt="12306 API">
  <img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT License">
</p>

---

## 功能特性

- **直达票查询** — 查询指定日期、出发站到到达站的所有直达车次，包含余票、时刻、席别、票价信息
- **中转票查询** — 当直达票售罄或无直达车次时，智能推荐中转方案，支持指定中转站
- **智能综合查询** — 综合直达、中转、多买几站、先上车后补票四种策略，多维度评分推荐最优方案
- **多买几站** — 直达售罄时自动查找目的地之后的有票站点，买远站提前下车
- **先上车后补票** — 查找目的地之前的有票短途站，上车后补票到目的地
- **价格解析** — 从 12306 原始数据解码各席别票价
- **自然语言日期** — 支持"今天"、"明天"、"后天"、"5月1号"等多种日期表达
- **车次类型过滤** — 按高铁、动车、直达、特快、快速等类型筛选
- **车站智能匹配** — 自动将中文站名映射为电报码，支持在线刷新车站数据
- **零外部依赖** — 仅使用 Python 标准库，自包含单文件部署

## 项目结构

```
train/
├── SKILL.md                 # Skill 配置 & 参数提取指南
├── README.md                # 项目说明
├── docs/
│   └── banner.png           # 项目 Banner
├── data/
│   └── station_cache.json   # 车站名称-电报码缓存
└── scripts/
    └── train_store.py       # 查票工具组（自包含）
```

## 快速开始

### 前置条件

- Python 3.10+

无需安装任何第三方依赖。

### 调用方式

本 Skill 通过主 Agent 的 `dispatch` 机制调用，提供三个工具函数：

#### 直达票查询

```python
# action: query
params = {
    "action": "query",
    "date": "明天",
    "from_station": "北京",
    "to_station": "上海",
    "train_type": "高铁"  # 可选
}
```

#### 中转票查询

```python
# action: transfer_query
params = {
    "action": "transfer_query",
    "date": "2026-05-01",
    "from_station": "北京",
    "to_station": "苏州北",
    "middle_station": "徐州"  # 可选，不传则自动推荐
}
```

### 参数说明

| 参数 | 必填 | 说明 |
|------|------|------|
| `date` | 是 | 出发日期，支持：YYYY-MM-DD、明天、后天、大后天、M月D日 |
| `from_station` | 是 | 出发站中文名称，如"北京"、"上海虹桥" |
| `to_station` | 是 | 到达站中文名称，如"深圳北"、"成都东" |
| `train_type` | 否 | 车次类型过滤：高铁、动车、直达、特快、快速 |
| `middle_station` | 否 | 中转站名称（仅中转查询） |
| `max_alt_stations` | 否 | 多买/少买最多查几站，默认 3（仅智能查询） |

#### 智能综合查询

```python
# action: smart_query
params = {
    "action": "smart_query",
    "date": "明天",
    "from_station": "北京",
    "to_station": "上海"
}
```

### 返回示例

**直达票查询返回：**

```json
{
  "date": "2026-05-01",
  "from": "北京",
  "to": "上海",
  "total": 42,
  "has_ticket_count": 38,
  "sold_out_count": 4,
  "trains": [
    {
      "station_train_code": "G1",
      "train_type": "高铁",
      "start_time": "06:36",
      "arrive_time": "11:36",
      "lishi": "05:00",
      "seats": {"二等座": "有", "一等座": "12", "商务座": "5"},
      "has_ticket": true
    }
  ]
}
```

**中转票查询返回：**

```json
{
  "date": "2026-05-01",
  "from": "北京",
  "to": "苏州北",
  "total": 8,
  "routes": [
    {
      "middle": "徐州东",
      "total_duration": "05:30",
      "wait_time": "00:45",
      "first_train": "G101",
      "first_start": "07:00",
      "first_arrive": "09:15",
      "second_train": "G201",
      "second_start": "10:00",
      "second_arrive": "12:30"
    }
  ]
}
```

## 触发词

在语音对话场景中，以下关键词会触发本 Skill：

`火车票` `高铁票` `查票` `买票` `火车` `高铁` `动车` `车次` `列车` `余票` `有票吗` `直达` `中转` `换乘` `智能查询` `推荐方案` `最优方案` `多买几站` `补票`

## 技术实现

### 架构

- **自包含设计** — 所有逻辑内联于 `scripts/train_store.py`，无需额外模块
- **OpenAI Function Calling** — 工具定义兼容 Function Calling 格式，便于主 Agent 调用
- **线程安全** — 车站数据加载、Cookie 管理均使用线程锁保护
- **自动重试** — API 请求失败时自动重试（最多 3 次），包含 Cookie 刷新机制

### 核心 API

| 数据源 | 接口 | 用途 |
|--------|------|------|
| 12306 | `/otn/leftTicket/query` | 直达票查询 |
| 12306 | `/otn/lcQuery/init` + `/lcquery/queryU` | 中转票查询 |
| 12306 | `/otn/czxx/queryByTrainNo` | 经停站查询（列车途经站） |
| 12306 | `/otn/resources/js/framework/station_name.js` | 车站电报码数据 |

### 席别说明

| 字段 | 席别名称 |
|------|----------|
| `swz_num` | 商务座 |
| `zy_num` | 一等座 |
| `ze_num` | 二等座 |
| `rw_num` | 软卧 |
| `yw_num` | 硬卧 |
| `yz_num` | 硬座 |
| `tz_num` | 特等座 |
| `gr_num` | 高级软卧 |

## 限制

- 仅支持查询，不支持购票/抢票
- 查询结果为实时数据，余票随时变化
- 建议用户到 12306 官方渠道完成购票

## License

MIT License
