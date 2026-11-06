#include <jni.h>

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include <android/log.h>

#include "llama.h"

#define LOG_TAG "llama_jni"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN, LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

// llama/ggml 的内部日志（含 LLAMA_LOG_ERROR，例如「failed to find a memory slot」、
// 「could not reserve space」、compute 失败等）默认只写 stderr，在 Android 上不会进 logcat，
// 导致 decode 失败时只能看到上层「prompt eval 失败」和折叠后的 -4，根因完全不可见。
// 这里把整套日志桥接到 __android_log_print，真机 logcat 即可看到真实错误。
static void sanxian_ggml_log(ggml_log_level level, const char* text, void* /*user_data*/) {
    if (!text) return;
    int prio;
    switch (level) {
        case GGML_LOG_LEVEL_ERROR: prio = ANDROID_LOG_ERROR; break;
        case GGML_LOG_LEVEL_WARN:  prio = ANDROID_LOG_WARN;  break;
        case GGML_LOG_LEVEL_INFO:  prio = ANDROID_LOG_INFO;  break;
        case GGML_LOG_LEVEL_DEBUG: prio = ANDROID_LOG_DEBUG; break;
        default:                   prio = ANDROID_LOG_INFO;  break;
    }
    __android_log_print(prio, "llama", "%s", text);
}

static std::atomic<bool> g_llama_log_bridged{false};
static void bridgeLlamaLogsOnce() {
    if (!g_llama_log_bridged.exchange(true)) {
        // llama_log_set 会同时接管 llama 与 ggml 两套回调，一处设置即可。
        llama_log_set(sanxian_ggml_log, nullptr);
    }
}

namespace {

// 一次模型加载对应的原生会话，Kotlin 侧持有其指针（jlong handle）。
struct LlmSession {
    llama_model* model = nullptr;
    llama_context* ctx = nullptr;
    const llama_vocab* vocab = nullptr;
    llama_sampler* sampler = nullptr;

    // D4：采样参数单一数据源（rebuildSampler 据此重建采样链）
    float   temperature   = 0.7f;  // 采样温度
    float   topP          = 0.9f;  // top-p
    int32_t topK          = 40;    // top-k；<=0 表示禁用
    float   repeatPenalty = 1.1f;  // 重复惩罚（penalty_repeat）；<=1.0 关闭
    int32_t repeatLastN   = 64;    // 重复惩罚回溯窗口（penalty_last_n）
    int64_t seed          = 0;     // 采样种子；>0 决定论，<=0 每次生成随机

    std::atomic<bool> cancelled{false};

    int32_t n_ctx = 0;      // 上下文窗口大小
    int32_t n_batch = 512;  // 单次 eval 的批大小
    int32_t n_past = 0;     // KV Cache 中已填充的 token 数（多轮对话复用）

    // 一轮 generate 会跑多次「渲染 + 分词」探针，模板缺失的告警只打一次，免得刷屏把裁剪告警冲掉
    bool loggedTemplateFallback = false;

