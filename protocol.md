# 设备资料

devices.json 是端点及设备能力，policy.json 是现场已有合同，observations.ndjson 每行是一条合成原始记录。seq 连续递增表示回放顺序，独立于实际发生时间。device 表示事件来源，kind 是类型，refs 是先前观测的序号。

回放端只在回环地址侦听。GET /devices 返回设备表。GET /observations?after=0&limit=16 返回游标之后的记录。POST /intents 以 JSON 接收 device、id 和 action。202 表示回放通道收到命令，实际设备动作以原始观测为依据。

observe 的 shown_g 是当时仪表读数，mode、zero_id、net 表示读数实际受何档与去皮影响，零点有效须与会话及仪表反馈相符。tag 提供称量区间内确证同在台面的嵌套容器组合，wet 或 dry 是现场环境观察。每个称量可以包含初值、秤端已经扣除的皮重、容器变化与旁路接触。transfer 表明实际一批内容变动，bill 是已对外确定的净重和取证范围；两次可比称量按同一内容清点记录连接。舍入只应用正式输出，半步进位，内部不提前丢失精度。
