# 基金管理工具 v2.0 (Linux)

个人 A 股基金实时估值与 AI 综合分析系统。FastAPI 后端 + 单文件前端 HTML，支持：

- 实时基金估值（盘中估算涨跌幅）
- 3 只基金专属估值模型：
  - **011609 易方达上证科创50联接C** → 指数跟随（科创50）
  - **004746 易方达上证50增强C** → 多因子加权回归
  - **020741 华泰保兴安悦债券C** → 债基基线 + gsz 残差修正
- AI 综合分析卡片（动态提示词 + 跑赢基准信号）
- 上证指数近 30 日柱状图 + 收盘价折线
- 市场资讯情绪+事件标签分类 + 顶部情绪概览条
- 新闻正文就地预览（trafilatura 提取，去图片）
- 深色模式开关（iOS 风格）
- 交易日历（自动跳过周末/节假日）
- 买点信号（基于基金历史回撤阈值）

---

## 📦 目录结构

```
fund-manager-v2.0/
├── start.sh                # 启动脚本（自动创建 venv + 装依赖）
├── stop.sh                 # 停止脚本
├── README.md               # 本文件
├── .gitignore
└── web-app/
    ├── index.html          # 单文件前端（所有逻辑都在里面）
    ├── manifest.json
    ├── icon-180.png
    ├── icon-192.png
    └── backend/
        ├── main.py         # FastAPI 后端
        ├── requirements.txt
        ├── buy_points.json
        └── buy_point_refs.json
```

---

## 🚀 快速开始

### 1. 环境要求

- **Python 3.10+**（含 `venv` 和 `pip`）
- Linux x86_64 / ARM64 均可
- 500MB 磁盘空间
- 可选：`git`（用于后续升级）

### 2. 安装 Python（如未安装）

```bash
# Ubuntu / Debian
sudo apt update
sudo apt install -y python3 python3-venv python3-pip

# CentOS / RHEL / Rocky
sudo yum install -y python3 python3-pip

# Arch
sudo pacman -S python python-pip
```

### 3. 启动服务

```bash
# 给脚本加执行权限（首次）
chmod +x start.sh stop.sh

# 启动
./start.sh
```

`start.sh` 会自动：
1. 检测 Python 版本
2. 在 `./venv/` 创建虚拟环境
3. 安装 `requirements.txt` 里的所有依赖（首次 1-2 分钟）
4. 检查端口占用，必要时清理旧进程
5. 后台启动 FastAPI 服务，输出到 `backend.log`
6. 健康检查

启动成功后访问：
- 本机：http://localhost:8000/
- 手机（同一 WiFi）：http://`<服务器IP>`:8000/

### 4. 停止服务

```bash
./stop.sh
```

### 5. 查看日志

```bash
tail -f backend.log
# 或
journalctl -f    # 如果用 systemd
```

---

## ⚙️ 自定义配置

通过环境变量调整：

```bash
# 改端口（默认 8000）
PORT=9000 ./start.sh

# 指定 Python 可执行文件
PYTHON=python3.11 ./start.sh

# 只监听本机（更安全）
HOST=127.0.0.1 ./start.sh
```

---

## 🌐 外网访问

### 方案 A：SSH 端口转发（最简单）

在你的家用电脑/服务器上启动后，在外网电脑上：

```bash
ssh -L 8000:localhost:8000 user@your-server-ip
```

然后本机访问 http://localhost:8000/。

### 方案 B：frp / Sakura Frp 内网穿透

参考 V2.0-DEPLOY.md（开发版部署文档）。

### 方案 C：云服务器直接部署

直接把整个 `web-app/` 目录上传到云服务器，运行 `./start.sh` 即可。FastAPI 默认绑定 `0.0.0.0:8000`。

---

## 🔄 升级

如果有新版本发布：

```bash
# 1. 备份当前数据
cp web-app/backend/buy_points.json web-app/backend/buy_points.json.bak

# 2. 拉取最新代码（如果是 Git 仓库）
git pull

# 3. 手动覆盖：用新版本的 main.py / index.html 覆盖

# 4. 重启
./stop.sh
./start.sh
```

---

## 🔐 安全注意

1. **访问密码**：默认密码是 `jjjr`，在 `web-app/index.html` 中修改（搜索 `ACCESS_PASSWORD`）。
2. **不暴露到公网**：如必须暴露，强烈建议加 Nginx 反向代理 + HTTPS。
3. **API 限流**：当前未做限流，生产环境建议在 Nginx 层加 `limit_req`。
4. **不要提交密钥**：`web-app/backend/*.pem` 和 `*.key` 已在 `.gitignore` 中排除。

---

## 🐛 常见问题

**Q: 启动报 `python3: command not found`？**
A: 先装 Python：`sudo apt install python3`（Ubuntu/Debian）。

**Q: pip install 失败 / 速度慢？**
A: 用国内镜像：
```bash
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r web-app/backend/requirements.txt
```

**Q: 端口 8000 被占用？**
A: 用其他端口：`PORT=9000 ./start.sh`，或先 `sudo lsof -i:8000` 找到进程杀掉。

**Q: 数据缓存（JSON）损坏？**
A: 删掉 `web-app/backend/*.json` 缓存文件（`buy_points.json` 除外，那个是用户数据），重启会自动重建。

**Q: 手机访问不到？**
A:
1. 确认手机和服务器在同一局域网
2. 防火墙放行端口：`sudo ufw allow 8000/tcp`（Ubuntu）
3. 用 `hostname -I` 查看服务器 IP

---

## 📝 版本

**v2.0.0** (2026-07)

主要特性：
- 3 只基金专属估值模型
- AI 综合分析（动态提示词）
- 市场资讯情绪分析 + 标签
- 新闻就地预览
- 深色模式
- 交易日历 / 残差修正 / 盘中快照

---

## 📄 许可

个人项目，仅供学习和自用。
