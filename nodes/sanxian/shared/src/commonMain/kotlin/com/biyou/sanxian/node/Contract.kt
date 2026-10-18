package com.biyou.sanxian.node

/** 契约身份常量，与 [docs/node-agent-contract.md] v1.0 保持一致。 */
object Contract {
    const val VERSION = "1.0"
    const val ROLE = "sanxian"
    const val RUNTIME = "llama.cpp"
    const val ARCH = "arm64"

    /** 响应头名（与准圣一致） */
    const val HEADER_NODE_ID = "X-Node-Id"
    const val HEADER_NODE_ROLE = "X-Node-Role"
    const val HEADER_CONTRACT_VERSION = "Contract-Version"
}