    // 上一轮送入 KV 的完整 token 序列快照（prompt + 生成的 assistant token），
    // 用于与本轮 prompt 求最长公共前缀，决定增量复用多少 KV。
    std::vector<llama_token> cachedTokens;
    // 缓存 token 与 KV 是否一致；清空 / 取消 / 异常后置 false，下一轮强制全量重算
    bool kvValid = false;
};

LlmSession* asSession(jlong handle) {
    return reinterpret_cast<LlmSession*>(handle);
}

// D4：采样扩展开关。置 0 时 rebuildSampler 跳过 top-k 与 penalties，
// 采样链退化为 min_p -> temp -> top_p -> dist（等价 D3 之前行为），用于回归对照 / 快速回滚。
#ifndef SANXIAN_SAMPLING_EXTENDED
#define SANXIAN_SAMPLING_EXTENDED 1
#endif
constexpr bool kSamplingExtended = (SANXIAN_SAMPLING_EXTENDED != 0);

// 以会话保存的采样参数重建整条采样链。每轮 generate 调用一次（每用户消息一次），
// 成本远小于 llama_decode 评估；同时规避 dist reset 的 RNG 语义歧义：
//   - seed > 0：以固定 seed 构造 dist -> 决定论复现
//   - seed <= 0：以 fresh std::rand() 构造 dist -> 每轮随机
void rebuildSampler(LlmSession* s) {
    if (s->sampler) {
        llama_sampler_free(s->sampler);
        s->sampler = nullptr;
    }
    llama_sampler_chain_params sparams = llama_sampler_chain_default_params();
    s->sampler = llama_sampler_chain_init(sparams);

    // 1) 重复惩罚（logit 处理器）：须先于 temp/top_k/top_p 作用于全词表 logits
    if (kSamplingExtended && s->repeatPenalty > 1.0f) {
        llama_sampler_chain_add(s->sampler, llama_sampler_init_penalties(
                llama_vocab_n_tokens(s->vocab),  // n_vocab
                s->repeatLastN,                  // penalty_last_n
                s->repeatPenalty,                // penalty_repeat
                0.0f, 0.0f));                    // frequency / presence 禁用
    }
    // 2) min_p（保持现状）
    llama_sampler_chain_add(s->sampler, llama_sampler_init_min_p(0.05f, 1));
    // 3) 温度
    llama_sampler_chain_add(s->sampler, llama_sampler_init_temp(s->temperature));
    // 4) top-k（可关闭）
    if (kSamplingExtended && s->topK > 0) {
        llama_sampler_chain_add(s->sampler, llama_sampler_init_top_k(s->topK));
    }
    // 5) top-p
    llama_sampler_chain_add(s->sampler, llama_sampler_init_top_p(s->topP, 1));
    // 6) dist 收尾（决定最终 token 选定）
    const uint32_t effSeed = (s->seed > 0)
            ? static_cast<uint32_t>(s->seed)
            : static_cast<uint32_t>(std::rand());
    llama_sampler_chain_add(s->sampler, llama_sampler_init_dist(effSeed));

    LOGI("采样链重建: top_k=%s repeat_penalty=%.2f seed=%s(%u)",
         (kSamplingExtended && s->topK > 0) ? std::to_string(s->topK).c_str() : "off",
         s->repeatPenalty,
         (s->seed > 0) ? "fixed" : "rand",
         effSeed);
}

// 把 tokens[from, from+n) 作为一个 batch 送进 llama_decode。
// needLogits 表示最后一个 token 需要保留 logits 以便采样。
int32_t decodeChunk(LlmSession* s, const std::vector<llama_token>& tokens,
                    size_t from, int32_t n, bool needLogits) {
    llama_batch batch = llama_batch_init(n, 0, 1);
    // llama_batch_init 只分配数组、把 n_tokens 显式置 0，必须调用方自己填 token 数；
    // 否则 llama_decode 看到 n_tokens==0 直接返回 -1（表现为上层折叠的 native code -4）。
    batch.n_tokens = n;
    for (int32_t j = 0; j < n; j++) {
        batch.token[j] = tokens[from + j];
        batch.pos[j] = s->n_past + j;
        batch.n_seq_id[j] = 1;
        batch.seq_id[j][0] = 0;
        batch.logits[j] = 0;
    }
    if (needLogits) batch.logits[n - 1] = 1;

    int32_t rc = llama_decode(s->ctx, batch);
    llama_batch_free(batch);
    return rc;
}

// 两 token 序列的最长公共前缀长度（多轮 KV 增量复用）。任一为空 → 0。
static size_t longestCommonPrefix(const std::vector<llama_token>& a,
                                  const std::vector<llama_token>& b) {
    const size_t n = std::min(a.size(), b.size());
    size_t i = 0;
    while (i < n && a[i] == b[i]) i++;
    return i;
}

// ---------------------------------------------------------------------------
// 上下文预算与消息级裁剪
//
// prompt 超出 n_ctx 时必须丢弃「最早的历史」而不是「最新的提问」。关键在于顺序：
// 裁剪必须发生在 chat template 渲染**之前** —— 渲染后的 token 流里，
// `<|im_start|>` 这类标记会横跨多个 token，按 token 裸切会产出结构非法的 prompt。
//
// 但「裁到哪」又得知道渲染后的 token 数，这是个自指问题。解法是把
// 「渲染 + 分词 + 判预算」打包成探针 probeBudget()，只用它做是/否判断，
// 命中时顺带把渲染文本与 token 序列带回，供最终 eval 直接复用（不重复分词）。
// ---------------------------------------------------------------------------

// 为生成预留的安全余量：模板收尾标记 + 位置对齐（见 PRD 验收 n_ctx−maxTokens−4）
constexpr int32_t kSafetyMargin = 4;

// 增量 KV 复用常量（见 09-28-multi-turn-kv-reuse/design.md §5）
constexpr int32_t kSafetyRewind = 4;      // BPE 边界退让：重算末几个 token 换取确定性
constexpr size_t  kMinReuseTokens = 32;   // 复用收益低于此阈值时直接全量重算

// 增量 KV 复用总开关：#define SANXIAN_KV_INCREMENTAL 0（或 -D...=0）退回改动前
// 「每轮 clear + 全量重算」行为，用于 A/B 与故障回退。
#ifndef SANXIAN_KV_INCREMENTAL
#define SANXIAN_KV_INCREMENTAL 1
#endif
constexpr bool kEnableIncrementalKv = (SANXIAN_KV_INCREMENTAL != 0);

// 一条会话消息（原生持有字符串存储，供 llama_chat_message 借用指针）
struct ChatMsg {
    std::string role;
    std::string content;
};

/**
 * 渲染「可选 system + msgs[start, end)」这段对话为 prompt。
 *
 * 模板取自模型 GGUF 内嵌的 tokenizer.chat_template（llama_model_chat_template）；
 * 模型未内嵌、或模板不在 llama.cpp 预置模板族内导致渲染失败时，退化为 ChatML 兜底。
 *
 * 取非 const 会话指针仅为记录「兜底告警已打过」——一轮 generate 会跑十来个探针，
 * 每探针都打一条 ERROR 会把真正重要的裁剪告警冲掉。
 */
bool renderChat(LlmSession* s, const std::vector<ChatMsg>& msgs,
                bool keepSystem, size_t start, std::string* out) {
    // 只有 msgs[0] 确实是 system 时才把它单独取出，否则 keepSystem 无意义
    const bool takeSystem = keepSystem && !msgs.empty() && msgs[0].role == "system";
    // takeSystem 时下标 0 已被取出，循环必须从 1 起，否则 system 会被渲染两次
    const size_t from = takeSystem ? std::max(start, static_cast<size_t>(1)) : start;

    std::vector<llama_chat_message> raw;
    raw.reserve(msgs.size() - std::min(from, msgs.size()) + 1);
    if (takeSystem) {
        raw.push_back(llama_chat_message{msgs[0].role.c_str(), msgs[0].content.c_str()});
    }
    for (size_t i = from; i < msgs.size(); i++) {
        raw.push_back(llama_chat_message{msgs[i].role.c_str(), msgs[i].content.c_str()});
    }
    if (raw.empty()) return false;

    const char* tmpl = (s->model) ? llama_model_chat_template(s->model, nullptr) : nullptr;
    if (tmpl != nullptr && tmpl[0] != '\0') {
        size_t total = 64;
        for (const auto& m : raw) {
            total += std::strlen(m.role) + std::strlen(m.content) + 16;
        }
        std::vector<char> buf(total * 2 + 256);
        int32_t res = llama_chat_apply_template(tmpl, raw.data(), raw.size(), true,
                                                buf.data(), static_cast<int32_t>(buf.size()));
        if (res > static_cast<int32_t>(buf.size())) {
            buf.resize(static_cast<size_t>(res) + 1);
            res = llama_chat_apply_template(tmpl, raw.data(), raw.size(), true,
                                            buf.data(), static_cast<int32_t>(buf.size()));
        }
        if (res > 0) {
            out->assign(buf.data(), static_cast<size_t>(res));
            return true;
        }
        if (!s->loggedTemplateFallback) {
            s->loggedTemplateFallback = true;
            LOGE("模型 chat template 不在此 llama.cpp 版本的预置模板族内，改用 ChatML 兜底");
        }
    } else if (!s->loggedTemplateFallback) {
        s->loggedTemplateFallback = true;
        LOGE("模型未内嵌 chat template，改用 ChatML 兜底");
    }

    // ChatML 兜底：与旧 Kotlin 侧 buildChatML 逐字节一致
    out->clear();
    for (const auto& m : raw) {
        out->append("<|im_start|>");
        out->append(m.role);
        out->append("\n");
        out->append(m.content);
        out->append("<|im_end|>\n");
    }
    out->append("<|im_start|>assistant\n");
    return true;
}

/** prompt 文本 → token 序列（负返回值表示缓冲不足，按其绝对值扩容重试）。失败时清空 out。 */
bool tokenizePrompt(const llama_vocab* vocab, const std::string& text,
                    std::vector<llama_token>* out) {
    out->resize(text.size() + 64);
    int32_t n = llama_tokenize(vocab, text.c_str(), static_cast<int32_t>(text.size()),
                               out->data(), static_cast<int32_t>(out->size()),
                               /*add_special=*/true, /*parse_special=*/true);
    if (n < 0) {
        out->resize(static_cast<size_t>(-n));
        n = llama_tokenize(vocab, text.c_str(), static_cast<int32_t>(text.size()),
                           out->data(), static_cast<int32_t>(out->size()), true, true);
    }
    if (n <= 0) {
        // 必须清空：否则调用方拿到的是「已 resize 但内容未初始化」的垃圾序列，
        // 会污染按 tokens.size() 做判断的逻辑（如裁剪日志里的原始 token 数）
        out->clear();
        return false;
    }
    out->resize(static_cast<size_t>(n));
    return true;
}

/**
 * 剥离尾部不完整的 UTF-8 序列。
 *
 * BBPE 词表里一个 token 可能只是某个多字节字符的前半（汉字的第 1、2 个字节），
 * 截断 token 序列就可能切在字符中间。残缺字节被重新渲染进 prompt 后，llama.cpp
 * 只能按字节回退成 `<0xNN>` 垃圾 token —— 正是要避免的「乱码」。
 */
void trimIncompleteUtf8(std::string* text) {
    while (!text->empty()) {
        // 从尾部往前跳过 UTF-8 续字节（0b10xxxxxx），定位最后一个字符的首字节
        size_t start = text->size();
        while (start > 0 && (static_cast<unsigned char>((*text)[start - 1]) & 0xC0) == 0x80) start--;
        if (start == 0) {  // 整串都是续字节，无法恢复
            text->clear();
            return;
        }
        unsigned char lead = static_cast<unsigned char>((*text)[start - 1]);
        size_t need = (lead & 0x80) == 0x00   ? 1
                    : (lead & 0xE0) == 0xC0   ? 2
                    : (lead & 0xF0) == 0xE0   ? 3
                    : (lead & 0xF8) == 0xF0   ? 4
                                              : 1;  // 非法首字节：当作单字节放过，不再纠缠
        if (text->size() - (start - 1) >= need) return;  // 该字符完整
        text->resize(start - 1);                         // 截掉残缺序列，继续往前检查
    }
}

/** token 序列 → 文本。用于把截断后的消息内容还原成可再次渲染的字符串。 */
std::string detokenize(const llama_vocab* vocab, const std::vector<llama_token>& toks) {
    std::string text;
    for (llama_token tok : toks) {
        char buf[256];
        int32_t n = llama_token_to_piece(vocab, tok, buf, sizeof(buf), 0, /*special=*/false);
        if (n >= 0) {
            text.append(buf, static_cast<size_t>(n));
            continue;
        }
        std::vector<char> big(static_cast<size_t>(-n));
        int32_t m = llama_token_to_piece(vocab, tok, big.data(),
                                         static_cast<int32_t>(big.size()), 0, false);
        if (m > 0) text.append(big.data(), static_cast<size_t>(m));
    }
    trimIncompleteUtf8(&text);
    return text;
}

/**
 * 预算探针：渲染 (keepSystem, start) 对应的窗口并分词，判断是否 ≤ budget。
 * 命中时把 prompt / tokens 一并带回，调用方直接拿去 eval，不必重新渲染或分词。
 */
bool probeBudget(LlmSession* s, const std::vector<ChatMsg>& msgs,
                 bool keepSystem, size_t start, int32_t budget,
                 std::string* prompt, std::vector<llama_token>* tokens) {
    if (start >= msgs.size()) return false;
    if (!renderChat(s, msgs, keepSystem, start, prompt)) return false;
    if (!tokenizePrompt(s->vocab, *prompt, tokens) || tokens->empty()) return false;
    return static_cast<int32_t>(tokens->size()) <= budget;
}

/**
 * 按 token 收缩 msgs[idx].content，直到窗口 (keepSystem, start) 落入预算。
 *
 * 每次砍掉约 1/4 token（几何收敛）。这是**唯一**允许切开单条消息内部的路径，
 * 且只对 system prompt 使用（PRD 明确要求 system 必须保留，超预算时截断并告警）；
 * 用户消息不得走这里 —— 截断用户提问会违反 PRD「最新一条用户消息完整存在」的硬验收。
 *
 * @return 最终是否落入预算；true 时 prompt / tokens 为该次成功探针的结果。
 *         false 时 msgs[idx].content 已恢复原值（避免把砍剩的残串泄漏给后续分支）
 */
bool shrinkMessageToFit(LlmSession* s, std::vector<ChatMsg>* msgs, size_t idx,
                        bool keepSystem, size_t start, int32_t budget,
                        std::string* prompt, std::vector<llama_token>* tokens) {
    std::vector<llama_token> toks;
    if (!tokenizePrompt(s->vocab, (*msgs)[idx].content, &toks) || toks.empty()) return false;

    const std::string original = (*msgs)[idx].content;
    while (true) {
        if (probeBudget(s, *msgs, keepSystem, start, budget, prompt, tokens)) return true;
        if (toks.size() <= 1) {
            (*msgs)[idx].content = original;  // 失败必须回滚，否则就地改坏的内容会污染后续分支
            return false;
        }
        toks.resize(toks.size() - std::max<size_t>(1, toks.size() / 4));
        (*msgs)[idx].content = detokenize(s->vocab, toks);
    }
}

/**
 * 把 JNI 的 roles[] / contents[] 平行数组拷进 native 的 ChatMsg 列表。
 * 逐项释放字符串与局部引用 —— 历史很长时局部引用表会溢出。
 *
 * @return false 表示数组为空或长度非法
 */
bool readChatArrays(JNIEnv* env, jobjectArray roles, jobjectArray contents,
                    std::vector<ChatMsg>* out) {
    jsize n = roles ? env->GetArrayLength(roles) : 0;
    jsize nContent = contents ? env->GetArrayLength(contents) : 0;
    if (n <= 0) return false;
    if (nContent < n) n = nContent;

    out->clear();
    out->resize(static_cast<size_t>(n));
    for (jsize i = 0; i < n; i++) {
        jstring jr = static_cast<jstring>(env->GetObjectArrayElement(roles, i));
        jstring jc = static_cast<jstring>(env->GetObjectArrayElement(contents, i));
        const char* rc = jr ? env->GetStringUTFChars(jr, nullptr) : nullptr;
        const char* cc = jc ? env->GetStringUTFChars(jc, nullptr) : nullptr;
        // GetStringUTFChars 在 OOM 时会抛异常并返回 null。不在此处拦下，异常会一路拖到
        // 后面的 CallBooleanMethod 才显形，现场就丢了。清掉已获取的资源后上报失败。
        if (env->ExceptionCheck()) {
            if (rc) env->ReleaseStringUTFChars(jr, rc);
            if (cc) env->ReleaseStringUTFChars(jc, cc);
            if (jr) env->DeleteLocalRef(jr);
            if (jc) env->DeleteLocalRef(jc);
            return false;
        }
        (*out)[i].role = rc ? rc : "";
        (*out)[i].content = cc ? cc : "";
        if (rc) env->ReleaseStringUTFChars(jr, rc);
        if (cc) env->ReleaseStringUTFChars(jc, cc);
        if (jr) env->DeleteLocalRef(jr);
        if (jc) env->DeleteLocalRef(jc);
    }
    return true;
}

}  // namespace

