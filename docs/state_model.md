# 状态模型与迁移说明

## 1. 清单版本（ManifestVersion）

- 版本号 `revision` 从 1 严格递增；每个版本通过 `parent_hash` 指向上一版本
  的 `version_hash`（首版指向 64 个 0 的根哈希）。哈希在规范化 JSON 上取
  SHA-256，内容一经计算即冻结。
- 任何变化都只能**追加新版本**，变更原因枚举：

  | 原因 | 触发动作 | 说明 |
  | --- | --- | --- |
  | `initial` | 首次推送 | 只允许出现在 revision 1 |
  | `correction` | 更正报告 | 不抹除旧声明，旧版本仍可审计 |
  | `withdrawal` | 撤回非必要附件 | 对应条目出现 `withdrawn=true` 墓碑 |
  | `recheck` | 到院复测 | 来源标注为接收机构，原始声明保留 |
  | `escalation` | 紧急升级 | 风险等级升为 `emergency`，系统不自动扩大资料范围 |

- **迁移方式（新增状态/规则）**：
  - 新增条目编码只能在 `catalog.ItemCode` 追加，不得复用旧编码改含义；
  - 制度规则含义变更时提升 `catalog.RULESET_VERSION`，每个版本快照
    `ruleset_version` 与 `required_codes`，旧清单按生成时规则复核；
  - 新增 `ChangeReason` 必须在服务层的阶段映射表与质控时间线中登记。

## 2. 缺失与冲突提示（Finding）

系统输出提示但**从不替临床人员判断病情、不拦截紧急转诊**：

`missing` / `unavailable` / `partial` / `conflict` / `unauthorized` /
`extra_scope` / `duplicate`。同一物品来源或采集时间不一致报 `conflict`，
由临床人员核实；完全重复声明仅记 `duplicate`。

## 3. 上传会话（UploadSession）

`open → open（分片 0..n-1，可乱序、可重传）→ assembled`；
哈希不符的分片被拒绝且不计入，`session_status` 返回缺失序号用于续传。
中止为 `aborted`（已组装不可中止）。组装产物按内容 SHA-256 寻址，
同文件全库一份。

## 4. 交付（Delivery）

```
delivered ──签收──▶ read            （永久不可变，回执锁定 seen_version_hash）
   │
   ├──新版本推送──▶ superseded      （仅未读交付；已签收不受影响）
   │
   └──超过 SLA 未读──▶ timed_out ──派生──▶ delivered（备份职责，同范围催办）
```

- 资料包条目 = 清单有效条目 ∩ 用途白名单 ∩ 职责可见集 ∩ 已授权条目；
- 超时升级的派生交付带 `scope_ceiling`，范围只能等于或小于原交付，
  违反即中止（`RuleViolation`）；**不会**自动扩大用途或条目范围；
- 封装头绑定 `purpose` 与接收职责密钥 id，用途不符 / 非接收职责 /
  密钥版本不符一律拒绝解封。

## 5. 旅程幂等（ReferralJourney）

业务键 `referral_key` 决定旅程唯一性；`idempotency_key` 决定推送幂等。
重复推送命中幂等键时返回 `deduped=true` 的首次结果，不产生新版本，
更不会产生第二条旅程。业务键绑定患者后不可更换患者。

## 6. 访问审计与质控边界

- 所有访问尝试（含拒绝）只追加 `AccessEvent`；患者可查
  「谁、因何用途、访问了哪些字段」；
- 质控人员仅对被 `grant_qc_scope` 指派的转诊有访问权，范围外访问被拒绝
  并留痕；质控视图是无正文的审计投影，缺项按
  「缺失首现 revision → 补齐 revision / 变更环节」定位。
