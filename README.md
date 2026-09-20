# 转诊资料最小交付

基层与县医院按病情和用途交换最小充分的转诊资料包。

`fixtures/packet_manifest.json` 保存一条经过脱敏的业务样例，源代码只定义读取这份样例所需的最小合同。后续模块应保持既有标识和时间含义，新增状态必须说明迁移方式。

## 本地检查

运行 `python -m unittest discover -s tests`。