extern "C" JNIEXPORT jstring JNICALL
Java_com_biyou_sanxian_llm_LlamaNative_hello(JNIEnv* env, jclass /*clazz*/) {
    llama_backend_init();

    std::string info = "llama.cpp linked";
    info += " | mmap=";
    info += llama_supports_mmap() ? "yes" : "no";
    info += " | mlock=";
    info += llama_supports_mlock() ? "yes" : "no";
    info += " | gpu_offload=";
    info += llama_supports_gpu_offload() ? "yes" : "no";

    return env->NewStringUTF(info.c_str());
}

extern "C" JNIEXPORT jlong JNICALL
Java_com_biyou_sanxian_llm_LlamaNative_loadModel(
        JNIEnv* env, jclass /*clazz*/,
        jstring jPath, jint nCtx, jint nThreads, jint nBatch, jint nGpuLayers,
        jboolean useMmap, jint kvCacheType, jfloat temperature, jfloat topP,
        jboolean enableEmbeddings) {

    const char* pathChars = env->GetStringUTFChars(jPath, nullptr);
    if (!pathChars) return 0;
    std::string modelPath(pathChars);
    env->ReleaseStringUTFChars(jPath, pathChars);

    llama_backend_init();
    bridgeLlamaLogsOnce();   // 首次加载时把 llama/ggml 内部日志桥接到 logcat

    llama_model_params mparams = llama_model_default_params();
    mparams.load_mode = useMmap ? LLAMA_LOAD_MODE_MMAP : LLAMA_LOAD_MODE_NONE;
    mparams.n_gpu_layers = nGpuLayers;  // 0 = 纯 CPU；>0 卸载到 GPU（Phase 3.3 调优）

    llama_model* model = llama_model_load_from_file(modelPath.c_str(), mparams);
    if (!model) {
        LOGE("模型加载失败: %s", modelPath.c_str());
        return 0;
    }

    // KV Cache 张量精度：受支持集合 {0=F32, 1=F16, 2=Q4_0, 8=Q8_0}，非法值回退 F16。
    ggml_type kvType;
    switch (kvCacheType) {
        case 0: kvType = GGML_TYPE_F32; break;
        case 1: kvType = GGML_TYPE_F16; break;
        case 2: kvType = GGML_TYPE_Q4_0; break;
        case 8: kvType = GGML_TYPE_Q8_0; break;
        default:
            LOGE("非法 kvCacheType=%d，回退 GGML_TYPE_F16", kvCacheType);
            kvType = GGML_TYPE_F16;
            break;
    }

    llama_context_params cparams = llama_context_default_params();
    cparams.n_ctx = nCtx;
    cparams.type_k = kvType;  // KV Cache K 张量精度 [EXPERIMENTAL]
    cparams.type_v = kvType;  // KV Cache V 张量精度 [EXPERIMENTAL]
    cparams.n_threads = nThreads;
    cparams.n_batch = nBatch;
    cparams.n_ubatch = nBatch;

    // embeddings 模式：只开启向量输出，不强行指定 pooling 策略。pooling_type 默认
    // UNSPECIFIED，llama.cpp 优先采用模型自带元数据（如 Qwen3-Embedding 的 last-token
    // pooling）；元数据缺失或为 NONE 时 llama_get_embeddings_seq 会返回 NULL，此时才回退
    // 到 LAST（decoder-only 嵌入模型的 pooled 向量取末尾 token）。chat 句柄不传此开关，
    // 仅独立的 embedding 句柄开启，二者 KV 互相隔离。
    if (enableEmbeddings) {
        cparams.embeddings = true;
    }

    llama_context* ctx = llama_init_from_model(model, cparams);
    if (!ctx) {
        LOGE("创建 context 失败（内存不足？）: n_ctx=%d", nCtx);
        llama_model_free(model);
        return 0;
    }

    if (enableEmbeddings) {
        const auto pooling = llama_pooling_type(ctx);
        if (pooling == LLAMA_POOLING_TYPE_NONE) {
            LOGW("embedding 模型未声明 pooling_type（元数据缺失/NONE），回退 LLAMA_POOLING_TYPE_LAST");
            llama_free(ctx);
            cparams.pooling_type = LLAMA_POOLING_TYPE_LAST;
            ctx = llama_init_from_model(model, cparams);
            if (!ctx) {
                LOGE("创建 context 失败（回退 LAST pooling 后）: n_ctx=%d", nCtx);
                llama_model_free(model);
                return 0;
            }
        } else {
            LOGI("embedding pooling 使用模型默认值: %d", static_cast<int>(pooling));
        }
    }

    auto* s = new LlmSession();
    s->model = model;
    s->ctx = ctx;
    s->vocab = llama_model_get_vocab(model);
    s->n_ctx = nCtx;
    s->n_batch = nBatch;

    // D4：写入采样参数单一数据源（topK/repeatPenalty/repeatLastN/seed 用 D4 默认）。
    // 随后 AndroidLlmEngine 会经 setSamplingParams 以完整 LlmConfig 覆盖；此处仅作加载瞬间兜底。
    s->temperature   = temperature;
    s->topP          = topP;
    s->topK          = 40;
    s->repeatPenalty = 1.1f;
    s->repeatLastN   = 64;
    s->seed          = 0;   // 默认随机
    rebuildSampler(s);

    // 启动即确认模型是否内嵌 chat template，便于排查「格式不对」类问题
    const char* chatTmpl = llama_model_chat_template(model, nullptr);
    LOGI("模型已加载: %s (n_ctx=%d, threads=%d), chat_template=%s",
         modelPath.c_str(), nCtx, nThreads,
         (chatTmpl && chatTmpl[0] != '\0') ? "内嵌" : "无（将回退 ChatML 兜底）");
    return reinterpret_cast<jlong>(s);
}

