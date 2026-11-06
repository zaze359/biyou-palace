"""仙徒节点智能体集合。

每个仙徒（准圣 PC / 散仙安卓）都是**自治双模单元**，对外暴露同一份
HTTP 契约（见 docs/node-agent-contract.md），实现语言可以不同：
- nodes/quasheng  准圣（PC · Python + Ollama）
- nodes/sanxian   散仙（安卓 App · Kotlin + llama.cpp）
"""
