# faster-source

在本机发行版的镜像名单里，用不超过 `deadline_sec`（随仓库为 5 秒）选出最快的软件源，并改写对应的源文件。探测期间不改系统配置。没有合格结果时，源文件保持原样。

行为以 `select_mirror.py`、`mirrors.yaml` 和 `test_select_mirror.py` 为准。`docs/superpowers/` 是早期只支持 Ubuntu 的设计和计划，和现在的多发行版目录冲突时，以代码和测试为准。

## 布局

- `select_mirror.py`：YAML 子集解析、发行版识别、并发 `curl` 探测、源文件改写、CLI。
- `mirrors.yaml`：探测超时、`/etc/os-release` 的相对路径、各发行版的探测方式和镜像。只有数据，没有逻辑。
- `test_select_mirror.py`：`unittest`。注入假的探测结果，不访问网络。
- 只使用 Python 3 标准库。不要引入 PyYAML 或其他第三方包。目标环境是 Ubuntu 22.04 上的 Python 3.10。

## 命令

```bash
python3 -m unittest test_select_mirror -v
python3 select_mirror.py [--config mirrors.yaml] [--sources PATH] [--release PATH] [--distro ID] [--suite SUITE] [--dry-run]
```

`--config` 默认是脚本同目录的 `mirrors.yaml`。`--distro` 和 `--suite` 必须同时给出；都不给时从 `--release`（默认 `/etc/os-release`）识别。`--sources` 不给时用该发行版 profile 的 `sources` 路径。

改完选择、改写、退出码或 YAML 结构后，跑上面的 unittest。测试不得打开 socket，也不得调用真实的 `curl` 去访问镜像；通过 `main(..., runner=...)` 注入结果。

## 退出码和标准错误

| 码 | 含义 |
| --- | --- |
| 0 | 已选中。文件已更新，或无需更新，或以 dry-run / 不可写方式把新内容打到标准输出 |
| 1 | 没有合格镜像。源文件未改。标准错误为 `selected none` |
| 2 | YAML 缺失或不符合子集。标准错误以 `error:` 开头。不写源文件 |
| 3 | 探测结束后已经超过 `deadline_sec`。不写源文件 |
| 4 | 源文件不存在，或备份 / 写入失败。不创建缺失文件 |

选中后标准错误一行：`selected name={name} baseurl={baseurl} time_total={seconds}`，`time_total` 固定六位小数。

## YAML 子集

顶层只有 `probe`、`release`、`distros`。缩进是 2 的倍数个空格。制表符、子集以外的结构，都抛 `ConfigError`。`#` 开头的整行是注释。

- `probe` 的三个键都是正整数（不允许前导 0），且 `connect_timeout_sec` ≤ `max_time_sec` ≤ `deadline_sec`。
- `release` 是相对根目录的路径片段，例如 `etc` + `os-release` 表示 `/etc/os-release`。`mirrors.yaml` 里不要写 `/etc/`、`/home/`、`/usr/`、`/tmp/`、`/var/` 这类绝对路径。
- 每个 distro 需要 `id`，以及 `suite_keys` 或固定 `suite`。`match` 缺省为 `id`。`probe_path` 缺省 `dists/{suite}/InRelease`。`marker` 缺省 `Codename: {suite}`。
- 没有固定 `suite` 的 profile，`probe_path` 必须含 `{suite}`。探测路径不要写 `x86_64`、`aarch64`、`amd64`。
- `mirrors` 每项有唯一的 `name` 和 `baseurl`。`baseurl` 以 `http://` 或 `https://` 开头，不含空白；使用前去掉末尾所有 `/`。
- `bases` 是官方源 URI 前缀，和镜像 `baseurl` 一起参与改写。`sources` 是源文件相对根目录的路径片段。`also` 是源文件父目录旁的额外目录名（apt 用 `sources.list.d`），里面只收集 `.list` 和 `.sources`。

发行版识别：按 `ID`、再按 `ID_LIKE` 的词，匹配第一个 `match` 命中的 profile。套件用固定 `suite`，否则用 `suite_keys` 里第一个有值的键（Ubuntu 衍生版因此先看 `UBUNTU_CODENAME`）。

## 探测和改写

每个镜像一个 `curl`，并发执行。命令固定为：

```text
curl --silent --show-error --max-redirs 0 \
  --connect-timeout {connect_timeout_sec} --max-time {max_time_sec} \
  --output {tempfile} --write-out '%{http_code} %{time_total}' \
  {baseurl}/{probe_path}
```

`--max-redirs 0` 让跳到其他主机的镜像失败。从进程开始最多等 `deadline_sec - 0.2` 秒，然后杀掉仍在跑的 `curl`；超时回调出错也保留已经完成的结果。剩下的 0.2 秒只用于本地备份和写文件。`curl` 不存在时，每个镜像记为失败，标准错误只打印一次 `error: curl: ...`，并删掉临时文件。

合格结果要同时满足：`curl` 退出码 0、HTTP 200，以及 marker：

- `Codename:` 开头：正文有一行与 `marker.format(suite=...)` 完全相同
- `digits`：正文去掉空白后全是数字（Arch `lastupdate`）
- `any`：正文非空（Alpine）
- 其他：marker 字符串出现在正文里（`repomd`）

若结果带了 `final_url`，其主机名必须和探测 URL 的主机名相同。合格结果按 `time_total` 升序，时间相同则保持 YAML 里的先后。

改写时，把已知 `baseurl` 和 `bases` 前缀换成胜出 `baseurl`（按前缀长度从长到短）。注释行，以及带 `[...]` 选项的 `deb` / `deb-src` 行，保持原样。前缀两侧要落在空白、引号或结尾上，避免吃掉更长的主机名。套件名和路径后缀原样保留。胜出前缀已经在文件里时不写文件。

需要写入时：复制为同目录 `{原名}.bak.YYYYMMDDHHMMSS`，写到临时文件并保留原权限，再用 `os.replace` 换上。`--dry-run`，或任一目标不可写时，把将要写入的全文打到标准输出，不建备份。备份或替换失败则退出码 4，原文件字节保持不变。

不要在选源之后运行 `apt`、`dnf`、`zypper` 或 `pacman`。不要在运行时拉取镜像列表。