extern "C" JNIEXPORT void JNICALL
Java_com_biyou_sanxian_llm_LlamaNative_setSamplingParams(
        JNIEnv* env, jclass /*clazz*/, jlong handle,
        jfloat temperature, jfloat topP, jint topK,
        jfloat repeatPenalty, jint repeatLastN, jlong seed) {
    LlmSession* s = asSession(handle);
    if (!s) return;
    s->temperature   = temperature;
    s->topP          = topP;
    s->topK          = topK;
    s->repeatPenalty = repeatPenalty;
    s->repeatLastN   = repeatLastN;
    s->seed          = seed;
    rebuildSampler(s);
}

extern "C" JNIEXPORT void JNICALL
Java_com_biyou_sanxian_llm_LlamaNative_freeModel(JNIEnv* /*env*/, jclass /*clazz*/, jlong handle) {
    LlmSession* s = asSession(handle);
    if (!s) return;
    if (s->sampler) llama_sampler_free(s->sampler);
    if (s->ctx) llama_free(s->ctx);
    if (s->model) llama_model_free(s->model);
    delete s;
}

extern "C" JNIEXPORT void JNICALL
Java_com_biyou_sanxian_llm_LlamaNative_stopGenerate(JNIEnv* /*env*/, jclass /*clazz*/, jlong handle) {
    if (LlmSession* s = asSession(handle)) s->cancelled = true;
}

