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
     * 「青萍剑」标记：剑身 + 叶形剑格 + 剑柄 + 剑首。
     *
     * 几何与 `assets/brand/mark.svg` 完全一致（同一套 path），区别只是这里用
     * `currentColor`：侧栏那行文字是深是浅，标记就跟着走，不引入第二套色板。
     * @param {{ size?: number }} props - 侧栏给出的请求边长。
     */
    function BiyouBrandMark(props) {
      const size = props && typeof props.size === "number" ? props.size : 24;
      return h(
        "svg",
        {
          viewBox: "0 0 100 100",
          width: size,
          height: size,
          fill: "none",
          "aria-hidden": "true",
          style: { display: "block", flex: "none" },
        },
        h(
          "g",
          { fill: "currentColor" },
          h("path", { d: "M50 15 L53.4 37 L53.4 59 L46.6 59 L46.6 37 Z" }),
          h("path", {
            d: "M35 65 C40.2 61.2 45 60.2 50 60.2 C55 60.2 59.8 61.2 65 65 C59.8 63.8 55 63.2 50 63.2 C45 63.2 40.2 63.8 35 65 Z",
          }),
          h("rect", { x: 47.4, y: 65, width: 5.2, height: 11, rx: 1.6 }),
        ),
        h("circle", { cx: 50, cy: 80.4, r: 3.4, fill: "currentColor" }),
      );
    }

    /** 品牌名：只出文字，字号与字距跟随侧栏这一行，不做额外排印。 */
    function BiyouBrandName() {
      return h(
        "span",
        { style: { whiteSpace: "nowrap", letterSpacing: "0.02em" } },
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

      // 品牌位是上游留给部署方的替换点：换掉鱼标与产品名，但不碰这一行的其它控件。
      ctx.slots.inject("sidebar.brand.mark", () =>
        ctx.slots.register({ name: "sidebar.brand.mark" }, BiyouBrandMark),
      );
      ctx.slots.inject("sidebar.brand.name", () =>
        ctx.slots.register({ name: "sidebar.brand.name" }, BiyouBrandName),
      );
    }

    const inject = ["slots"];

    exports.apply = apply;
    exports.inject = inject;
    return module.exports;
  },
});
