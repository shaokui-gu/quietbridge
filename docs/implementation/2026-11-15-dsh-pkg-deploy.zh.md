# DSH bundle 接入 PKG 用户部署（2026-11-15）

在 [DSH 适配](2026-11-15-dsh-adaptation.zh.md) 与 [隔离实机验证](2026-11-15-dsh-isolated-verification.zh.md)
之后，本轮把 DSH bundle 的生成接入了正常安装路径：用户安装 QuietBridge PKG 后，
`deploy.py` 会从已校验的 release 内渲染一份带具体路径的 DSH bundle，随其它用户文件
一起事务化提交。用户只需用官方 CLI 把它装进目标 DSH profile，不再需要源码
checkout 或手工渲染。

## 1. 行为

`packaging/macos/deploy.py` 在 release 内容哈希与签名校验通过后：

1. 以 release 内 `QuietBridge.app/Contents/Resources/bridge/adapters/deepseek_harness/bundle`
   为模板，调用 `adapters.deepseek_harness.configuration.bundle_render(..., apply=False)`
   纯预览渲染；
2. 把预览计划的 7 个文件（`package.json`、`cordis.patch.yml`（渲染后文本）、`index.js`、
   `locale/{en,zh}.json`、`icon.svg`、`README.md`）作为普通文件项加入既有
   `Transaction`，与 MCP 配置、hooks、扩展宿主等一起提交/回滚；
3. 渲染配置固定为实际安装路径：
   - `connector`：release 内 `bridge/server.py`
   - `helper`：本次安装的系统 helper
   - `configDir`：`~/Library/Application Support/QuietBridge`
   - `python`：`/usr/bin/python3`（不存在时退回 deploy 的解释器）
   - `serverName`：`quietbridge`
4. 输出位置：`~/Library/Application Support/QuietBridge/releases/<digest>/dsh-bundle`；
   deploy 结果与事务 journal 记录：
   `dshBundle = {installed: true, path, files: 7, installCommand: "dsh plugin --profile <profile> add <path>"}`；
5. DSH 是可选宿主：模板缺失、`package.json` 损坏等渲染失败只记录
   `{installed: false, reason}`，**不阻断** Workbuddy/PKG 主安装；
6. 事务回滚会删除新渲染的 bundle 文件；残留的空派生目录在下次部署前按“无
   `package.json` 的 `release/dsh-bundle`”识别并清理后重渲染。

安装到 DSH profile 仍是显式动作（不写 `~/.dsh`）：

```sh
dsh plugin --profile <profile> add "$HOME/Library/Application Support/QuietBridge/releases/<digest>/dsh-bundle"
```

## 2. 验证

| 检查 | 结果 |
| --- | --- |
| `env -u PYTHONPATH python3 -m unittest discover -s packaging/macos -p 'test_*.py' -q` | **53 项通过**（基线 47 + 新增 6） |
| `env -u PYTHONPATH python3 -m unittest discover -s services/mcp -p 'test_*.py' -q` | 489 项通过 |
| DSH 隔离实机检查 | 通过（12.5s，toolCount=19） |
| `python3 -m py_compile packaging/macos/deploy.py`、`git diff --check` | 通过 |

新增部署测试覆盖：

- release 内 bundle 渲染成功：7 文件、`package.json` 名称正确、patch 中
  `connector/helper/configDir/python` 均为预期绝对路径、无 `!!js process.env`；
- 文件 mode `0600`、journal `phase=committed` 且 `dshBundle` 与返回结果一致；
- 重复部署幂等；
- 模板缺失 / `package.json` 损坏时部署仍成功，`dshBundle.installed=false`；
- 事务提交失败回滚删除新 bundle 文件；
- 回滚残留空目录后再次部署能自愈并重新渲染。

## 3. 限制

- 本轮只在打包测试的临时部署目录中验证渲染与事务行为；未执行真实 PKG 安装
  （需要签名、root helper、系统路径），真实安装后的路径/权限仍需一次安装验收。
- 每个 release digest 增加约 81KB（`index.js` 约 71KB + README 等），release 保留策略不变。
- 若 `release/dsh-bundle` 中存在**损坏的 `package.json`**，出于安全不自动清理，本次
  部署降级为 `installed=false`；正常回滚残留（无 `package.json`）会自愈。
- `python` 解释器取 `/usr/bin/python3`（Apple CLT 提供）；若该机器没有它，则退回
  deploy 解释器路径，需在真实安装机复核。
- DSH 预览宿主绑定、真实 helper 权限/TCC、并发等仍按前两轮文档保持未验收。