/** 清空 KV Cache，用于开启新会话。 */
extern "C" JNIEXPORT void JNICALL
Java_com_biyou_sanxian_llm_LlamaNative_clearContext(JNIEnv* /*env*/, jclass /*clazz*/, jlong handle) {
    LlmSession* s = asSession(handle);
    if (!s || !s->ctx) return;
    llama_memory_clear(llama_get_memory(s->ctx), true);
    s->n_past = 0;
    s->cachedTokens.clear();
    s->kvValid = false;
}

extern "C" JNIEXPORT jstring JNICALL
Java_com_biyou_sanxian_llm_LlamaNative_modelDesc(JNIEnv* env, jclass /*clazz*/, jlong handle) {
    LlmSession* s = asSession(handle);
    if (!s || !s->model) return env->NewStringUTF("");
    char buf[512] = {0};
    llama_model_desc(s->model, buf, sizeof(buf));
    return env->NewStringUTF(buf);
}

extern "C" JNIEXPORT jlong JNICALL
Java_com_biyou_sanxian_llm_LlamaNative_modelSize(JNIEnv* /*env*/, jclass /*clazz*/, jlong handle) {
    LlmSession* s = asSession(handle);
    return (s && s->model) ? static_cast<jlong>(llama_model_size(s->model)) : 0;
}

extern "C" JNIEXPORT jlong JNICALL
Java_com_biyou_sanxian_llm_LlamaNative_modelParamCount(JNIEnv* /*env*/, jclass /*clazz*/, jlong handle) {
    LlmSession* s = asSession(handle);
    return (s && s->model) ? static_cast<jlong>(llama_model_n_params(s->model)) : 0;
}

/**
 * 只渲染不推理：用模型内嵌的 chat template 把多轮消息格式化成 prompt。
 *
 * 生产生成路径请用 generate()（它才是「预算 → 裁剪 → 渲染 → 分词 → eval」的完整链路）；
 * 本函数定位为诊断 / 单测入口，最典型的用途是免推理验证模板取源是否正确
 * —— 例如用 Llama-3.2-Instruct（非 ChatML 系）调一次就能看出渲染结果里是不是
 * `<|start_header_id|>` 这类自家标记。
 *
 * 两个必须知道的点：
 *  1. llama_chat_apply_template 的首参是「模板字符串本身」。传 nullptr 会被 llama.cpp
 *     硬编码成 "chatml"（见 third_party/llama.cpp/src/llama.cpp 中 curr_tmpl 的构造），
 *     与模型自带模板无关 —— 即所有模型都会被套上 ChatML 格式。取模型内嵌模板必须走
 *     llama_model_chat_template(model, nullptr)。
 *  2. llama_chat_apply_template 不是 Jinja 解析器，只对预置模板族做子串识别
 *     （见 third_party/llama.cpp/src/llama-chat.cpp 的 llm_chat_detect_template）。
 *     模型模板不在识别范围内时返回 <=0，此时 renderChat 会退化为 ChatML。
 *
 * @return 渲染后的 prompt；消息数组为空时返回空串
 */
