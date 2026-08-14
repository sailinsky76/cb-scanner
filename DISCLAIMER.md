# 免责声明 / Disclaimer

## 一、性质

本项目是一个**信息聚合与到期提醒工具**。它做的事是：把若干公开接口的数据取回来、
按规则排序、印成一份日报。它**不产出估值、不预测价格、不给出买卖指令**。

- **不构成投资建议**，不构成任何证券、基金、可转债的要约或要约邀请。
- 作者**不从事、也不具备**证券投资咨询业务资格，本项目**不提供荐股服务**。
- 本项目**不代为交易、不接管账户、不收取任何费用、不收集使用者数据**。
  所有配置与凭证只存在于使用者自己的机器上。

## 二、数据

数据来自第三方公开接口（经 [akshare](https://github.com/akfamily/akshare) 封装的
东方财富、新浪财经、同花顺、巨潮资讯，以及可选的集思录）。

- 作者**不对数据的准确性、完整性、及时性作任何保证**。上游接口随时可能变更字段、
  改变口径或直接失效，这类故障通常表现为「静默地少给」而不是报错。
- 报告中一切数值均为**接口返回值的算术再加工**，不是权威披露。以交易所公告、
  基金合同、券商交易软件的实际显示为准。
- 使用者应遵守各数据源的使用条款，**请勿高频请求**。本项目内置当日缓存与退避重试，
  请不要为了"跑快点"把它们关掉。

## 三、报告里那些数的含义

- **「预估(元)」「可投(万)」**：基于 `config.yaml` 中的假设（本金、单笔占日成交额比例、
  单只仓位上限）做的算术推演，是**乐观值** —— 冷门品种按 5% 日成交额去吃单会打穿多档盘口。
  它不是可实现收益。
- **赎回费**：默认取持有不足 7 日的监管下限，多数基金按此收，但**实际以基金合同费率表为准**。
- **折溢价**：净值口径天然落后价格，盘中尤其。跨境品种结构性落后 1–2 个交易日。
- **场内规模退市线提示**：数据只覆盖深市 LOF，沪市 501/502/505/506 段取不到，
  **不标记不等于安全**；且是单日快照，不是监管口径的"连续 60 个交易日"。
- **事件套利、转债获批**：只做**线索发现**，条款必须人工核对原文公告。
- 本项目对**强赎**与**自然到期**不作区分（数据源把两者放在同一列，样本不足以分开），
  报告一律只说「最后交易日」。这两种情形的处置方向相反，请自行核对。

## 四、责任

本软件按 MIT 许可**「按现状」（AS IS）**提供，不附带任何明示或默示的担保。
任何人因使用、参考或无法使用本软件而产生的任何直接或间接损失（包括但不限于
投资亏损、错过交易时点、数据错误导致的决策失误），作者与贡献者**不承担任何责任**。

**投资有风险，决策请自行核实。**

---

## English (summary)

This project is an information-aggregation and deadline-reminder tool for
publicly disclosed data on Chinese exchange-traded securities. It is **not
investment advice**, not a solicitation, and not a securities advisory service.
The author holds no securities advisory license and provides no stock
recommendations.

Data comes from third-party public endpoints via `akshare`; **no guarantee is
made as to accuracy, completeness, or timeliness**. All figures in the generated
report are arithmetic derivations from those endpoints under user-supplied
assumptions — they are not realizable returns. Verify against exchange filings,
fund prospectuses, and your broker before acting.

Provided **AS IS** under the MIT License. The author and contributors accept no
liability for any loss arising from use of this software.
