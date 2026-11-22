window.__ModuleLoader__.load({
  id: `@biyou/dsh-local-models`,
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;
    const React = require("react");
    const h = React.createElement;

    const STATUS_PATH = "/api/biyou-local/status";
    const ACTION_PATH = "/api/biyou-local/action";
    const POLL_MS = 2000;

    async function fetchStatus() {
      const response = await fetch(STATUS_PATH, { cache: "no-store" });
      const data = await response.json().catch(() => null);
      if (!response.ok || !data?.ok) {
        throw new Error(data?.error?.message || `状态读取失败（HTTP ${response.status}）`);
      }
      return data.value;
    }

    async function callAction(action, args) {
      const response = await fetch(ACTION_PATH, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ action, args: args || {} }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok || !data?.ok) {
        const error = new Error(data?.error?.message || `操作失败（HTTP ${response.status}）`);
        error.code = data?.error?.code;
        throw error;
      }
      return data.value;
    }

    function formatBytes(value) {
      const bytes = Number(value) || 0;
      if (bytes < 1024) return `${bytes} B`;
      const units = ["KB", "MB", "GB", "TB"];
      let size = bytes / 1024;
      let index = 0;
      while (size >= 1024 && index < units.length - 1) {
        size /= 1024;
        index += 1;
      }
      return `${size.toFixed(size >= 10 ? 0 : 1)} ${units[index]}`;
    }

    function formatCtx(value) {
      const ctx = Number(value) || 0;
      return ctx >= 1024 ? `${Math.round(ctx / 1024)}K` : String(ctx);
    }

    function progressOf(record) {
      if (!record || !record.totalBytes) return 0;
      return Math.max(0, Math.min(100, Math.round((record.receivedBytes / record.totalBytes) * 100)));
    }

    const styles = {
      page: {
        display: "flex",
        flexDirection: "column",
        gap: "18px",
        padding: "20px 24px 32px",
        color: "var(--text, #0f1115)",
        fontSize: "14px",
        lineHeight: 1.5,
      },
      header: { display: "flex", flexDirection: "column", gap: "4px" },
      title: { margin: 0, fontSize: "17px", fontWeight: 600 },
      subtitle: { margin: 0, color: "var(--text-2, #61666b)", fontSize: "13px" },
      card: {
        border: "1px solid var(--line, rgba(0,0,0,.08))",
        borderRadius: "12px",
        background: "var(--bg-layer-1, #f9fafb)",
        padding: "14px 16px",
        display: "flex",
        flexDirection: "column",
        gap: "10px",
      },
      cardTitle: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: "10px",
        fontSize: "14px",
        fontWeight: 600,
      },
      row: { display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" },
      meta: { color: "var(--text-2, #61666b)", fontSize: "12px" },
      tag: {
        display: "inline-flex",
        alignItems: "center",
        height: "20px",
        padding: "0 8px",
        borderRadius: "999px",
        border: "1px solid var(--line-strong, rgba(0,0,0,.12))",
        color: "var(--text-2, #61666b)",
        fontSize: "11px",
        whiteSpace: "nowrap",
      },
      button: {
        height: "30px",
        padding: "0 12px",
        borderRadius: "8px",
        border: "1px solid var(--line-strong, rgba(0,0,0,.12))",
        background: "var(--bg-raised, #ffffff)",
        color: "var(--text, #0f1115)",
        fontSize: "13px",
        cursor: "pointer",
      },
      primary: {
        height: "30px",
        padding: "0 12px",
        borderRadius: "8px",
        border: "1px solid transparent",
        background: "var(--accent, #3964fe)",
        color: "#ffffff",
        fontSize: "13px",
        cursor: "pointer",
      },
      danger: {
        height: "30px",
        padding: "0 12px",
        borderRadius: "8px",
        border: "1px solid var(--line-strong, rgba(0,0,0,.12))",
        background: "transparent",
        color: "var(--danger, #f25a5a)",
        fontSize: "13px",
        cursor: "pointer",
      },
      bar: {
        height: "6px",
        borderRadius: "999px",
        background: "var(--line, rgba(0,0,0,.08))",
        overflow: "hidden",
        width: "100%",
      },
      barFill: {
        height: "100%",
        borderRadius: "999px",
        background: "var(--accent, #3964fe)",
        transition: "width .2s ease",
      },
      input: {
        height: "32px",
        flex: "1 1 260px",
        minWidth: "200px",
        padding: "0 10px",
        borderRadius: "8px",
        border: "1px solid var(--line-strong, rgba(0,0,0,.12))",
        background: "var(--bg-base, #ffffff)",
        color: "var(--text, #0f1115)",
        fontSize: "13px",
      },
      pre: {
        margin: 0,
        padding: "10px 12px",
        borderRadius: "8px",
        background: "var(--bg-base, #ffffff)",
        border: "1px solid var(--line, rgba(0,0,0,.08))",
        color: "var(--text-2, #61666b)",
        fontSize: "11px",
        lineHeight: 1.5,
        maxHeight: "180px",
        overflow: "auto",
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
      },
      error: {
        border: "1px solid color-mix(in srgb, var(--danger, #f25a5a) 35%, transparent)",
        background: "color-mix(in srgb, var(--danger, #f25a5a) 8%, transparent)",
        color: "var(--danger, #f25a5a)",
        borderRadius: "8px",
        padding: "8px 10px",
        fontSize: "12px",
      },
      model: {
        border: "1px solid var(--line, rgba(0,0,0,.08))",
        borderRadius: "10px",
        background: "var(--bg-base, #ffffff)",
        padding: "12px 14px",
        display: "flex",
        flexDirection: "column",
        gap: "8px",
      },
    };

    function Button(props) {
      const style = props.variant === "primary" ? styles.primary : props.variant === "danger" ? styles.danger : styles.button;
      return h(
        "button",
        {
          type: "button",
          style: { ...style, opacity: props.disabled ? 0.55 : 1, cursor: props.disabled ? "default" : "pointer" },
          disabled: props.disabled,
          onClick: props.onClick,
          title: props.title,
        },
        props.children,
      );
    }

    function DownloadBar(props) {
      const record = props.record;
      if (!record) return null;
      const percent = progressOf(record);
      const active = record.status === "downloading" || record.status === "verifying";
      return h(
        "div",
        { style: { display: "flex", flexDirection: "column", gap: "6px" } },
        h(
          "div",
          { style: { ...styles.row, justifyContent: "space-between", fontSize: "12px", color: "var(--text-2)" } },
          h("span", null, active ? `下载中 ${percent}%` : `已暂停 ${percent}%`),
          h("span", null, `${formatBytes(record.receivedBytes)} / ${formatBytes(record.totalBytes)}`),
        ),
        h("div", { style: styles.bar }, h("div", { style: { ...styles.barFill, width: `${percent}%` } })),
        h(
          "div",
          { style: styles.row },
          active
            ? h(Button, { onClick: props.onPause, disabled: props.disabled }, "暂停")
            : h(Button, { onClick: props.onResume, disabled: props.disabled }, "继续"),
          h(Button, { variant: "danger", onClick: props.onCancel, disabled: props.disabled }, "取消"),
        ),
      );
    }

    function RuntimeSection(props) {
      const { status, busy, run } = props;
      const runtime = status.runtime;
      const download = status.downloads.find((item) => item.kind === "runtime");
      const active = runtime.active;
      return h(
        "section",
        { style: styles.card },
        h(
          "div",
          { style: styles.cardTitle },
          h("span", null, "运行时"),
          h("span", { style: styles.tag }, active ? `已安装 ${active.tag} · ${active.backend}` : "未安装"),
        ),
        h(
          "div",
          { style: styles.meta },
          `本机 ${runtime.platform}-${runtime.arch}` +
            (runtime.gpu && runtime.gpu !== "unknown" ? ` · GPU ${runtime.gpu}` : "") +
            (runtime.target ? ` · 推荐 ${runtime.target.backend}` : ""),
        ),
        runtime.supported
          ? null
          : h("div", { style: styles.error }, runtime.reason || "当前平台暂不支持本地运行时"),
        download && (download.status === "downloading" || download.status === "verifying" || download.status === "paused")
          ? h(DownloadBar, {
              record: download,
              disabled: Boolean(busy),
              onPause: () => run("runtime.pause"),
              onResume: () => run("runtime.resume"),
              onCancel: () => run("runtime.cancel"),
            })
          : null,
        download?.status === "failed"
          ? h(
              "div",
              { style: { display: "flex", flexDirection: "column", gap: "8px" } },
              h("div", { style: styles.error }, download.error?.message || "运行时下载失败"),
              h(Button, { onClick: () => run("runtime.install"), disabled: Boolean(busy) }, "重试安装"),
            )
          : null,
        !download || download.status === "completed" || download.status === "cancelled"
          ? h(
              "div",
              { style: styles.row },
              h(
                Button,
                {
                  variant: active ? "default" : "primary",
                  disabled: Boolean(busy) || !runtime.supported,
                  onClick: () => run("runtime.install"),
                },
                active ? "重新安装运行时" : "安装运行时",
              ),
              h(Button, { onClick: () => run("runtime.detect"), disabled: Boolean(busy) }, "探测已有服务"),
            )
          : null,
        active
          ? h(
              "div",
              { style: styles.meta },
              `已安装版本：${active.tag} · ${active.backend} · ${active.installedAt ? new Date(active.installedAt).toLocaleString() : "时间未知"}`,
            )
          : null,
        status.external?.server || status.external?.ollama
          ? h(
              "div",
              { style: { ...styles.meta, display: "flex", flexDirection: "column", gap: "2px" } },
              status.external.server ? h("span", null, `检测到 llama-server：${status.external.server.baseUrl}`) : null,
              status.external.ollama
                ? h("span", null, `检测到 Ollama：${status.external.ollama.baseUrl}（${status.external.ollama.models.length} 个模型，仅提示）`)
                : null,
            )
          : null,
      );
    }

    function ModelRow(props) {
      const { model, busy, run } = props;
      const download = model.download;
      const active = download && (download.status === "downloading" || download.status === "verifying" || download.status === "paused");
      const installed = model.servable;
      return h(
        "div",
        { style: styles.model },
        h(
          "div",
          { style: styles.row },
          h("strong", { style: { fontSize: "14px" } }, model.display_name),
          h("span", { style: styles.tag }, model.tool_calling ? "工具调用" : "文本"),
          h("span", { style: styles.tag }, model.source === "imported" ? "已导入" : model.license || "license ?"),
          h("span", { style: styles.tag }, installed ? "可服务" : model.installed ? "文件不完整" : "未安装"),
        ),
        model.description ? h("div", { style: styles.meta }, model.description) : null,
        h(
          "div",
          { style: styles.meta },
          [
            formatBytes(model.total_bytes),
            `上下文 ${formatCtx(model.context?.default || model.context?.train)}（可到 ${formatCtx(model.context?.max)}）`,
            model.estimate?.fit === "no" ? "内存可能不足" : model.estimate?.fit === "tight" ? "内存偏紧" : null,
            model.repo || null,
          ]
            .filter(Boolean)
            .join(" · "),
        ),
        active
          ? h(DownloadBar, {
              record: download,
              disabled: Boolean(busy),
              onPause: () => run("model.pause", { id: model.id }),
              onResume: () => run("model.resume", { id: model.id }),
              onCancel: () => run("model.cancel", { id: model.id }),
            })
          : null,
        download?.status === "failed"
          ? h("div", { style: styles.error }, download.error?.message || "下载失败")
          : null,
        h(
          "div",
          { style: styles.row },
          installed
            ? h(
                Button,
                { variant: "primary", disabled: Boolean(busy), onClick: () => run("model.use", { id: model.id }) },
                "加载并使用",
              )
            : h(
                Button,
                {
                  variant: "primary",
                  disabled: Boolean(busy) || active,
                  onClick: () => run("model.download", { id: model.id }),
                },
                model.installed ? "重新下载" : "下载",
              ),
          installed
            ? h(Button, { variant: "danger", disabled: Boolean(busy), onClick: () => run("model.remove", { id: model.id }) }, "移除")
            : null,
        ),
      );
    }

    function ModelsSection(props) {
      const { status, busy, run } = props;
      const models = status.catalog.models || [];
      return h(
        "section",
        { style: styles.card },
        h("div", { style: styles.cardTitle }, h("span", null, `模型（${models.filter((model) => model.servable).length}/${models.length} 可服务）`)),
        models.length === 0 ? h("div", { style: styles.meta }, "目录为空") : models.map((model) => h(ModelRow, { key: model.id, model, busy, run })),
        h(
          "div",
          { style: { ...styles.row, borderTop: "1px solid var(--line, rgba(0,0,0,.08))", paddingTop: "10px" } },
          h(ImportControl, { busy, run }),
        ),
      );
    }

    function ImportControl(props) {
      const [path, setPath] = React.useState("");
      return h(
        "div",
        { style: { ...styles.row, width: "100%" } },
        h("input", {
          style: styles.input,
          value: path,
          placeholder: "导入已有 .gguf 的绝对路径（只建链接，不复制）",
          onChange: (event) => setPath(event.target.value),
          spellCheck: false,
        }),
        h(
          Button,
          {
            disabled: props.busy || path.trim().length === 0,
            onClick: async () => {
              await props.run("model.import", { path: path.trim() });
              setPath("");
            },
          },
          "导入",
        ),
      );
    }

    function ServerSection(props) {
      const { status, busy, run } = props;
      const server = status.server;
      const stateLabel = {
        stopped: "已停止",
        starting: "启动中",
        idle: "空闲（模型未加载）",
        loading: "模型加载中",
        ready: "就绪",
        error: "错误",
      }[server.state] || server.state;
      return h(
        "section",
        { style: styles.card },
        h(
          "div",
          { style: styles.cardTitle },
          h("span", null, "服务状态"),
          h("span", { style: styles.tag }, stateLabel),
        ),
        h(
          "div",
          { style: styles.meta },
          [
            server.port ? `端口 ${server.port}` : null,
            server.loaded_model ? `模型 ${server.loaded_model}` : "未加载模型",
            server.pid ? `pid ${server.pid}` : null,
          ]
            .filter(Boolean)
            .join(" · "),
        ),
        server.error ? h("div", { style: styles.error }, server.error) : null,
        h(
          "div",
          { style: styles.row },
          h(Button, { variant: "primary", disabled: Boolean(busy), onClick: () => run("server.start") }, "启动服务"),
          h(Button, { disabled: Boolean(busy), onClick: () => run("server.stop") }, "停止服务"),
          h(Button, { disabled: Boolean(busy), onClick: () => run("catalog.refresh") }, "刷新"),
        ),
        server.log_tail?.length ? h("pre", { style: styles.pre }, server.log_tail.slice(-20).join("\n")) : null,
      );
    }

    function LocalModelsPage() {
      const [status, setStatus] = React.useState(null);
      const [error, setError] = React.useState(null);
      const [busy, setBusy] = React.useState(null);

      const refresh = React.useCallback(async () => {
        try {
          setStatus(await fetchStatus());
          setError(null);
        } catch (cause) {
          setError(cause instanceof Error ? cause.message : String(cause));
        }
      }, []);

      React.useEffect(() => {
        void refresh();
        const timer = setInterval(() => {
          void refresh();
        }, POLL_MS);
        return () => clearInterval(timer);
      }, [refresh]);

      const run = React.useCallback(
        async (action, args) => {
          setBusy(action);
          setError(null);
          try {
            await callAction(action, args);
            await refresh();
          } catch (cause) {
            setError(cause instanceof Error ? cause.message : String(cause));
          } finally {
            setBusy(null);
          }
        },
        [refresh],
      );

      if (!status) {
        return h(
          "div",
          { style: styles.page, "data-biyou": "local-models" },
          h("div", { style: styles.header }, h("h2", { style: styles.title }, "本地模型"), h("p", { style: styles.subtitle }, "正在读取本地模型状态…")),
          error ? h("div", { style: styles.error }, error) : null,
        );
      }

      return h(
        "div",
        { style: styles.page, "data-biyou": "local-models" },
        h(
          "div",
          { style: styles.header },
          h("h2", { style: styles.title }, "本地模型"),
          h(
            "p",
            { style: styles.subtitle },
            "在客户端内安装 llama.cpp 运行时、下载 GGUF，并把本地模型注册为 dsh provider；模型下载完成后可完全离线对话与调用工具。",
          ),
        ),
        error ? h("div", { style: styles.error }, error) : null,
        busy ? h("div", { style: styles.meta }, `正在执行：${busy}`) : null,
        h(RuntimeSection, { status, busy, run }),
        h(ModelsSection, { status, busy, run }),
        h(ServerSection, { status, busy, run }),
        h(
          "p",
          { style: { ...styles.meta, margin: 0 } },
          "运行时与模型都按需下载，不进入安装包。API key 只保存在用户数据目录，不会显示在此页面。",
        ),
      );
    }

    /** 浏览器插件体：只注册设置页中的一个 tab。 */
    function apply(ctx) {
      ctx.slots.inject("settings.plugins.tab", () =>
        ctx.slots.register(
          {
            name: "settings.plugins.tab",
            id: "biyou.local-models",
            order: 40,
            label: () => "本地模型",
          },
          LocalModelsPage,
        ),
      );
    }

    const inject = ["slots"];

    exports.apply = apply;
    exports.inject = inject;
    return module.exports;
  },
});