extern "C" JNIEXPORT jstring JNICALL
Java_com_biyou_sanxian_llm_LlamaNative_formatChat(
        JNIEnv* env, jclass /*clazz*/, jlong handle,
        jobjectArray roles, jobjectArray contents) {

    LlmSession* s = asSession(handle);
    if (!s || !s->model) return env->NewStringUTF("");

    std::vector<ChatMsg> msgs;
    if (!readChatArrays(env, roles, contents, &msgs)) return env->NewStringUTF("");

    std::string out;
    if (!renderChat(s, msgs, /*keepSystem=*/false, /*start=*/0, &out)) {
        return env->NewStringUTF("");
    }
    return env->NewStringUTF(out.c_str());
}

/**
 * 返回模型 GGUF 内嵌的 chat template 原文（tokenizer.chat_template），
 * 供诊断 / UI 查看「当前模型实际使用的模板」。模型未内嵌时返回空串。
 */
extern "C" JNIEXPORT jstring JNICALL
Java_com_biyou_sanxian_llm_LlamaNative_modelChatTemplate(
        JNIEnv* env, jclass /*clazz*/, jlong handle) {
    LlmSession* s = asSession(handle);
    if (!s || !s->model) return env->NewStringUTF("");
    const char* tmpl = llama_model_chat_template(s->model, nullptr);
    return env->NewStringUTF(tmpl ? tmpl : "");
}

/**
 * 流式生成。每产出一个 token 就回调 TokenSink.onToken()，返回 false 可中断。
 *
 * 入参是**消息数组**而不是渲染好的 prompt 字符串：上下文超出 n_ctx 时必须按消息粒度裁剪
 * （丢最早的历史，保全 system 与最新提问），而裁剪必须早于 chat template 渲染。
 * 「渲染 + 分词 + 预算判定」只有在原生侧才能闭环（n_ctx / maxTokens / 分词器都在这里）。
 * 完整降级阶梯见 .trellis/tasks/09-28-fix-context-overflow-window/design.md §4。
 *
 * @return >=0 生成的 token 数；<0 为错误码：
 *         -1 未加载 / -2 回调方法缺失 / -3 已被取消 / -4 推理失败 / -5 Java 回调抛异常
 *         -6 无法构造合法 prompt（消息数组为空，或裁剪到极限仍超预算）
 */
