// 前端 lint 配置（Wave34 接入 `dev_diagnostics`，见 docs/HANDOFF-vibecoding.md §5.27）。
//
// ## 为什么用 `vue/essential` 而不是 `vue/recommended`
//
// 实测（2026-10-03，eslint 10.12.0，129 个文件 / 39352 行）：
//
//   vue/recommended → 7413 条（error 34 / **warning 7379**）
//   vue/essential  →   34 条（error 34 / warning    0）
//
// 那 7379 条全是**排版**规则（`max-attributes-per-line` 4079、
// `singleline-html-element-content-newline` 1758、`html-indent` 976…）——那是 Prettier 的活。
// 留在 eslint 里只会把真问题埋掉，所以这里只要 `essential`。
//
// ## 为什么 `parserOptions.parser` 必须写（这一行是踩过的）
//
// 少了它，`vue-eslint-parser` 会用**纯 JS parser** 去解 `<script lang="ts">`：
// 实测 57 个 `.vue` 文件全部报 `Parsing error: The keyword 'interface' is reserved` ——
// 而它们**一行都没被检查**，看上去却像"只有几条小问题"。识别它的线索就是那句
// `interface is reserved`。这是典型的假干净：绿，但盲。
//
// ## 三条豁免为什么是这样
//
// 都是实测出来的"刻意写法"，不是疏忽：
//   - `no-unused-vars`：`_ok` / `_e` 这种下划线开头是**故意**从 rest 里摘掉的
//   - `no-explicit-any`：`window as any` 是浏览器 API 探测的标准写法（逃生口）
//   - `no-empty`：`catch {}` 是**故意**吞掉的（隐私模式下 sessionStorage 写失败不该炸 UI）
import js from "@eslint/js";
import tseslint from "typescript-eslint";
import pluginVue from "eslint-plugin-vue";
import vueParser from "vue-eslint-parser";
import globals from "globals";

export default tseslint.config(
  {
    ignores: ["node_modules/**", "dist/**", ".tmp/**", ".docmind/**"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs["flat/essential"],
  {
    files: ["**/*.vue"],
    languageOptions: {
      parser: vueParser,
      parserOptions: {
        parser: tseslint.parser,          // ← 少了这行，下面 57 个文件全部解析失败（盲区）
        ecmaVersion: "latest",
        sourceType: "module",
      },
      globals: { ...globals.browser, ...globals.node },
    },
  },
  {
    files: ["**/*.ts"],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
  },
  {
    rules: {
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", ignoreRestSiblings: true },
      ],
      "@typescript-eslint/no-explicit-any": "off",
      "no-empty": ["error", { allowEmptyCatch: true }],
    },
  },
);
