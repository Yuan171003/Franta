# Franta

[English](README.md)

Franta 是一个支持持久化与中断恢复的多智能体数学研究调度器。它通过 Codex CLI
协调主智能体、研究工作者、整理者、事实验证者与综合者。只有确定性的调度器能够写入
正式记忆；智能体通过受限工具提交研究提案和计算证据。

可选的 Explorer 先探索并记录尚未验证的想法，再由 Franta 选择、验证和整合。
可选的 Advisor 在每轮研究后提出下一轮子问题，等待用户选择。本地网页可查看
研究进展、记忆、模型用量和反馈请求。

## 环境要求

| 组件 | 要求 |
| --- | --- |
| Python | 3.11 或更新版本；运行时不依赖第三方 Python 包 |
| 操作系统 | macOS 或 Linux；运行时使用 POSIX 文件锁和 Unix socket，不支持原生 Windows，可在 WSL 等 Linux 环境使用 |
| Codex CLI | 实际研究必需；需要登录，并拥有配置所用模型的访问权限 |
| SageMath、Macaulay2 | 可选，单独安装并在配置中启用 |
| Tectonic | 可选，用于编译整理者申请人工指导时的 PDF 报告 |
| CAS / 报告沙箱 | macOS 使用系统 `sandbox-exec`；Linux 需要 `bubblewrap`（`bwrap`），且系统允许 user namespaces |

安装 Python 包、初始化空项目和运行离线测试不需要登录模型或安装 CAS。
实际研究会使用账户的模型额度。Linux 的沙箱实现仍受运行主机的安全策略限制。

## 下载与安装

使用仓库 GitHub 地址克隆，或者选择 **Code → Download ZIP** 后解压。
在包含 `pyproject.toml` 的目录执行：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
franta --help
```

开发时可改用 `python -m pip install -e .`。安装包名为 `franta-research`，
命令名与 Python 导入名都是 `franta`。这里安装的是本地源码，不要求项目已经发布到 PyPI。
安装包包含智能体技能文档和网页静态资源。

也可以直接从源码目录运行：

```sh
PYTHONPATH=src python3 -m franta.cli --help
sh bin/franta --help
```

按照 [OpenAI 官方说明](https://learn.chatgpt.com/docs/codex/cli)安装 Codex CLI，
在运行 Franta 的同一系统账户下登录：

```sh
codex --version
codex -c 'cli_auth_credentials_store="file"' login
codex login status
```

Franta 为研究创建隔离的 Codex home，并复制 `CODEX_HOME` 中的 `auth.json`；
未设置该变量时从 `~/.codex` 读取。仅保存在系统 keyring 的登录信息不会被复制，
因此上面显式选择文件存储。宿主 Codex 的配置、技能和插件不会导入研究进程。
API key 和无浏览器环境的登录方式见[官方认证文档](https://learn.chatgpt.com/docs/auth)。

## 第一个研究项目

先将示例复制到源码目录外，避免把研究数据误提交到仓库：

```sh
mkdir -p ../franta-work/config
cp examples/bootstrap.toml examples/root-problem.md examples/foundation.md ../franta-work/config/
```

编辑复制后的三个文件：

- `root-problem.md`：完整、明确地描述数学问题。
- `foundation.md`：说明允许的基础假设、已有定理和证明标准。
- `bootstrap.toml`：设置项目名称、输出目录、模型和已安装的工具。

配置中的相对路径以 TOML 所在目录为起点。示例设置
`directory = "../example-project"`，因此以下命令创建
`../franta-work/example-project`：

```sh
franta init ../franta-work/config/bootstrap.toml
franta status ../franta-work/example-project
```

这两步仅初始化、查看本地状态，不调用模型。确认认证和模型兼容性后启动：

```sh
franta start ../franta-work/config/bootstrap.toml
```

研究在前台运行，直到完成、持久化的人工反馈/异常暂停、当前无可执行工作，或被中断。
按 `Ctrl-C` 停止后，使用已生成的项目目录恢复：

```sh
franta resume ../franta-work/example-project
```

配置在初始化时固定。修改原始 TOML 不会修改已存在项目；使用变更后的配置对同一项目
再次 `start` 会被拒绝。需要更换研究配置时，选择新的项目目录。恢复已有项目应使用
`resume PROJECT`，不要直接修改数据库或私有配置。

本次 Franta 发布版面向新建项目。早期内部版本使用的持久化名称和结构已发生变化，
当前没有历史研究项目迁移工具。不要用此版本直接恢复旧数据；旧研究应保留其对应的原程序。

## SageMath、Macaulay2 与报告编译

未配置的可选工具不启用。仅在本机已安装时，在 `[tools]` 中加入对应项目：

```toml
[tools]
codex = "codex"
sage = "sage"
macaulay2 = "M2"
tectonic = "tectonic"
```

工具名通过当前 `PATH` 查找，也支持绝对路径、`~/...` 和相对 TOML 的路径。
每个值必须是一个可执行文件，不能填写 `conda run ...` 等整条 shell 命令。
Conda 环境建议先激活，再配置环境中实际的 `sage` 程序。

CAS 和报告编译不允许网络访问；缺少可用沙箱时会拒绝执行。Tectonic 使用只读缓存，
首次运行前需在普通终端中联网编译一次包含所需 LaTeX 宏包的模板。
未配置 Tectonic 不影响初始化或普通研究，但整理者的 PDF 人工指导流程需要它。
安装链接、缓存准备和诊断见[完整使用指南](docs/USAGE.md#tools-and-confinement)。

## 模型与兼容性

源码默认使用 `gpt-6-astra`：主智能体、整理者和验证者默认 `ultra`，综合者 `xhigh`，
工作者与 Explorer `max`，主排序器与 Advisor 默认 `ultra`。默认路由和综合者可由 manifest 配置，
部分路由仍固定在源码中。研究传输层配置的上下文窗口为 `872000`，自动压缩阈值为
`780000`。这些是本项目的配置，不代表所有账户或 Codex 版本都支持。

启动前确认账户有相应权限。CLI 还必须支持隔离运行所用参数，以及关闭其内置智能体
委派功能；不兼容时调度器会拒绝启动。参数详见[模型与运行时兼容性](docs/USAGE.md#models-and-runtime-compatibility)。

## Explorer、Advisor 和人工建议

在**新项目**的配置中加入以下表，可启用 `Explorer → Franta → Advisor → Explorer → …`：

```toml
[explorer]
enabled = true

