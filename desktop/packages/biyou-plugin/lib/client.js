window.__ModuleLoader__.load({
  id: `@biyou/dsh-plugin`,
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;
    const React = require("react");
    const h = React.createElement;

    /**
     * 运行状态 pill（D1 占位）。
     *
     * 挂在 `shell.overlay` 这个全帧浮层里：该 slot 本身 click-through，
     * 条目要自己退出指针事件，否则会挡住下面的应用。
     * 颜色一律用 currentColor + color-mix 推导，跟着 dsh 的深浅主题走，
     * 不在这里硬编码第二套色板。
     */
    function BiyouStatusPill() {
      return h(
        "div",
        {
          "data-biyou": "status",
          title: "碧游宫",
          style: {
            pointerEvents: "none",
            position: "absolute",
            right: "12px",
            bottom: "10px",
            display: "inline-flex",
            alignItems: "center",
            gap: "6px",
            height: "24px",
            padding: "0 10px",
            borderRadius: "999px",
            border: "1px solid color-mix(in srgb, currentColor 14%, transparent)",
            background: "color-mix(in srgb, currentColor 6%, transparent)",
            color: "inherit",
            font: "inherit",
            fontSize: "12px",
            lineHeight: "1",
            opacity: 0.72,
            userSelect: "none",
          },
        },
        h("span", {
          "aria-hidden": "true",
          style: {
            width: "6px",
            height: "6px",
            borderRadius: "999px",
            background: "currentColor",
            opacity: 0.55,
          },
        }),
        "碧游宫",
      );
    }

    /**
     * 浏览器插件体：只注册自己那一格，不替换、不复制上游的 layout / sidebar / conversation。
     * @param {import('@deepseek-ai/cordis').Context} ctx - 客户端根 context。
     */
    function apply(ctx) {
      ctx.slots.inject("shell.overlay", () =>
        ctx.slots.register(
          { name: "shell.overlay", id: "biyou.status" },
          BiyouStatusPill,
        ),
      );
    }

    const inject = ["slots"];

    exports.apply = apply;
    exports.inject = inject;
    return module.exports;
  },
});