extern "C" JNIEXPORT jint JNICALL
Java_com_biyou_sanxian_llm_LlamaNative_generate(
        JNIEnv* env, jclass /*clazz*/,
        jlong handle, jobjectArray roles, jobjectArray contents,
        jint maxTokens, jobject sink) {

    LlmSession* s = asSession(handle);
    if (!s || !s->ctx || !s->vocab) return -1;

    jclass sinkCls = env->GetObjectClass(sink);
    jmethodID onToken = env->GetMethodID(sinkCls, "onToken", "(Ljava/lang/String;)Z");
    if (!onToken) return -2;

    std::vector<ChatMsg> msgs;
    if (!readChatArrays(env, roles, contents, &msgs)) {
        LOGE("消息数组为空或读取失败，无法构造 prompt");
        return -6;
    }
    // 清除上一轮收尾时（flow 的 finally 里 stopGenerate）置位的取消标志。
    // 必须在裁剪探针之前清：上一轮残留的 true 会让本轮直接判取消，且裁剪期间用户点
    // 「停止」也不会被响应。
    s->cancelled = false;

    const llama_vocab* vocab = s->vocab;
    const int32_t n_ctx = s->n_ctx;

    // ---- 1. 输入预算：n_ctx 扣掉生成长度预留与安全余量 ----
    int32_t budget = n_ctx - maxTokens - kSafetyMargin;
    if (budget <= 0) {
        LOGE("配置异常：n_ctx=%d 装不下 maxTokens=%d + 余量 %d，输入预算压到 1",
             n_ctx, maxTokens, kSafetyMargin);
        budget = 1;
    }

    // ---- 2. 选择保留窗口（渲染 + 分词 + 判预算，命中即带回 prompt/tokens）----
    std::string prompt;
    std::vector<llama_token> tokens;

    if (!probeBudget(s, msgs, /*keepSystem=*/false, /*start=*/0, budget, &prompt, &tokens)) {
        // 超预算：从最早的历史开始丢，system 与最新一条消息优先保全。不在此上报「原始
        // token 数」——探针失败可能是分词错误而非真超长，硬说「X > budget」会误导排查；
        // 下方按「丢弃条数 + 最终 token 数」如实记录。
        const size_t last = msgs.size() - 1;
        const bool hasSystem = msgs.size() > 1 && msgs[0].role == "system";
        const size_t systemFloor = hasSystem ? 1 : 0;  // 保留 system 时历史最早能切到哪

        if (s->cancelled) return -3;  // 裁剪期间也响应取消

        if (probeBudget(s, msgs, hasSystem, last, budget, &prompt, &tokens)) {
            // T1：system + 最后一条能装下 → 二分找「丢得最少」的 start
            //     单调性：start 越大（保留越少）→ token 数单调不增
            size_t lo = systemFloor;
            size_t hi = last;
            while (lo < hi) {
                if (s->cancelled) return -3;  // 二分探针较多，期间也响应取消
                size_t mid = lo + (hi - lo) / 2;
                if (probeBudget(s, msgs, hasSystem, mid, budget, &prompt, &tokens)) hi = mid;
                else lo = mid + 1;
            }
            // 二分退出时的 prompt/tokens 未必对应 lo（最后可能是一次失败的探针），重探一次
            if (!probeBudget(s, msgs, hasSystem, lo, budget, &prompt, &tokens)) {
                LOGE("裁剪后仍超预算（预算 %d），无法构造 prompt", budget);
                return -6;
            }
            LOGI("上下文溢出：丢弃最早 %zu 条历史，保留 system=%d，最终 %zu tokens（预算 %d）",
                 lo - systemFloor, hasSystem ? 1 : 0, tokens.size(), budget);
        } else if (hasSystem &&
                   shrinkMessageToFit(s, &msgs, 0, /*keepSystem=*/true, last, budget,
                                      &prompt, &tokens)) {
            // T2：system 自身超预算 → 按 token 截断 system，但仍保留它（PRD 唯一允许切开
            //     单条消息内部的路径，且前提是 system 必须保留）
            // 截断后的 system 与缓存的 KV（旧完整 system 渲染结果）不再一致，强制下一轮全量重算
            s->kvValid = false;
            LOGE("system prompt 超预算，已按 token 截断；丢弃最早 %zu 条历史，最终 %zu tokens",
                 last - systemFloor, tokens.size());
        } else {
            // T3：连 system + 最后一条都装不下 → 不静默截断用户提问。单条消息自身超出预算，
            //     要么丢整条（无内容可问）、要么切一半；后者违反 PRD「不得切开单条消息」
            //     与「最新用户消息必须完整存在」，故报错交由 UI 呈现。
            LOGE("单条消息超出输入预算（%d），无法构造 prompt（不静默截断用户消息）", budget);
            return -6;
        }
    }

    // ---- 3. KV 复用决策：增量复用 or 全量重算 ----
    //      增量：保留 LCP 长度的 KV，只对新增 token 做 llama_decode（设计见
    //      09-28-multi-turn-kv-reuse/design.md §3）。开关关闭时退回旧行为。
    llama_memory_t mem = llama_get_memory(s->ctx);

    // D4：每轮以保存的采样参数重建采样链——seed>0 决定论，seed<=0 每次 fresh rand；
    //     重建成本远小于 eval，且规避 dist reset 的 RNG 语义歧义（见 design.md §3.2）。
    rebuildSampler(s);
    // 保留：新链上为 no-op，但覆盖 penalties 状态，保证轮次隔离（D3 groundwork）。
    llama_sampler_reset(s->sampler);

    size_t reuseLen = 0;
    if (kEnableIncrementalKv && s->kvValid && !s->cachedTokens.empty()
        && s->n_past == static_cast<int32_t>(s->cachedTokens.size())) {
        reuseLen = longestCommonPrefix(s->cachedTokens, tokens);
        // BPE 边界退让：两次 tokenize 的尾部合并方式可能不同，重算末几个 token 换取确定性
        reuseLen = (reuseLen > static_cast<size_t>(kSafetyRewind))
                       ? (reuseLen - static_cast<size_t>(kSafetyRewind))
                       : 0;
        // 不能复用超出 KV 实际驻留的内容
        reuseLen = std::min(reuseLen, static_cast<size_t>(s->n_past));
        // 小复用不值得截断 + 重建的开销
        if (reuseLen < kMinReuseTokens) reuseLen = 0;
    }

    if (reuseLen == 0) {
        // 全量路径：清空 KV，从 0 重 eval 整段对话
        llama_memory_clear(mem, true);
        s->n_past = 0;
        LOGI("KV 全量重算: %zu tokens", tokens.size());
    } else {
        // 增量路径：截断 KV 到 [0, reuseLen)，只 eval 之后的 token
        if (reuseLen < static_cast<size_t>(s->n_past)) {
            if (!llama_memory_seq_rm(mem, 0, static_cast<llama_pos>(reuseLen), -1)) {
                LOGE("seq_rm 失败，退回全量重算");
                llama_memory_clear(mem, true);
                s->n_past = 0;
                s->kvValid = false;
                LOGI("KV 全量重算（seq_rm 回退）: %zu tokens", tokens.size());
            } else {
                s->n_past = static_cast<int32_t>(reuseLen);
                LOGI("KV 复用: LCP=%zu, 新增 eval=%zu, n_past=%d",
                     reuseLen, tokens.size() - reuseLen, s->n_past);
            }
        } else {
            LOGI("KV 复用: LCP=%zu, 新增 eval=%zu, n_past=%d",
                 reuseLen, tokens.size() - reuseLen, s->n_past);
        }
    }

    // 快照：记录本轮送入 KV 的完整 token（prompt + 后续逐 token 生成的 assistant token）
    std::vector<llama_token> kvTokens = tokens;

    // ---- 4. 分批 eval prompt（从复用点 offset 起，不重复分词）----
    const size_t start = static_cast<size_t>(s->n_past);
    size_t offset = start;
    while (offset < kvTokens.size()) {
        int32_t n = static_cast<int32_t>(
                std::min<size_t>(kvTokens.size() - offset, static_cast<size_t>(s->n_batch)));
        bool needLogits = (offset + static_cast<size_t>(n) >= kvTokens.size());
        int32_t rc = decodeChunk(s, kvTokens, offset, n, /*needLogits=*/needLogits);
        if (rc != 0) {
            LOGE("prompt eval 失败: decode_rc=%d, n_ctx=%d, n_batch=%d, batch_n=%d, n_past=%d, total=%zu",
                 rc, s->n_ctx, s->n_batch, n, s->n_past, kvTokens.size());
            s->kvValid = false;
            return -4;
        }
        s->n_past += n;
        offset += static_cast<size_t>(n);
    }

    // ---- 5. 逐 token 采样 + 回调 ----
    const llama_token eos = llama_vocab_eos(vocab);
    const llama_token eot = llama_vocab_eot(vocab);
    int32_t nGenerated = 0;
    bool ok = true;  // 正常完成（含 eos/eot/上下文占满/达到 maxTokens）才置 true，供末尾更新快照

    while (nGenerated < maxTokens) {
        if (s->cancelled) {
            s->kvValid = false;  // 取消后 KV 部分写入，下一轮强制全量
            return -3;
        }
        if (s->n_past >= n_ctx) break;

        llama_token id = llama_sampler_sample(s->sampler, s->ctx, -1);
        if (id == eos || id == eot) break;  // 正常结束：eot 不进 KV，下一轮前缀可续接

        char buf[256];
        int32_t n = llama_token_to_piece(vocab, id, buf, sizeof(buf), 0, true);
        if (n < 0) {
            s->kvValid = false;  // 渲染失败视为异常，下一轮强制全量
            ok = false;
            break;
        }

        llama_sampler_accept(s->sampler, id);

        jstring jPiece = env->NewStringUTF(std::string(buf, static_cast<size_t>(n)).c_str());
        if (!jPiece) {
            s->kvValid = false;
            return -4;
        }
        jboolean cont = env->CallBooleanMethod(sink, onToken, jPiece);
        env->DeleteLocalRef(jPiece);  // 长循环必须释放，否则局部引用表溢出

        if (env->ExceptionCheck()) {
            s->kvValid = false;
            return -5;
        }
        if (!cont) {
            s->cancelled = true;
            s->kvValid = false;  // 用户停止，KV 含半成品，下一轮强制全量
            ok = false;
            break;
        }

        // 把新 token 送入解码，为下一次采样准备 logits；同步进快照
        kvTokens.push_back(id);
        std::vector<llama_token> one{id};
        int32_t rc2 = decodeChunk(s, one, 0, 1, /*needLogits=*/true);
        if (rc2 != 0) {
            LOGE("token decode 失败: decode_rc=%d, n_past=%d, n_ctx=%d", rc2, s->n_past, s->n_ctx);
            s->kvValid = false;
            return -4;
        }
        s->n_past++;
        nGenerated++;
    }

    // 正常结束（达到 maxTokens / 命中 eos·eot / 上下文占满）：KV 与快照一致，供下一轮复用。
    // 用户停止 / 渲染失败等异常路径已置 ok=false（并 kvValid=false），不更新快照。
    if (ok) {
        s->cachedTokens = std::move(kvTokens);
        s->kvValid = true;
    }
    LOGI("生成结束: %d tokens, prompt=%zu tokens, n_past=%d", nGenerated, tokens.size(), s->n_past);
    return nGenerated;
}

