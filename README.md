# faster-source

在本机发行版的镜像名单里，用大约 5 秒选出当前最快的软件源，并改写对应的源文件。探测期间不改系统配置。没有合格结果时，源文件保持原样。

程序读取 `/etc/os-release` 判断发行版和套件，再按 `mirrors.yaml` 里该发行版的名单，并发请求每个镜像上的一个小文件。合格结果里耗时最短的胜出；耗时相同则保留名单中更靠前的那一个。选中之后，把源文件里已知的官方地址和名单中的镜像地址换成胜出地址。注释行、带选项的 `deb` 行，以及其他仓库保持原样。

改写前会在同目录留下 `原文件名.bak.YYYYMMDDHHMMSS`。写入使用临时文件再替换，避免源文件停在半截内容。目标文件不可写时（例如没有 root），把将要写入的内容打到标准输出，不创建备份。

程序只改源文件，不执行 `apt`、`dnf`、`zypper` 或 `pacman`，也不在运行时去网上拉取镜像列表。

## 支持的发行版

名单和探测方式都在 `mirrors.yaml`。

| 发行版 | 源文件 | 用来确认镜像的文件 |
| --- | --- | --- |
| Ubuntu，以及 Linux Mint、Pop!_OS 等衍生版 | `/etc/apt/sources.list`，以及 `sources.list.d` 里的 `.list` / `.sources` | `dists/{suite}/InRelease` |
| Debian、Raspberry Pi OS | 同上 | 同上 |
| Kali | `/etc/apt/sources.list` 及 `sources.list.d` | 同上 |
| Fedora、Rocky Linux、AlmaLinux | `/etc/yum.repos.d` | `repomd.xml` |
| openSUSE Leap、Tumbleweed | `/etc/zypp/repos.d` | `repomd.xml` |
| Arch Linux | `/etc/pacman.d/mirrorlist` | `lastupdate` |
| Alpine | `/etc/apk/repositories` | `v{版本}/main/` |

套件来自 `os-release`。Ubuntu 衍生版优先使用 `UBUNTU_CODENAME`。Arch 和 openSUSE Tumbleweed 使用固定套件 `rolling`。

## 依赖

- Python 3.10（只使用标准库，不需要 PyYAML）
- `curl`

## 用法

先看会改成什么，不写文件：

```bash
python3 select_mirror.py --dry-run
```

确认后再写入。系统源文件通常需要 root：

```bash
sudo python3 select_mirror.py
```

标准错误会打印选中结果，例如：

```text
selected name=ubuntu-aliyun baseurl=https://mirrors.aliyun.com/ubuntu time_total=0.120000
```

常用参数：

```bash
python3 select_mirror.py \
  --config mirrors.yaml \
  --release /etc/os-release \
  --distro ubuntu --suite jammy \
  --sources ./sources.list \
  --dry-run
```

`--config` 默认是脚本旁边的 `mirrors.yaml`。`--distro` 和 `--suite` 要一起给；都不给时从 `--release` 识别。`--sources` 不给时使用该发行版自己的源文件路径。

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 已选中。文件已更新，或本来就是该镜像，或以 dry-run 打印 |
| 1 | 没有合格镜像，源文件未改 |
| 2 | 配置缺失或格式不对 |
| 3 | 超过 `deadline_sec`（默认 5 秒），源文件未改 |
| 4 | 源文件不存在，或备份、写入失败 |

## 测试

测试不访问网络：

```bash
python3 -m unittest test_select_mirror -v
```

增加镜像时改 `mirrors.yaml` 即可。探测超时、发行版匹配和源文件路径也在这个文件里。