[advisor]
enabled = true
```

Advisor 必须与 Explorer 一起启用，Explorer 可单独启用。禁用时删除对应表，
不要设置 `enabled = false`。并发和时间配置见示例 TOML。

Advisor 每次提出五项任务，等待你选择一到两项后才继续研究。通过 `status` 或网页
获取真实请求 ID 和任务 ID，替换下例中的占位符：

```sh
franta advisor-feedback PROJECT REQUEST_ID \
  '{"choices":[{"kind":"listed","obligation_id":"OBLIGATION_ID"}],"instructions":"优先研究这项任务。"}'
franta resume PROJECT
```

可以使用 `@/path/to/feedback.json` 传入 JSON 文件；自定义选项格式为
`{"kind":"custom","statement":"完整的子问题陈述"}`。`choices` 是绑定决策，
`instructions` 不能替代或推翻它。原始 ROOT 问题不会被修改。

运行期间也可以提交研究建议：

```sh
franta suggest PROJECT "尝试先证明相对情形。"
franta suggest PROJECT @/path/to/guidance.md
```

建议将在下一个符合条件的 Explorer 首次尝试或 Franta Main 调用中交付，
不会插入已开始的调用，也不会直接变成已验证事实。

## 本地网页

`start` 和 `resume` 会启动或复用网页，并打印实际地址。默认端口 `1113`，被占用时
递增。可以加 `--no-dashboard` 关闭自动启动，或 `--dashboard-port PORT` 更换起始端口。
独立打开已有项目：

```sh
franta dashboard PROJECT --port 1113
```

网页仅绑定 `127.0.0.1`。查看记忆、分页和筛选不调用模型；手动刷新摘要，以及研究
运行或等待反馈时每 90 分钟自动刷新摘要，会调用独立的只读模型，单独显示其用量。
网页支持 Advisor 选择和研究建议；网页提交的有效当前轮 Advisor 反馈可恢复暂停项目。

## 测试与发布

在源码目录执行完整自动化测试和只读评估示例：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m franta.evaluation \
  --snapshot evals/fixtures/complete_observation.json
```

离线测试覆盖调度、持久化、恢复、隔离、传输契约及可选子系统，不需要真实模型账户。
真实 Codex 测试另见[评估指南](evals/README.md)和[在线探针说明](evals/LIVE_AGENT_PROBES.md)。
离线测试通过并不证明线上模型可访问，也不证明最终数学结果正确。

生成干净发布目录与源码 ZIP：

```sh
python3 scripts/export_release.py
```

默认输出 `release/Franta/` 和 `release/Franta-source.zip`。GitHub 上传、wheel/sdist
构建与下载后的验证步骤见[发布指南](docs/PUBLISHING.md)。研究项目包含私有日志与
可能复制的认证文件，不能作为源码一起上传。

## 代码与设计

- `src/franta/`：主调度器、记忆、执行网关、命令行和适配器。
- `src/explorer_system/`、`src/advisor_system/`、`src/dashboard_system/`：可独立集成的子系统，各有 README。
- `.agents/skills/`：技能文档源码，随运行时一起打包。
- `examples/`：最小输入和配置；`tests/`、`evals/`：测试与评估工具。
- `Design.md`、`IMPLEMENTATION.md`：研究流程设计与实现决策。

## 许可

当前包元数据仍标为 **Proprietary**，本仓库没有授予开源许可证；需要许可的使用方式
应向作者取得授权。网页中的第三方资源保留各自的[版权与许可说明](src/dashboard_system/static/vendor/NOTICE.md)。