/**
 * 文本向量化（embeddings JNI 桥，Plan A：独立 embedding 句柄）。
 *
 * 与 generate() 走不同的 llama.cpp 入口：generate 用 llama_decode + 采样，embed 用
 * llama_encode + 取 pooled 向量。两者都写同一上下文的 KV，因此必须跑在**独立的**
 * embedding 上下文上（loadModel 时 enableEmbeddings=true 创建），与 chat 句柄隔离，
 * 否则会互相污染 KV。
 *
 * 流程：分词 → llama_batch_get_one(单序列, pos=0) → llama_encode →
 *       llama_get_embeddings_seq(ctx, 0) 取 seq 0 的 pooled 向量 → jfloatArray。
 *
 * @return n_embd 维向量；失败（未加载 / 分词空 / encode 失败 / pooling 为 NONE）返回长度 0 数组。
 */
extern "C" JNIEXPORT jfloatArray JNICALL
Java_com_biyou_sanxian_llm_LlamaNative_embed(
        JNIEnv* env, jclass /*clazz*/, jlong handle, jstring jtext) {

    LlmSession* s = asSession(handle);
    if (!s || !s->model || !s->ctx) {
        LOGE("embed 失败：句柄未加载");
        return env->NewFloatArray(0);
    }

    const char* txt = env->GetStringUTFChars(jtext, nullptr);
    if (!txt) return env->NewFloatArray(0);
    std::string text(txt);
    env->ReleaseStringUTFChars(jtext, txt);

    std::vector<llama_token> toks;
    if (!tokenizePrompt(s->vocab, text, &toks) || toks.empty()) {
        LOGE("embed 分词失败（空文本或非法输入），返回空向量");
        return env->NewFloatArray(0);
    }

    // llama_encode 的硬约束：n_tokens 不得超过 n_ubatch，否则 llama.cpp 内
    // GGML_ASSERT(n_ubatch >= n_tokens) 会直接 abort 进程（不是返回错误码）。
    // 因此按「实际分词结果」截断，而不是限制字符数（字符与 token 并非一一对应）。
    const size_t maxTokens = static_cast<size_t>(llama_n_ubatch(s->ctx));
    if (maxTokens == 0) {
        LOGE("embed 失败：llama_n_ubatch 返回 0");
        return env->NewFloatArray(0);
    }
    if (toks.size() > maxTokens) {
        LOGW("embed 输入过长：%zu tokens 超过 n_ubatch=%zu，已截断保留前 %zu tokens",
             toks.size(), maxTokens, maxTokens);
        toks.resize(maxTokens);
    }

    // llama_batch_get_one 内部 pos=0、seq_id=0，返回的 batch 须用 llama_batch_free 释放。
    llama_batch batch = llama_batch_get_one(toks.data(), static_cast<int32_t>(toks.size()));
    if (llama_encode(s->ctx, batch) != 0) {
        LOGE("embed llama_encode 失败");
        llama_batch_free(batch);
        return env->NewFloatArray(0);
    }
    llama_batch_free(batch);

    // pooling 跟随模型元数据（cparams.pooling_type 默认 UNSPECIFIED）；仅当元数据缺失
    // 或为 NONE 时，loadModel 才重建 context 回退 LAST。此处仍兜底判空。
    const float* emb = llama_get_embeddings_seq(s->ctx, 0);
    if (!emb) {
        LOGE("embed 取向量失败（pooling_type 可能为 NONE）");
        return env->NewFloatArray(0);
    }

    const int32_t n = llama_model_n_embd(s->model);
    jfloatArray out = env->NewFloatArray(n);
    if (!out) return env->NewFloatArray(0);
    env->SetFloatArrayRegion(out, 0, n, emb);
    return out;
}
